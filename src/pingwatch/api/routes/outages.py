"""Outage list + detail endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from pingwatch.api.deps import ConnDep
from pingwatch.db import queries as q

router = APIRouter(prefix="/api/outages", tags=["outages"])


@router.get("")
async def list_outages(
    response: Response,
    conn: ConnDep,
    since: int | None = Query(default=None, description="epoch ms"),
    until: int | None = Query(default=None),
    types: str | None = Query(default=None, description="comma-separated"),
    dest_id: int | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    types_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    rows, total = await q.list_outages(
        conn,
        since_ms=since,
        until_ms=until,
        types=types_list,
        dest_id=dest_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/{outage_id}")
async def get_outage(outage_id: int, conn: ConnDep) -> dict[str, Any]:
    outage = await q.get_outage(conn, outage_id)
    if not outage:
        raise HTTPException(status_code=404, detail="outage not found")

    # Latency context ±30 s for each affected dest.
    start = int(outage["start_ts_ms"])
    end = int(outage.get("end_ts_ms") or start)
    win_start = start - 30_000
    win_end = end + 30_000

    contexts: dict[int, list[dict[str, Any]]] = {}
    member_ids = [m["dest_id"] for m in outage.get("members") or []]
    if not member_ids and outage.get("dest_id_primary"):
        member_ids = [outage["dest_id_primary"]]
    for did in member_ids:
        rows, _ = await q.list_raw_pings(
            conn, did, since_ms=win_start, until_ms=win_end, limit=1000
        )
        contexts[did] = list(reversed(rows))

    # Correlated wifi events
    wcur = await conn.execute(
        "SELECT id, ts_ms, event_type, duration_ms, ssid, rssi "
        "FROM wifi_events WHERE ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (win_start, win_end),
    )
    wifi_events = [dict(r) for r in await wcur.fetchall()]

    scur = await conn.execute(
        "SELECT id, ts_ms, event_type, duration_ms, kbps_before, kbps_after "
        "FROM stream_events WHERE ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (win_start, win_end),
    )
    stream_events = [dict(r) for r in await scur.fetchall()]

    trace_snapshot: dict[str, Any] | None = None
    if outage.get("trace_id"):
        trace_snapshot = await q.get_trace(conn, int(outage["trace_id"]))

    return {
        "outage": outage,
        "latency_context": contexts,
        "wifi_events": wifi_events,
        "stream_events": stream_events,
        "trace": trace_snapshot,
    }
