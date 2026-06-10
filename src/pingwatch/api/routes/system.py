"""System metrics, restart/factory-reset, diagnose bundle."""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from pingwatch.api import host_fifo
from pingwatch.api.deps import ConnDep
from pingwatch.api.schemas import OkResponse, SystemMetrics
from pingwatch.db import queries as q

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

# Written by the host-helper `check_update` command (git fetch + behind-count).
_SHARED_DIR = Path("/run/pingwatch-shared")
_UPDATE_STATUS_FILE = _SHARED_DIR / "update-status.json"
# Written by the detached update runner (deploy/pingwatch-update).
_UPDATE_RESULT_FILE = _SHARED_DIR / "update-result.json"


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
    except Exception:  # noqa: BLE001,S110
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
    except Exception:  # noqa: BLE001,S110
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


# GET so the frontend's `window.location = '/api/system/diagnose-bundle'` works.
@router.get("/diagnose-bundle")
async def diagnose_bundle(conn: ConnDep) -> StreamingResponse:
    from pingwatch.export import zip_bundle

    try:
        path = await zip_bundle.build_diagnose_bundle(conn)
    except Exception as exc:  # noqa: BLE001
        log.exception("diagnose-bundle-failed")
        raise HTTPException(
            status_code=503,
            detail=f"diagnose failed: {type(exc).__name__}",
        ) from exc

    def _iter() -> Any:
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(64 * 1024):
                    yield chunk
        finally:
            path.unlink(missing_ok=True)

    return StreamingResponse(
        _iter(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=pingwatch-diagnose-{int(time.time())}.zip"
            )
        },
    )


async def _write_host_command(cmd: str) -> bool:
    try:
        await host_fifo.write_command(cmd)
        return True
    except FileNotFoundError:
        log.warning("host-fifo-missing", cmd=cmd)
        return False
    except (PermissionError, TimeoutError, OSError) as exc:
        log.warning("host-fifo-write-failed", error=str(exc))
        return False


@router.post("/restart", response_model=OkResponse)
async def restart_system() -> OkResponse:
    # The host helper (deploy/pingwatch-host-helper.sh) expects the verb
    # `reboot` for a full pi reboot. Use `restart_app` to only bounce the
    # container — exposed via /api/system/restart-app if needed.
    ok = await _write_host_command("reboot")
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
    await _write_host_command("factory_reset")
    return OkResponse(ok=True, detail="factory reset complete")


@router.post("/clear-data", response_model=OkResponse)
async def clear_data(conn: ConnDep) -> OkResponse:
    await q.purge_all_metrics(conn)
    return OkResponse(ok=True, detail="metrics purged")


@router.post("/check-update", response_model=OkResponse)
async def check_update() -> OkResponse:
    # Tell the host-helper to `git fetch` and refresh update-status.json.
    ok = await _write_host_command("check_update")
    return OkResponse(ok=ok, detail="check triggered" if ok else "fifo unavailable")


@router.get("/update-status")
async def get_update_status() -> dict[str, Any]:
    """Last known update status written by the host-helper `check_update`.

    ``behind`` is the number of commits the local checkout is behind
    ``origin/main`` (0 = up to date, ``None`` = never checked / unavailable).
    """
    try:
        if _UPDATE_STATUS_FILE.exists():
            data = json.loads(_UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {"behind": None, "current_sha": None, "remote_sha": None, "ts_ms": None}


@router.post("/update", response_model=OkResponse)
async def install_update() -> OkResponse:
    # Host-helper starts the detached update runner (pull + installer +
    # restarts); progress lands in update-result.json (see /update-result).
    ok = await _write_host_command("update_check")
    return OkResponse(ok=ok, detail="update started" if ok else "fifo unavailable")


@router.get("/update-result")
async def get_update_result() -> dict[str, Any]:
    """Progress of the running/last update (written by the update runner).

    ``phase`` is one of pull/install/restart/done/failed; ``None`` when no
    update ran since boot.
    """
    try:
        if _UPDATE_RESULT_FILE.exists():
            data = json.loads(_UPDATE_RESULT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {"phase": None, "detail": None, "ts_ms": None}


@router.get("/info")
async def info() -> dict[str, Any]:
    return {
        "version": "0.1.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "started_at": int(_START_TS),
    }
