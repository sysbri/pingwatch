"""System metrics, restart/factory-reset, diagnose bundle."""

from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from pingwatch.api import _queries_compat as q
from pingwatch.api.deps import ConnDep
from pingwatch.api.schemas import OkResponse, SystemMetrics

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

HOST_FIFO = Path("/run/pingwatch-host.fifo")


def _read_cpu_pct() -> float:
    try:
        load1, _, _ = os.getloadavg()
        return round(load1 * 100.0 / max(1, os.cpu_count() or 1), 1)
    except OSError:
        return 0.0


def _read_ram() -> tuple[int, int]:
    try:
        from pingwatch.system import pi_metrics  # type: ignore[attr-defined]

        used, total = pi_metrics.ram_mb()
        return used, total
    except Exception:  # noqa: BLE001
        pass
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return 0, 0
    info: dict[str, int] = {}
    for ln in meminfo.read_text().splitlines():
        parts = ln.split()
        if len(parts) >= 2:
            try:
                info[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                continue
    total = info.get("MemTotal", 0) // 1024
    avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
    return total - avail, total


def _read_temp() -> float | None:
    try:
        from pingwatch.system import pi_metrics  # type: ignore[attr-defined]

        return pi_metrics.cpu_temp_c()
    except Exception:  # noqa: BLE001
        pass
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    if path.exists():
        try:
            return round(int(path.read_text().strip()) / 1000.0, 1)
        except (ValueError, OSError):
            return None
    return None


def _read_sd(target: str = "/") -> tuple[float, float]:
    try:
        usage = shutil.disk_usage(target)
        return (
            round((usage.total - usage.free) / 1_000_000_000, 2),
            round(usage.total / 1_000_000_000, 2),
        )
    except OSError:
        return 0.0, 0.0


_START_TS = time.time()


@router.get("", response_model=SystemMetrics)
async def get_system(conn: ConnDep) -> SystemMetrics:
    ram_used, ram_total = _read_ram()
    sd_used, sd_total = _read_sd()
    db_bytes = await q.db_size_bytes(conn)
    wifi = await q.wifi_current(conn)
    return SystemMetrics(
        cpu_pct=_read_cpu_pct(),
        ram_used_mb=ram_used,
        ram_total_mb=ram_total,
        temp_c=_read_temp(),
        sd_used_gb=sd_used,
        sd_total_gb=sd_total,
        db_size_mb=round(db_bytes / 1_000_000, 2),
        uptime_seconds=int(time.time() - _START_TS),
        version=f"0.1.0 ({platform.python_version()})",
        wifi=wifi,
    )


@router.post("/diagnose-bundle")
async def diagnose_bundle(conn: ConnDep) -> StreamingResponse:  # noqa: ARG001
    try:
        from pingwatch.system import diagnose  # type: ignore[attr-defined]

        data = await diagnose.build_bundle()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"diagnose unavailable: {type(exc).__name__}",
        ) from exc
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=pingwatch-diagnose-{int(time.time())}.zip"
            )
        },
    )


def _write_host_command(cmd: str) -> bool:
    if not HOST_FIFO.exists():
        log.warning("host-fifo-missing", path=str(HOST_FIFO), cmd=cmd)
        return False
    try:
        with open(HOST_FIFO, "w") as fh:
            fh.write(cmd + "\n")
        return True
    except OSError as exc:
        log.warning("host-fifo-write-failed", error=str(exc))
        return False


@router.post("/restart", response_model=OkResponse)
async def restart_system() -> OkResponse:
    # The host helper (deploy/pingwatch-host-helper.sh) expects the verb
    # `reboot` for a full pi reboot. Use `restart_app` to only bounce the
    # container — exposed via /api/system/restart-app if needed.
    ok = _write_host_command("reboot")
    return OkResponse(ok=ok, detail="restart triggered" if ok else "fifo unavailable")


@router.post("/factory-reset", response_model=OkResponse)
async def factory_reset(
    conn: ConnDep, confirm: str = Query(default=""),
) -> OkResponse:
    if confirm != "yes":
        raise HTTPException(
            status_code=400,
            detail="confirmation required: pass ?confirm=yes",
        )
    await q.factory_reset(conn)
    # The helper does its own `docker compose down -v` + reboot; we just
    # need to trigger the `factory_reset` verb.
    _write_host_command("factory_reset")
    return OkResponse(ok=True, detail="factory reset complete")


@router.post("/clear-data", response_model=OkResponse)
async def clear_data(conn: ConnDep) -> OkResponse:
    await q.purge_all_metrics(conn)
    return OkResponse(ok=True, detail="metrics purged")


@router.get("/info")
async def info() -> dict[str, Any]:
    return {
        "version": "0.1.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_at": int(_START_TS),
    }
