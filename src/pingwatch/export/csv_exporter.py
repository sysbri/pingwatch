"""CSV exporters.

Each "table" produces ONE CSV blob (header + rows).
"""

from __future__ import annotations

import csv
import io

import aiosqlite


def _writer() -> tuple[io.StringIO, csv._writer]:
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
