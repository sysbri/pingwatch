"""Raw ping log + detail-tab Übersicht payload."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query, Response

from pingwatch.api.deps import RANGE_TO_MS, ConnDep
from pingwatch.db import queries as q

router = APIRouter(tags=["pings"])


@router.get("/api/destinations/{dest_id}/pings")
async def list_pings(
    response: Response,
    dest_id: int,
    conn: ConnDep,
    since: int | None = Query(default=None),
    until: int | None = Query(default=None),
    statuses: str | None = Query(default=None, description="comma-separated ok,spike,loss"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    statuses_list = (
        [s.strip() for s in statuses.split(",") if s.strip()] if statuses else None
    )
    rows, total = await q.list_raw_pings(
        conn,
        dest_id,
        since_ms=since,
        until_ms=until,
        statuses=statuses_list,
        limit=limit,
        offset=offset,
        search=search,
    )
    response.headers["X-Total-Count"] = str(total)
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/api/destinations/{dest_id}/overview")
async def overview(
    dest_id: int,
    conn: ConnDep,
    range_: str = Query(default="24h", alias="range"),
) -> dict[str, Any]:
    window_ms = RANGE_TO_MS.get(range_, 86_400_000)
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_ms

    kpi = await q.dest_kpis(conn, dest_id, since_ms=since_ms)

    pcur = await conn.execute(
        "SELECT latency_us FROM raw_pings WHERE dest_id = ? AND success = 1 "
        "AND ts_ms >= ? ORDER BY latency_us",
        (dest_id, since_ms),
    )
    lats = [int(r["latency_us"]) for r in await pcur.fetchall() if r["latency_us"]]

    def pct(p: float) -> int:
        if not lats:
            return 0
        idx = min(len(lats) - 1, max(0, int(round((p / 100.0) * (len(lats) - 1)))))
        return lats[idx]

    percentiles = {
        "p50": pct(50),
        "p75": pct(75),
        "p90": pct(90),
        "p95": pct(95),
        "p99": pct(99),
    }

    # Histogram: 20 fixed buckets up to 250 ms; tail bucket for >250 ms.
    bin_count = 20
    bin_width_ms = 12.5
    bins = [0] * (bin_count + 1)
    for lat in lats:
        lat_ms = lat / 1000.0
        if lat_ms >= bin_count * bin_width_ms:
            bins[-1] += 1
        else:
            bins[min(bin_count - 1, int(lat_ms / bin_width_ms))] += 1
    edges_ms: list[float | None] = [
        round(i * bin_width_ms, 1) for i in range(bin_count + 1)
    ]
    edges_ms.append(None)

    # Time-series fuer Latenz-Chart: alle Samples im Fenster (bis ~1500
    # Punkte, danach downsampled per LTTB-aehnlichem Bucket-Average).
    scur = await conn.execute(
        "SELECT ts_ms, latency_us, success, flags FROM raw_pings "
        "WHERE dest_id = ? AND ts_ms >= ? ORDER BY ts_ms",
        (dest_id, since_ms),
    )
    raw_series = [dict(r) for r in await scur.fetchall()]
    max_points = 1500
    if len(raw_series) > max_points:
        bucket_size = len(raw_series) // max_points + 1
        downsampled = []
        for i in range(0, len(raw_series), bucket_size):
            bucket = raw_series[i:i + bucket_size]
            ok_lats = [b["latency_us"] for b in bucket if b["success"] and b["latency_us"]]
            downsampled.append({
                "ts_ms": bucket[len(bucket) // 2]["ts_ms"],
                "latency_us": sum(ok_lats) // len(ok_lats) if ok_lats else None,
                "success": 1 if ok_lats else 0,
                "flags": max((b["flags"] or 0) for b in bucket),
            })
        series = downsampled
    else:
        series = raw_series

    # Spike-Marker: Pings mit SPIKE-Flag gesetzt (bit 0)
    spike_markers = [
        {"ts_ms": r["ts_ms"], "latency_us": r["latency_us"]}
        for r in raw_series if (r.get("flags") or 0) & 1
    ]

    # Outage-Marker im Fenster
    ocur = await conn.execute(
        "SELECT id, start_ts_ms, end_ts_ms, duration_ms, type FROM outages "
        "WHERE dest_id_primary = ? AND start_ts_ms >= ? ORDER BY start_ts_ms",
        (dest_id, since_ms),
    )
    outage_markers = [dict(r) for r in await ocur.fetchall()]

    return {
        "range": range_,
        "since_ms": since_ms,
        "now_ms": now_ms,
        "kpi": kpi,
        "percentiles": percentiles,
        "histogram": {
            "edges_ms": edges_ms,
            "counts": bins,
        },
        "series": series,
        "spike_markers": spike_markers,
        "outage_markers": outage_markers,
    }
