"""JSON exporters — same shapes as csv_exporter; structured nested dump."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import aiosqlite


def _rows_to_dicts(rows: list, columns: list[str]) -> list[dict]:
    return [dict(zip(columns, row, strict=True)) for row in rows]


async def export_destinations_json(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> bytes:
    cols = [
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
    cur = await conn.execute(f"SELECT {','.join(cols)} FROM destinations ORDER BY ordering")
    rows = await cur.fetchall()
    payload = {"destinations": _rows_to_dicts([list(r) for r in rows], cols)}
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


async def export_pings_json(
    conn: aiosqlite.Connection, dest_id: int, since_ms: int, until_ms: int
) -> AsyncIterator[bytes]:
    """Streaming JSON array — chunked to keep memory bounded."""
    yield b'{"dest_id": ' + str(dest_id).encode() + b', "pings": ['
    cols = ["id", "dest_id", "ts_ms", "success", "latency_us", "ttl", "sequence", "error_kind", "flags"]
    cur = await conn.execute(
        f"SELECT {','.join(cols)} FROM raw_pings "
        "WHERE dest_id=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (dest_id, since_ms, until_ms),
    )
    first = True
    async for row in cur:
        prefix = b"" if first else b","
        first = False
        obj = dict(zip(cols, list(row), strict=True))
        yield prefix + json.dumps(obj, ensure_ascii=False).encode("utf-8")
    yield b"]}"


async def export_outages_json(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> bytes:
    cols = [
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
    cur = await conn.execute(
        f"SELECT {','.join(cols)} FROM outages "
        "WHERE start_ts_ms BETWEEN ? AND ? ORDER BY start_ts_ms",
        (since_ms, until_ms),
    )
    rows = await cur.fetchall()
    outages = _rows_to_dicts([list(r) for r in rows], cols)
    for o in outages:
        mcur = await conn.execute(
            "SELECT dest_id, start_ts_ms, end_ts_ms, lost_count FROM outage_members WHERE outage_id=?",
            (o["id"],),
        )
        o["members"] = [
            {"dest_id": m[0], "start_ts_ms": m[1], "end_ts_ms": m[2], "lost_count": m[3]}
            for m in await mcur.fetchall()
        ]
    return json.dumps({"outages": outages}, indent=2, ensure_ascii=False).encode("utf-8")


async def export_traces_json(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> bytes:
    cur = await conn.execute(
        "SELECT id, dest_id, ts_ms, trigger, success, hop_count, signature_hash, route_changed "
        "FROM traceroutes WHERE ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (since_ms, until_ms),
    )
    traces = []
    async for row in cur:
        trace_id, dest_id, ts_ms, trigger, success, hop_count, sig_hash, route_changed = row
        hops_cur = await conn.execute(
            "SELECT hop_no, host, ip, rtt_us, rtt_min_us, rtt_max_us, loss_pct, is_suspect "
            "FROM trace_hops WHERE trace_id=? ORDER BY hop_no",
            (trace_id,),
        )
        hops = [
            {
                "hop_no": h[0],
                "host": h[1],
                "ip": h[2],
                "rtt_us": h[3],
                "rtt_min_us": h[4],
                "rtt_max_us": h[5],
                "loss_pct": h[6],
                "is_suspect": bool(h[7]),
            }
            for h in await hops_cur.fetchall()
        ]
        traces.append(
            {
                "id": trace_id,
                "dest_id": dest_id,
                "ts_ms": ts_ms,
                "trigger": trigger,
                "success": bool(success),
                "hop_count": hop_count,
                "signature_hash": sig_hash,
                "route_changed": bool(route_changed),
                "hops": hops,
            }
        )
    return json.dumps({"traces": traces}, indent=2, ensure_ascii=False).encode("utf-8")


async def export_all_json(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> bytes:
    """Single structured dump containing all tables under top-level keys."""
    dests = await export_destinations_json(conn, since_ms, until_ms)
    outages = await export_outages_json(conn, since_ms, until_ms)
    traces = await export_traces_json(conn, since_ms, until_ms)
    payload = {
        "range": {"since_ms": since_ms, "until_ms": until_ms},
        "destinations": json.loads(dests)["destinations"],
        "outages": json.loads(outages)["outages"],
        "traces": json.loads(traces)["traces"],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
