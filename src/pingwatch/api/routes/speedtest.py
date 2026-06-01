"""Speedtest API endpoints (Cloudflare provider, single-flight)."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from pingwatch.api.deps import ConnDep
from pingwatch.db import queries

router = APIRouter(prefix="/api/speedtest", tags=["speedtest"])

# Single-flight task registry (module-level). Only one speedtest may run
# concurrently to avoid blowing the daily byte budget and to keep the
# HttpTrickleWorker pause semantics simple.
_current_task: dict[str, Any] = {"task_id": None, "task": None}

_BUDGET_GUARD_BYTES = 50 * 1024 * 1024  # 25 MB down + 5 MB up + headroom
_DEFAULT_CAP_MB = 2048


async def _cap_mb(conn: Any) -> int:
    raw = await queries.get_setting(conn, "stream.daily_cap_mb")
    try:
        return int(raw) if raw is not None else _DEFAULT_CAP_MB
    except (TypeError, ValueError):
        return _DEFAULT_CAP_MB


async def _fetch_speedtest_row(conn: Any, task_id: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, ts_ms, down_mbps, up_mbps, latency_ms, jitter_ms, server, "
        "bytes_down, bytes_up, duration_ms, status, error, task_id "
        "FROM speedtests WHERE task_id = ?",
        (task_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def _fetch_last(conn: Any) -> dict[str, Any] | None:
    # Prefer dedicated query if Agent 1 has shipped it; else fall back to SQL.
    fn = getattr(queries, "speedtest_last", None)
    if fn is not None:
        return await fn(conn)
    cur = await conn.execute(
        "SELECT id, ts_ms, down_mbps, up_mbps, latency_ms, jitter_ms, server, "
        "bytes_down, bytes_up, duration_ms, status, error, task_id "
        "FROM speedtests WHERE status = 'done' ORDER BY ts_ms DESC LIMIT 1"
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run(conn: ConnDep) -> dict[str, Any]:
    # Already running -> return current task (200, idempotent client UX).
    cur = _current_task["task"]
    if cur is not None and not cur.done():
        return {"task_id": _current_task["task_id"], "status": "running", "eta_s": 20}

    # Load provider + iperf3 settings
    provider_raw = await queries.get_setting(conn, "speedtest.provider")
    provider = (provider_raw or "cloudflare").strip().lower()
    if provider not in {"cloudflare", "speedtest_net", "iperf3"}:
        provider = "cloudflare"
    iperf3_server = await queries.get_setting(conn, "speedtest.iperf3_server")
    iperf3_port_raw = await queries.get_setting(conn, "speedtest.iperf3_port")
    try:
        iperf3_port = int(iperf3_port_raw) if iperf3_port_raw else 5201
    except (TypeError, ValueError):
        iperf3_port = 5201
    settings_payload: dict[str, Any] = {
        "speedtest.provider": provider,
        "speedtest.iperf3_server": iperf3_server or "",
        "speedtest.iperf3_port": iperf3_port,
    }

    # Budget guard: only for Internet-providers (cloudflare / speedtest_net).
    # iperf3 is LAN -> kein Internet-Volumen.
    if provider in {"cloudflare", "speedtest_net"}:
        used = await queries.bytes_today(conn)
        cap_bytes = (await _cap_mb(conn)) * 1024 * 1024
        if used + _BUDGET_GUARD_BYTES > cap_bytes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "daily_cap_exceeded",
                    "bytes_today": used,
                    "cap_bytes": cap_bytes,
                },
            )

    # Lazy import: another agent owns the worker module.
    try:
        from pingwatch.streams.speedtest import run_speedtest  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"speedtest worker unavailable: {type(exc).__name__}",
        ) from exc

    task_id = secrets.token_hex(8)
    task = asyncio.create_task(run_speedtest(conn, task_id, settings_payload))
    _current_task["task_id"] = task_id
    _current_task["task"] = task
    eta = 5 if provider == "iperf3" else 20
    return {"task_id": task_id, "status": "running", "eta_s": eta, "provider": provider}


@router.get("/status")
async def get_status(conn: ConnDep) -> dict[str, Any]:
    task = _current_task["task"]
    task_id = _current_task["task_id"]
    if task is not None and not task.done():
        return {"status": "running", "task_id": task_id}

    if task is not None and task.done():
        row = await _fetch_speedtest_row(conn, task_id) if task_id else None
        # Reset registry once result is observable.
        if row is None or row.get("status") in ("done", "failed", "aborted"):
            _current_task["task_id"] = None
            _current_task["task"] = None
        if row is not None:
            return {"status": row.get("status", "done"), "task_id": task_id, "result": row}

    last = await _fetch_last(conn)
    return {"status": "idle", "last": last}


@router.get("/history")
async def history(
    conn: ConnDep,
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT id, ts_ms, down_mbps, up_mbps, latency_ms, jitter_ms, server, "
        "bytes_down, bytes_up, duration_ms, status, error, task_id "
        "FROM speedtests ORDER BY ts_ms DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return {"items": [dict(r) for r in rows]}
