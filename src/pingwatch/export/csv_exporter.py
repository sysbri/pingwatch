"""CSV exporters.

Choice: each "table" produces ONE CSV blob (header + rows). The destinations
export concatenates per-destination metadata into a single CSV; per-destination
ping data is exposed via `export_pings_csv` which is streaming-only (rows can
be hundreds of thousands).
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator

import aiosqlite


def _writer() -> tuple[io.StringIO, "csv._writer"]:
    buf = io.StringIO()
    return buf, csv.writer(buf, lineterminator="\n")


async def export_destinations_csv(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> bytes:
    buf, w = _writer()
    w.writerow(
        [
            "id",
            "name",
            "address",
            "type",
            "kind",
            "interval_ms",
            "timeout_ms",
            "port",
            "enabled",
            "ordering",
            "resolved_ip",
        ]
    )
    cur = await conn.execute(
        "SELECT id,name,address,type,kind,interval_ms,timeout_ms,port,"
        "enabled,ordering,resolved_ip FROM destinations ORDER BY ordering"
    )
    async for row in cur:
        w.writerow(list(row))
    return buf.getvalue().encode("utf-8")


async def export_pings_csv(
    conn: aiosqlite.Connection, dest_id: int, since_ms: int, until_ms: int
) -> AsyncIterator[bytes]:
    """Streaming export — yields chunks to avoid loading multi-million-row results."""
    header_buf, header_w = _writer()
    header_w.writerow(
        ["id", "dest_id", "ts_ms", "success", "latency_us", "ttl", "sequence", "error_kind", "flags"]
    )
    yield header_buf.getvalue().encode("utf-8")

    cur = await conn.execute(
        "SELECT id,dest_id,ts_ms,success,latency_us,ttl,sequence,error_kind,flags "
        "FROM raw_pings WHERE dest_id=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (dest_id, since_ms, until_ms),
    )
    batch_buf, batch_w = _writer()
    count = 0
    async for row in cur:
        batch_w.writerow(list(row))
        count += 1
        if count >= 1000:
            yield batch_buf.getvalue().encode("utf-8")
            batch_buf, batch_w = _writer()
            count = 0
    if count:
        yield batch_buf.getvalue().encode("utf-8")


async def export_outages_csv(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> bytes:
    buf, w = _writer()
    w.writerow(
        [
            "id",
            "dest_id_primary",
            "start_ts_ms",
            "end_ts_ms",
            "duration_ms",
            "lost_count",
            "type",
            "suspect_hop_no",
            "trace_id",
            "notes",
        ]
    )
    cur = await conn.execute(
        "SELECT id,dest_id_primary,start_ts_ms,end_ts_ms,duration_ms,lost_count,"
        "type,suspect_hop_no,trace_id,notes FROM outages "
        "WHERE start_ts_ms BETWEEN ? AND ? ORDER BY start_ts_ms",
        (since_ms, until_ms),
    )
    async for row in cur:
        w.writerow(list(row))
    return buf.getvalue().encode("utf-8")


async def export_traces_csv(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> bytes:
    """Combined traces + hops in a single CSV with a 'kind' discriminator column."""
    buf, w = _writer()
    w.writerow(
        [
            "kind",
            "trace_id",
            "dest_id",
            "ts_ms",
            "trigger",
            "hop_no",
            "host",
            "ip",
            "rtt_us",
            "loss_pct",
            "is_suspect",
            "route_changed",
        ]
    )
    cur = await conn.execute(
        "SELECT id, dest_id, ts_ms, trigger, route_changed FROM traceroutes "
        "WHERE ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (since_ms, until_ms),
    )
    traces = await cur.fetchall()
    for tr in traces:
        trace_id, dest_id, ts_ms, trigger, route_changed = tr
        w.writerow(
            ["trace", trace_id, dest_id, ts_ms, trigger, "", "", "", "", "", "", route_changed]
        )
        hops_cur = await conn.execute(
            "SELECT hop_no, host, ip, rtt_us, loss_pct, is_suspect FROM trace_hops "
            "WHERE trace_id=? ORDER BY hop_no",
            (trace_id,),
        )
        async for h in hops_cur:
            hop_no, host, ip, rtt_us, loss_pct, is_suspect = h
            w.writerow(
                [
                    "hop",
                    trace_id,
                    dest_id,
                    ts_ms,
                    trigger,
                    hop_no,
                    host or "",
                    ip or "",
                    rtt_us if rtt_us is not None else "",
                    loss_pct if loss_pct is not None else "",
                    is_suspect,
                    "",
                ]
            )
    return buf.getvalue().encode("utf-8")
