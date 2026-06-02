"""Traceroute endpoints: list, snapshot detail, hop-trend sparklines."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pingwatch.api.deps import ConnDep
from pingwatch.db import queries as q

router = APIRouter(tags=["traces"])


@router.get("/api/destinations/{dest_id}/traces")
async def list_traces(
    dest_id: int,
    conn: ConnDep,
    since: int | None = Query(default=None),
    until: int | None = Query(default=None),
    changed_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    rows = await q.list_traces(
        conn,
        dest_id,
        since_ms=since,
        until_ms=until,
        changed_only=changed_only,
        limit=limit,
    )
    return {"items": rows, "count": len(rows)}


@router.get("/api/traces/{trace_id}")
async def get_trace(trace_id: int, conn: ConnDep) -> dict[str, Any]:
    snap = await q.get_trace(conn, trace_id)
    if not snap:
        raise HTTPException(status_code=404, detail="trace not found")
    return snap


@router.get("/api/destinations/{dest_id}/trace/latest")
async def latest_trace(dest_id: int, conn: ConnDep) -> dict[str, Any]:
    """Letzter Traceroute-Snapshot fuer Detail-View 'Aktuelle Route'.

    Liefert auch teilweise gescheiterte Traces (success=0) — die ersten N
    Hops sind oft trotzdem auswertbar, z.B. wenn das Ziel selbst ICMP
    droppt aber die Route bis dahin sichtbar ist.
    """
    cur = await conn.execute(
        "SELECT id FROM traceroutes WHERE dest_id = ? "
        "ORDER BY ts_ms DESC LIMIT 1",
        (dest_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no trace yet")
    snap = await q.get_trace(conn, int(row["id"]))
    if not snap:
        raise HTTPException(status_code=404, detail="trace not found")
    return snap


@router.get("/api/destinations/{dest_id}/hop-trends")
async def hop_trends(
    dest_id: int,
    conn: ConnDep,
    since: int | None = Query(default=None),
) -> dict[str, Any]:
    """For each hop in the latest trace, return RTT history across last 200 traces."""
    traces_cur = await conn.execute(
        "SELECT id, ts_ms FROM traceroutes WHERE dest_id = ? "  # noqa: S608  # internal constant identifier, not user input
        + ("AND ts_ms >= ? " if since is not None else "")
        + "ORDER BY ts_ms DESC LIMIT 200",
        ((dest_id, since) if since is not None else (dest_id,)),
    )
    traces = [dict(r) for r in await traces_cur.fetchall()]
    if not traces:
        return {"latest": None, "hops": []}
    latest_id = traces[0]["id"]
    hcur = await conn.execute(
        "SELECT hop_no, host, ip, rtt_us, is_suspect FROM trace_hops "
        "WHERE trace_id = ? ORDER BY hop_no",
        (latest_id,),
    )
    latest_hops = [dict(r) for r in await hcur.fetchall()]
    trace_ids = [t["id"] for t in traces]
    placeholders = ",".join("?" for _ in trace_ids)
    sparks_cur = await conn.execute(
        f"SELECT trace_id, hop_no, rtt_us FROM trace_hops "  # noqa: S608
        f"WHERE trace_id IN ({placeholders}) ORDER BY trace_id, hop_no",
        trace_ids,
    )
    series: dict[int, list[int | None]] = {h["hop_no"]: [] for h in latest_hops}
    for r in await sparks_cur.fetchall():
        if r["hop_no"] in series:
            series[r["hop_no"]].append(r["rtt_us"])
    return {
        "latest": {"id": latest_id, "ts_ms": traces[0]["ts_ms"]},
        "hops": [
            {**h, "sparkline_rtt_us": series.get(h["hop_no"], [])}
            for h in latest_hops
        ],
        "trace_count": len(traces),
    }
