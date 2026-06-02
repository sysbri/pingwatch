"""Read/write helpers for the HTTP layer.

We could call `pingwatch.db.queries` directly, but the parallel agent's module
exposes domain-typed signatures (``Destination`` dataclasses, ``**fields``
update style, etc) that don't quite match what the HTTP layer needs (plain
dicts, ``X-Total-Count`` headers, etc). Instead of coupling tightly to that
module, this shim issues the raw SQL the routes need against the same schema.

The schema is the contract — both layers agree on it via ``schema.sql``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------- Destinations ----------

async def list_destinations(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT id, name, address, type, kind, interval_ms, timeout_ms, port, "
        "enabled, ordering, resolved_ip FROM destinations ORDER BY ordering"
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_destination(
    conn: aiosqlite.Connection, dest_id: int
) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, name, address, type, kind, interval_ms, timeout_ms, port, "
        "enabled, ordering, resolved_ip FROM destinations WHERE id = ?",
        (dest_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def create_destination(conn: aiosqlite.Connection, data: dict[str, Any]) -> int:
    cur = await conn.execute(
        "INSERT INTO destinations(name, address, type, kind, interval_ms, "
        "timeout_ms, port, enabled, ordering) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            data["name"],
            data["address"],
            data["type"],
            data["kind"],
            data["interval_ms"],
            data["timeout_ms"],
            data.get("port"),
            1 if data["enabled"] else 0,
            data["ordering"],
        ),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def update_destination(
    conn: aiosqlite.Connection, dest_id: int, patch: dict[str, Any]
) -> bool:
    if not patch:
        return True
    cols = []
    vals: list[Any] = []
    for k, v in patch.items():
        cols.append(f"{k} = ?")
        if isinstance(v, bool):
            v = 1 if v else 0
        vals.append(v)
    vals.append(dest_id)
    await conn.execute(
        f"UPDATE destinations SET {', '.join(cols)} WHERE id = ?",  # noqa: S608
        vals,
    )
    await conn.commit()
    return True


async def delete_destination(conn: aiosqlite.Connection, dest_id: int) -> bool:
    await conn.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
    await conn.commit()
    return True


async def reset_destination_data(conn: aiosqlite.Connection, dest_id: int) -> None:
    """Loescht alle historischen Daten eines Ziels (Pings, Aggregates,
    Outages, Traceroutes). Wird bei Adress-Wechsel ausgefuehrt, damit der
    User einen sauberen Start hat. Die Destination-Row selbst bleibt erhalten.
    """
    await conn.execute("DELETE FROM raw_pings WHERE dest_id = ?", (dest_id,))
    await conn.execute("DELETE FROM hourly_aggregates WHERE dest_id = ?", (dest_id,))
    await conn.execute("DELETE FROM daily_aggregates WHERE dest_id = ?", (dest_id,))
    await conn.execute(
        "DELETE FROM trace_hops WHERE trace_id IN "
        "(SELECT id FROM traceroutes WHERE dest_id = ?)",
        (dest_id,),
    )
    await conn.execute("DELETE FROM traceroutes WHERE dest_id = ?", (dest_id,))
    await conn.execute("DELETE FROM route_signatures WHERE dest_id = ?", (dest_id,))
    await conn.execute(
        "DELETE FROM outage_members WHERE outage_id IN "
        "(SELECT id FROM outages WHERE dest_id_primary = ?)",
        (dest_id,),
    )
    await conn.execute("DELETE FROM outages WHERE dest_id_primary = ?", (dest_id,))
    await conn.commit()


async def reorder_destinations(
    conn: aiosqlite.Connection, order: list[int]
) -> None:
    # Two-phase to avoid colliding with UNIQUE(ordering).
    for idx, did in enumerate(order):
        await conn.execute(
            "UPDATE destinations SET ordering = ? WHERE id = ?",
            (10_000 + idx, did),
        )
    for idx, did in enumerate(order):
        await conn.execute(
            "UPDATE destinations SET ordering = ? WHERE id = ?",
            (idx, did),
        )
    await conn.commit()


# ---------- Settings ----------

def _coerce(v: str, t: str) -> Any:
    if t == "int":
        try:
            return int(v)
        except ValueError:
            return 0
    if t == "float":
        try:
            return float(v)
        except ValueError:
            return 0.0
    if t == "bool":
        return v.lower() in ("1", "true", "yes", "on")
    if t == "json":
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return None
    return v


def _detect_type(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (dict, list)):
        return "json"
    return "string"


def _stringify(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


async def get_all_settings(conn: aiosqlite.Connection) -> dict[str, Any]:
    cur = await conn.execute("SELECT key, value, value_type FROM settings")
    return {r["key"]: _coerce(r["value"], r["value_type"]) for r in await cur.fetchall()}


async def set_settings(conn: aiosqlite.Connection, items: dict[str, Any]) -> None:
    now = _now_ms()
    for k, v in items.items():
        await conn.execute(
            "INSERT INTO settings(key, value, value_type, updated_at_ts_ms) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "value_type=excluded.value_type, updated_at_ts_ms=excluded.updated_at_ts_ms",
            (k, _stringify(v), _detect_type(v), now),
        )
    await conn.commit()


# ---------- Outages ----------

async def list_outages(
    conn: aiosqlite.Connection,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    types: list[str] | None = None,
    dest_id: int | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["1=1"]
    args: list[Any] = []
    if since_ms is not None:
        where.append("o.start_ts_ms >= ?")
        args.append(since_ms)
    if until_ms is not None:
        where.append("o.start_ts_ms <= ?")
        args.append(until_ms)
    if types:
        placeholders = ",".join("?" for _ in types)
        where.append(f"o.type IN ({placeholders})")  # noqa: S608
        args.extend(types)
    if dest_id is not None:
        where.append(
            "(o.dest_id_primary = ? OR EXISTS "
            "(SELECT 1 FROM outage_members m WHERE m.outage_id = o.id "
            "AND m.dest_id = ?))"
        )
        args.extend([dest_id, dest_id])
    if search:
        where.append("(d.name LIKE ? OR COALESCE(o.notes,'') LIKE ?)")
        args.extend([f"%{search}%", f"%{search}%"])
    where_clause = " AND ".join(where)
    sql = (
        "SELECT o.id, o.dest_id_primary, o.start_ts_ms, o.end_ts_ms, "  # noqa: S608  # internal constant identifier, not user input
        "o.duration_ms, o.lost_count, o.type, o.suspect_hop_no, o.trace_id, "
        "o.notes, d.name AS dest_name "
        "FROM outages o LEFT JOIN destinations d ON d.id = o.dest_id_primary "
        f"WHERE {where_clause} "
        "ORDER BY o.start_ts_ms DESC LIMIT ? OFFSET ?"
    )
    cur = await conn.execute(sql, [*args, limit, offset])
    rows = [dict(r) for r in await cur.fetchall()]
    cnt_sql = (
        "SELECT COUNT(*) AS c FROM outages o "  # noqa: S608  # internal constant identifier, not user input
        "LEFT JOIN destinations d ON d.id = o.dest_id_primary "
        f"WHERE {where_clause}"
    )
    cnt_cur = await conn.execute(cnt_sql, args)
    cnt_row = await cnt_cur.fetchone()
    total = int(cnt_row["c"]) if cnt_row else 0
    return rows, total


async def get_outage(
    conn: aiosqlite.Connection, outage_id: int
) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT o.*, d.name AS dest_name FROM outages o "
        "LEFT JOIN destinations d ON d.id = o.dest_id_primary WHERE o.id = ?",
        (outage_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    outage = dict(row)
    mcur = await conn.execute(
        "SELECT m.dest_id, m.start_ts_ms, m.end_ts_ms, m.lost_count, d.name "
        "FROM outage_members m JOIN destinations d ON d.id = m.dest_id "
        "WHERE m.outage_id = ?",
        (outage_id,),
    )
    outage["members"] = [dict(r) for r in await mcur.fetchall()]
    return outage


# ---------- Pings ----------

async def list_raw_pings(
    conn: aiosqlite.Connection,
    dest_id: int,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    statuses: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
    search: str | None = None,  # noqa: ARG001
) -> tuple[list[dict[str, Any]], int]:
    where = ["dest_id = ?"]
    args: list[Any] = [dest_id]
    if since_ms is not None:
        where.append("ts_ms >= ?")
        args.append(since_ms)
    if until_ms is not None:
        where.append("ts_ms <= ?")
        args.append(until_ms)
    if statuses:
        ss = set(statuses)
        clauses: list[str] = []
        if "ok" in ss:
            clauses.append("(success = 1 AND (flags & 1) = 0)")
        if "spike" in ss:
            clauses.append("(success = 1 AND (flags & 1) = 1)")
        if "loss" in ss:
            clauses.append("(success = 0)")
        if clauses:
            where.append("(" + " OR ".join(clauses) + ")")
    where_clause = " AND ".join(where)
    sql = (
        "SELECT id, dest_id, ts_ms, success, latency_us, ttl, sequence, "  # noqa: S608  # internal constant identifier, not user input
        "error_kind, flags FROM raw_pings "
        f"WHERE {where_clause} "
        "ORDER BY ts_ms DESC LIMIT ? OFFSET ?"
    )
    cur = await conn.execute(sql, [*args, limit, offset])
    rows = [dict(r) for r in await cur.fetchall()]
    cnt_cur = await conn.execute(
        f"SELECT COUNT(*) AS c FROM raw_pings WHERE {where_clause}",  # noqa: S608
        args,
    )
    cnt = await cnt_cur.fetchone()
    return rows, int(cnt["c"]) if cnt else 0


# ---------- Traceroutes ----------

async def list_traces(
    conn: aiosqlite.Connection,
    dest_id: int,
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    changed_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where = ["dest_id = ?"]
    args: list[Any] = [dest_id]
    if since_ms is not None:
        where.append("ts_ms >= ?")
        args.append(since_ms)
    if until_ms is not None:
        where.append("ts_ms <= ?")
        args.append(until_ms)
    if changed_only:
        where.append("route_changed = 1")
    sql = (
        "SELECT id, dest_id, ts_ms, trigger, success, hop_count, "  # noqa: S608  # internal constant identifier, not user input
        "signature_hash, route_changed FROM traceroutes "
        f"WHERE {' AND '.join(where)} ORDER BY ts_ms DESC LIMIT ?"
    )
    cur = await conn.execute(sql, [*args, limit])
    return [dict(r) for r in await cur.fetchall()]


async def get_trace(
    conn: aiosqlite.Connection, trace_id: int
) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT * FROM traceroutes WHERE id = ?", (trace_id,))
    row = await cur.fetchone()
    if not row:
        return None
    snap = dict(row)
    hcur = await conn.execute(
        "SELECT hop_no, host, ip, rtt_us, rtt_min_us, rtt_max_us, loss_pct, "
        "is_suspect FROM trace_hops WHERE trace_id = ? ORDER BY hop_no",
        (trace_id,),
    )
    snap["hops"] = [dict(r) for r in await hcur.fetchall()]
    return snap


# ---------- Aggregates / Dashboard ----------

async def hourly_aggregates_for(
    conn: aiosqlite.Connection, dest_id: int, since_ms: int, until_ms: int
) -> list[dict[str, Any]]:
    hour_from = since_ms // 3_600_000
    hour_to = until_ms // 3_600_000
    cur = await conn.execute(
        "SELECT * FROM hourly_aggregates WHERE dest_id = ? "
        "AND hour_bucket BETWEEN ? AND ? ORDER BY hour_bucket",
        (dest_id, hour_from, hour_to),
    )
    return [dict(r) for r in await cur.fetchall()]


async def latest_traces_per_dest(
    conn: aiosqlite.Connection,
) -> dict[int, dict[str, Any]]:
    cur = await conn.execute(
        "SELECT t.* FROM traceroutes t INNER JOIN ("
        "  SELECT dest_id, MAX(ts_ms) AS mts FROM traceroutes GROUP BY dest_id"
        ") l ON l.dest_id = t.dest_id AND l.mts = t.ts_ms"
    )
    out: dict[int, dict[str, Any]] = {}
    for r in await cur.fetchall():
        d = dict(r)
        hcur = await conn.execute(
            "SELECT hop_no, host, ip, rtt_us, is_suspect FROM trace_hops "
            "WHERE trace_id = ? ORDER BY hop_no",
            (d["id"],),
        )
        d["hops"] = [dict(h) for h in await hcur.fetchall()]
        out[d["dest_id"]] = d
    return out


async def recent_events(
    conn: aiosqlite.Connection, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Combined event log: outages, wifi events, stream events — newest first."""
    o_cur = await conn.execute(
        "SELECT o.id, o.start_ts_ms AS ts_ms, o.type AS event_type, "
        "o.duration_ms, d.name AS dest_name, o.lost_count "
        "FROM outages o LEFT JOIN destinations d ON d.id = o.dest_id_primary "
        "ORDER BY o.start_ts_ms DESC LIMIT ?",
        (limit,),
    )
    outages = [{"source": "outage", **dict(r)} for r in await o_cur.fetchall()]
    w_cur = await conn.execute(
        "SELECT id, ts_ms, event_type, duration_ms, ssid, rssi "
        "FROM wifi_events ORDER BY ts_ms DESC LIMIT ?",
        (limit,),
    )
    wifi = [{"source": "wifi", **dict(r)} for r in await w_cur.fetchall()]
    s_cur = await conn.execute(
        "SELECT id, ts_ms, event_type, duration_ms, kbps_before, kbps_after "
        "FROM stream_events WHERE event_type != 'connect' "
        "ORDER BY ts_ms DESC LIMIT ?",
        (limit,),
    )
    stream = [{"source": "stream", **dict(r)} for r in await s_cur.fetchall()]
    merged = [*outages, *wifi, *stream]
    merged.sort(key=lambda e: e.get("ts_ms") or 0, reverse=True)
    return merged[:limit]


async def latency_sparkline(
    conn: aiosqlite.Connection, dest_id: int, *, points: int = 60
) -> list[int]:
    cur = await conn.execute(
        "SELECT latency_us, success FROM raw_pings WHERE dest_id = ? "
        "ORDER BY ts_ms DESC LIMIT ?",
        (dest_id, points),
    )
    rows = await cur.fetchall()
    return [int(r["latency_us"] or 0) if r["success"] else 0 for r in reversed(rows)]


async def outages_today_for_dest(
    conn: aiosqlite.Connection, dest_id: int, *, since_ms: int
) -> list[dict[str, Any]]:
    now_ms = _now_ms()
    cur = await conn.execute(
        "SELECT id, start_ts_ms, end_ts_ms, "
        "COALESCE(duration_ms, ? - start_ts_ms) AS duration_ms, type "
        "FROM outages WHERE dest_id_primary = ? AND start_ts_ms >= ? "
        "ORDER BY start_ts_ms DESC",
        (now_ms, dest_id, since_ms),
    )
    return [dict(r) for r in await cur.fetchall()]


async def dest_kpis(
    conn: aiosqlite.Connection, dest_id: int, *, since_ms: int
) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok_count, "
        "AVG(CASE WHEN success = 1 THEN latency_us END) AS mean_us, "
        "MIN(CASE WHEN success = 1 THEN latency_us END) AS min_us, "
        "MAX(CASE WHEN success = 1 THEN latency_us END) AS max_us "
        "FROM raw_pings WHERE dest_id = ? AND ts_ms >= ?",
        (dest_id, since_ms),
    )
    row = await cur.fetchone()
    total = int(row["total"] or 0) if row else 0
    ok_count = int(row["ok_count"] or 0) if row else 0
    return {
        "total": total,
        "ok": ok_count,
        "loss_pct": ((total - ok_count) / total * 100.0) if total else 0.0,
        "mean_us": int(row["mean_us"] or 0) if row else 0,
        "min_us": int(row["min_us"] or 0) if row else 0,
        "max_us": int(row["max_us"] or 0) if row else 0,
    }


async def stream_summary(
    conn: aiosqlite.Connection, *, since_ms: int
) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT kbps FROM stream_throughput_samples ORDER BY ts_ms DESC LIMIT 1"
    )
    row = await cur.fetchone()
    kbps_now = int(row["kbps"]) if row else 0
    dcur = await conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(MAX(duration_ms),0) AS longest "
        "FROM stream_events WHERE ts_ms >= ? AND event_type = 'drop'",
        (since_ms,),
    )
    drow = await dcur.fetchone()
    rcur = await conn.execute(
        "SELECT COUNT(*) AS c FROM stream_events "
        "WHERE ts_ms >= ? AND event_type IN ('connect','reconnect')",
        (since_ms,),
    )
    rrow = await rcur.fetchone()
    # bytes_today: kbps ist Delta pro 1s-Sample. SUM(kbps) ist Tagesbytes/1024 in KB.
    # bytes = SUM(kbps) * 1024 (reset-sicher, unabhaengig von Worker-Restarts).
    bcur = await conn.execute(
        "SELECT COALESCE(SUM(kbps),0) * 1024 AS tot "
        "FROM stream_throughput_samples WHERE ts_ms >= ?",
        (since_ms,),
    )
    brow = await bcur.fetchone()
    # Time-series fuer Dashboard-Stream-Chart: letzte 5 min, ~300 samples
    now_ms = int(time.time() * 1000)
    five_min_ago = max(since_ms, now_ms - 5 * 60_000)
    scur = await conn.execute(
        "SELECT ts_ms, kbps FROM stream_throughput_samples "
        "WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (five_min_ago,),
    )
    series = [dict(r) for r in await scur.fetchall()]
    # Drops im selben Zeitfenster fuer Chart-Marker
    ddcur = await conn.execute(
        "SELECT ts_ms, duration_ms FROM stream_events "
        "WHERE event_type = 'drop' AND ts_ms >= ? ORDER BY ts_ms ASC",
        (five_min_ago,),
    )
    drops = [dict(r) for r in await ddcur.fetchall()]
    return {
        "kbps_now": kbps_now,
        "drops_count": int(drow["c"] or 0) if drow else 0,
        "longest_drop_ms": int(drow["longest"] or 0) if drow else 0,
        "reconnects": int(rrow["c"] or 0) if rrow else 0,
        "bytes_today": int(brow["tot"] or 0) if brow else 0,
        "series": series,
        "drops": drops,
    }


async def wifi_current(conn: aiosqlite.Connection) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT ts_ms, rssi, channel, link_rate_kbps FROM wifi_rssi_samples "
        "ORDER BY ts_ms DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row:
        return {"associated": False, "ssid": None, "rssi": None, "channel": None}
    ecur = await conn.execute(
        "SELECT ssid, bssid FROM wifi_events "
        "WHERE event_type IN ('connect','reassoc') AND ssid IS NOT NULL "
        "ORDER BY ts_ms DESC LIMIT 1"
    )
    erow = await ecur.fetchone()
    return {
        "associated": True,
        "ssid": erow["ssid"] if erow else None,
        "bssid": erow["bssid"] if erow else None,
        "rssi": int(row["rssi"]),
        "channel": int(row["channel"]) if row["channel"] is not None else None,
        "link_rate_kbps": int(row["link_rate_kbps"] or 0),
    }


async def hero_stats(
    conn: aiosqlite.Connection, *, since_ms: int
) -> dict[str, Any]:
    total_cur = await conn.execute(
        "SELECT COUNT(*) AS c FROM raw_pings WHERE ts_ms >= ?", (since_ms,)
    )
    ttl_row = await total_cur.fetchone()
    now_ms = _now_ms()
    out_cur = await conn.execute(
        "SELECT COUNT(*) AS c, "
        "COALESCE(SUM(COALESCE(duration_ms, ? - start_ts_ms)), 0) AS tot "
        "FROM outages WHERE start_ts_ms >= ?",
        (now_ms, since_ms),
    )
    out_row = await out_cur.fetchone()
    window_ms = _now_ms() - since_ms
    outage_ms = int(out_row["tot"] or 0) if out_row else 0
    uptime_pct = max(0.0, min(100.0, 100.0 * (1 - outage_ms / max(window_ms, 1))))
    return {
        "uptime_pct": round(uptime_pct, 3),
        "outages_24h": int(out_row["c"] or 0) if out_row else 0,
        "total_outage_ms": outage_ms,
        "total_pings": int(ttl_row["c"] or 0) if ttl_row else 0,
    }


async def db_size_bytes(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute(
        "SELECT page_count * page_size AS sz "
        "FROM pragma_page_count(), pragma_page_size()"
    )
    row = await cur.fetchone()
    return int(row["sz"]) if row else 0


async def purge_all_metrics(conn: aiosqlite.Connection) -> None:
    """Clear-data: keep destinations + settings, nuke everything else."""
    for tbl in (
        "raw_pings",
        "hourly_aggregates",
        "daily_aggregates",
        "outage_members",
        "outages",
        "trace_hops",
        "traceroutes",
        "route_signatures",
        "wifi_events",
        "wifi_rssi_samples",
        "stream_events",
        "stream_throughput_samples",
        "heartbeat_events",
        "notifications",
    ):
        await conn.execute(f"DELETE FROM {tbl}")  # noqa: S608
    await conn.commit()


async def factory_reset(conn: aiosqlite.Connection) -> None:
    await purge_all_metrics(conn)
    await conn.execute("DELETE FROM destinations")
    await conn.commit()
