from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any, Literal, TypeVar, cast

import aiosqlite

from ..models import (
    DestKind,
    Destination,
    HeartbeatEvent,
    OutageType,
    PingSample,
    ProbeType,
    StreamEvent,
    TraceHop,
    WifiEvent,
)

T = TypeVar("T")

_PingStatus = Literal["ok", "spike", "loss"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_destination(row: aiosqlite.Row) -> Destination:
    return Destination(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        type=ProbeType(row["type"]),
        kind=DestKind(row["kind"]),
        interval_ms=row["interval_ms"],
        timeout_ms=row["timeout_ms"],
        port=row["port"],
        enabled=bool(row["enabled"]),
        ordering=row["ordering"],
        resolved_ip=row["resolved_ip"],
    )


# ===== Destinations =====


async def list_destinations(
    conn: aiosqlite.Connection, enabled_only: bool = True
) -> list[Destination]:
    sql = (
        "SELECT id, name, address, type, kind, interval_ms, timeout_ms, port, "
        "enabled, ordering, resolved_ip FROM destinations"
    )
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY ordering ASC, id ASC"
    cur = await conn.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_destination(r) for r in rows]


async def get_destination(conn: aiosqlite.Connection, dest_id: int) -> Destination | None:
    cur = await conn.execute(
        "SELECT id, name, address, type, kind, interval_ms, timeout_ms, port, "
        "enabled, ordering, resolved_ip FROM destinations WHERE id = ?",
        (dest_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return _row_to_destination(row) if row else None


async def insert_destination(
    conn: aiosqlite.Connection,
    *,
    name: str,
    address: str,
    type_: ProbeType | str,
    kind: DestKind | str,
    interval_ms: int = 1000,
    timeout_ms: int = 2000,
    port: int | None = None,
    ordering: int = 0,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO destinations(name, address, type, kind, interval_ms,
                                 timeout_ms, port, ordering)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, address, str(type_), str(kind), interval_ms, timeout_ms, port, ordering),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


_DEST_UPDATABLE = frozenset(
    {
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
        "resolved_at_ts_ms",
    }
)


async def update_destination(conn: aiosqlite.Connection, dest_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in _DEST_UPDATABLE:
            raise ValueError(f"unknown destination field: {k}")
        cols.append(f"{k} = ?")
        if isinstance(v, bool):
            vals.append(1 if v else 0)
        elif hasattr(v, "value"):
            vals.append(v.value)
        else:
            vals.append(v)
    vals.append(dest_id)
    await conn.execute(
        f"UPDATE destinations SET {', '.join(cols)} WHERE id = ?",
        tuple(vals),
    )
    await conn.commit()


async def delete_destination(conn: aiosqlite.Connection, dest_id: int) -> None:
    await conn.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
    await conn.commit()


async def reorder_destinations(conn: aiosqlite.Connection, id_order: list[int]) -> None:
    # Two-phase write to dodge UNIQUE(ordering) collisions.
    offset = 1_000_000
    await conn.execute("BEGIN")
    try:
        for idx, dest_id in enumerate(id_order):
            await conn.execute(
                "UPDATE destinations SET ordering = ? WHERE id = ?",
                (offset + idx, dest_id),
            )
        for idx, dest_id in enumerate(id_order):
            await conn.execute(
                "UPDATE destinations SET ordering = ? WHERE id = ?",
                (idx, dest_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


# ===== Settings =====


async def get_setting(conn: aiosqlite.Connection, key: str) -> str | None:
    cur = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    return row["value"] if row else None


def _cast_setting(value: str, value_type: str) -> Any:
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        return value.lower() in ("1", "true", "yes", "on")
    if value_type == "json":
        return json.loads(value)
    return value


async def get_setting_typed(conn: aiosqlite.Connection, key: str, default: T) -> T:
    cur = await conn.execute("SELECT value, value_type FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return default
    try:
        return cast(T, _cast_setting(row["value"], row["value_type"]))
    except (ValueError, json.JSONDecodeError):
        return default


def _infer_value_type(value: Any) -> tuple[str, str]:
    if isinstance(value, bool):
        return ("true" if value else "false"), "bool"
    if isinstance(value, int):
        return str(value), "int"
    if isinstance(value, float):
        return repr(value), "float"
    if isinstance(value, (dict, list)):
        return json.dumps(value), "json"
    return str(value), "string"


async def set_setting(conn: aiosqlite.Connection, key: str, value: Any) -> None:
    str_val, vtype = _infer_value_type(value)
    # Preserve declared value_type if a row already exists.
    cur = await conn.execute("SELECT value_type FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    if row:
        vtype = row["value_type"]
        if vtype == "bool" and isinstance(value, bool):
            str_val = "true" if value else "false"
        elif vtype == "json":
            str_val = json.dumps(value) if not isinstance(value, str) else value
        else:
            str_val = str(value)
    await conn.execute(
        """
        INSERT INTO settings(key, value, value_type, updated_at_ts_ms)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at_ts_ms = excluded.updated_at_ts_ms
        """,
        (key, str_val, vtype, _now_ms()),
    )
    await conn.commit()


async def list_settings(conn: aiosqlite.Connection) -> dict[str, str]:
    cur = await conn.execute("SELECT key, value FROM settings ORDER BY key")
    rows = await cur.fetchall()
    await cur.close()
    return {r["key"]: r["value"] for r in rows}


# ===== Raw pings =====


async def insert_raw_pings(
    conn: aiosqlite.Connection, samples: Iterable[PingSample]
) -> None:
    rows = [
        (
            s.dest_id,
            s.ts_ms,
            1 if s.success else 0,
            s.latency_us,
            s.ttl,
            s.sequence,
            s.error_kind,
            getattr(s, "flags", 0) if hasattr(s, "flags") else 0,
        )
        for s in samples
    ]
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO raw_pings(dest_id, ts_ms, success, latency_us, ttl,
                              sequence, error_kind, flags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    await conn.commit()


async def insert_raw_pings_with_flags(
    conn: aiosqlite.Connection, rows: Iterable[tuple[int, int, int, int | None, int | None,
                                                     int | None, str | None, int]]
) -> None:
    rows_list = list(rows)
    if not rows_list:
        return
    await conn.executemany(
        """
        INSERT INTO raw_pings(dest_id, ts_ms, success, latency_us, ttl,
                              sequence, error_kind, flags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_list,
    )
    await conn.commit()


async def list_raw_pings(
    conn: aiosqlite.Connection,
    dest_id: int,
    *,
    since_ms: int,
    until_ms: int,
    limit: int,
    offset: int = 0,
    statuses: set[_PingStatus] | None = None,
) -> list[dict[str, Any]]:
    clauses = ["dest_id = ?", "ts_ms >= ?", "ts_ms < ?"]
    params: list[Any] = [dest_id, since_ms, until_ms]
    if statuses:
        status_clauses: list[str] = []
        if "loss" in statuses:
            status_clauses.append("success = 0")
        if "spike" in statuses:
            status_clauses.append("(success = 1 AND (flags & 1) = 1)")
        if "ok" in statuses:
            status_clauses.append("(success = 1 AND (flags & 1) = 0)")
        if status_clauses:
            clauses.append("(" + " OR ".join(status_clauses) + ")")
    sql = (
        "SELECT id, dest_id, ts_ms, success, latency_us, ttl, sequence, error_kind, flags "
        f"FROM raw_pings WHERE {' AND '.join(clauses)} "
        "ORDER BY ts_ms DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    cur = await conn.execute(sql, tuple(params))
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def count_raw_pings(
    conn: aiosqlite.Connection, dest_id: int, since_ms: int, until_ms: int
) -> dict[str, int]:
    cur = await conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN success = 1 AND (flags & 1) = 0 THEN 1 ELSE 0 END) AS ok,
          SUM(CASE WHEN success = 1 AND (flags & 1) = 1 THEN 1 ELSE 0 END) AS spike,
          SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS loss
        FROM raw_pings
        WHERE dest_id = ? AND ts_ms >= ? AND ts_ms < ?
        """,
        (dest_id, since_ms, until_ms),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return {"total": 0, "ok": 0, "spike": 0, "loss": 0}
    return {
        "total": row["total"] or 0,
        "ok": row["ok"] or 0,
        "spike": row["spike"] or 0,
        "loss": row["loss"] or 0,
    }


async def latest_pings(
    conn: aiosqlite.Connection, dest_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT id, dest_id, ts_ms, success, latency_us, ttl, sequence, error_kind, flags
        FROM raw_pings WHERE dest_id = ?
        ORDER BY ts_ms DESC LIMIT ?
        """,
        (dest_id, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def sparkline_pings(
    conn: aiosqlite.Connection, dest_id: int, *, since_ms: int
) -> list[tuple[int, int | None, bool]]:
    cur = await conn.execute(
        """
        SELECT ts_ms, latency_us, success FROM raw_pings
        WHERE dest_id = ? AND ts_ms >= ?
        ORDER BY ts_ms ASC
        """,
        (dest_id, since_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["latency_us"], bool(r["success"])) for r in rows]


# ===== Hourly aggregates =====


_HOURLY_COLS = (
    "dest_id",
    "hour_bucket",
    "count_total",
    "count_ok",
    "count_spike",
    "p50_us",
    "p95_us",
    "p99_us",
    "min_us",
    "max_us",
    "mean_us",
    "loss_pct",
    "jitter_us",
    "outage_count",
    "outage_ms",
    "finalized",
)


async def upsert_hourly_aggregate(conn: aiosqlite.Connection, row: dict[str, Any]) -> None:
    vals = tuple(row.get(c) for c in _HOURLY_COLS)
    placeholders = ", ".join(["?"] * len(_HOURLY_COLS))
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in _HOURLY_COLS if c not in ("dest_id", "hour_bucket")
    )
    await conn.execute(
        f"""
        INSERT INTO hourly_aggregates({', '.join(_HOURLY_COLS)})
        VALUES ({placeholders})
        ON CONFLICT(dest_id, hour_bucket) DO UPDATE SET {updates}
        """,
        vals,
    )
    await conn.commit()


async def list_hourly_aggregates(
    conn: aiosqlite.Connection, dest_id: int, since_ms: int, until_ms: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM hourly_aggregates
        WHERE dest_id = ? AND hour_bucket >= ? AND hour_bucket < ?
        ORDER BY hour_bucket ASC
        """,
        (dest_id, since_ms, until_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def latest_finalized_hour(conn: aiosqlite.Connection, dest_id: int) -> int | None:
    cur = await conn.execute(
        """
        SELECT MAX(hour_bucket) AS h FROM hourly_aggregates
        WHERE dest_id = ? AND finalized = 1
        """,
        (dest_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row["h"] if row and row["h"] is not None else None


# ===== Daily aggregates =====


_DAILY_COLS = (
    "dest_id",
    "day_bucket",
    "count_total",
    "count_ok",
    "count_spike",
    "p50_us",
    "p95_us",
    "p99_us",
    "min_us",
    "max_us",
    "mean_us",
    "loss_pct",
    "jitter_us",
    "outage_count",
    "outage_ms",
)


async def upsert_daily_aggregate(conn: aiosqlite.Connection, row: dict[str, Any]) -> None:
    vals = tuple(row.get(c) for c in _DAILY_COLS)
    placeholders = ", ".join(["?"] * len(_DAILY_COLS))
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in _DAILY_COLS if c not in ("dest_id", "day_bucket")
    )
    await conn.execute(
        f"""
        INSERT INTO daily_aggregates({', '.join(_DAILY_COLS)})
        VALUES ({placeholders})
        ON CONFLICT(dest_id, day_bucket) DO UPDATE SET {updates}
        """,
        vals,
    )
    await conn.commit()


async def list_daily_aggregates(
    conn: aiosqlite.Connection, dest_id: int, since_ms: int, until_ms: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM daily_aggregates
        WHERE dest_id = ? AND day_bucket >= ? AND day_bucket < ?
        ORDER BY day_bucket ASC
        """,
        (dest_id, since_ms, until_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ===== Outages =====


async def open_outage(
    conn: aiosqlite.Connection,
    *,
    dest_id: int | None,
    start_ts_ms: int,
    type_: OutageType | str,
    lost_count: int,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO outages(dest_id_primary, start_ts_ms, type, lost_count)
        VALUES (?, ?, ?, ?)
        """,
        (dest_id, start_ts_ms, str(type_), lost_count),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


async def close_outage(conn: aiosqlite.Connection, outage_id: int, end_ts_ms: int) -> None:
    await conn.execute(
        "UPDATE outages SET end_ts_ms = ? WHERE id = ? AND end_ts_ms IS NULL",
        (end_ts_ms, outage_id),
    )
    await conn.commit()


async def add_outage_member(
    conn: aiosqlite.Connection,
    outage_id: int,
    dest_id: int,
    start_ts_ms: int,
    end_ts_ms: int | None,
    lost_count: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO outage_members(outage_id, dest_id, start_ts_ms, end_ts_ms, lost_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(outage_id, dest_id) DO UPDATE SET
          end_ts_ms = excluded.end_ts_ms,
          lost_count = excluded.lost_count
        """,
        (outage_id, dest_id, start_ts_ms, end_ts_ms, lost_count),
    )
    await conn.commit()


async def update_outage_type(
    conn: aiosqlite.Connection, outage_id: int, type_: OutageType | str
) -> None:
    await conn.execute(
        "UPDATE outages SET type = ? WHERE id = ?",
        (str(type_), outage_id),
    )
    await conn.commit()


async def list_outages(
    conn: aiosqlite.Connection,
    *,
    since_ms: int,
    until_ms: int,
    types: set[OutageType] | None = None,
    dest_id: int | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses = ["start_ts_ms >= ?", "start_ts_ms < ?"]
    params: list[Any] = [since_ms, until_ms]
    if types:
        placeholders = ", ".join(["?"] * len(types))
        clauses.append(f"type IN ({placeholders})")
        params.extend([str(t) for t in types])
    if dest_id is not None:
        clauses.append("dest_id_primary = ?")
        params.append(dest_id)
    if search:
        clauses.append("(notes LIKE ?)")
        params.append(f"%{search}%")
    sql = (
        "SELECT id, dest_id_primary, start_ts_ms, end_ts_ms, duration_ms, lost_count, "
        "type, suspect_hop_no, trace_id, notes FROM outages "
        f"WHERE {' AND '.join(clauses)} ORDER BY start_ts_ms DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    cur = await conn.execute(sql, tuple(params))
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def get_outage(conn: aiosqlite.Connection, outage_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, dest_id_primary, start_ts_ms, end_ts_ms, duration_ms, lost_count, "
        "type, suspect_hop_no, trace_id, notes FROM outages WHERE id = ?",
        (outage_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_outage_members(
    conn: aiosqlite.Connection, outage_id: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT outage_id, dest_id, start_ts_ms, end_ts_ms, lost_count "
        "FROM outage_members WHERE outage_id = ? ORDER BY start_ts_ms ASC",
        (outage_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def open_outages(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT id, dest_id_primary, start_ts_ms, lost_count, type "
        "FROM outages WHERE end_ts_ms IS NULL ORDER BY start_ts_ms ASC"
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def count_outages(
    conn: aiosqlite.Connection,
    dest_id: int | None,
    since_ms: int,
    until_ms: int,
) -> int:
    if dest_id is not None:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM outages "
            "WHERE dest_id_primary = ? AND start_ts_ms >= ? AND start_ts_ms < ?",
            (dest_id, since_ms, until_ms),
        )
    else:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM outages "
            "WHERE start_ts_ms >= ? AND start_ts_ms < ?",
            (since_ms, until_ms),
        )
    row = await cur.fetchone()
    await cur.close()
    return int(row["c"]) if row else 0


async def total_outage_ms(
    conn: aiosqlite.Connection,
    dest_id: int | None,
    since_ms: int,
    until_ms: int,
) -> int:
    if dest_id is not None:
        cur = await conn.execute(
            "SELECT COALESCE(SUM(duration_ms), 0) AS s FROM outages "
            "WHERE dest_id_primary = ? AND start_ts_ms >= ? AND start_ts_ms < ? "
            "AND duration_ms IS NOT NULL",
            (dest_id, since_ms, until_ms),
        )
    else:
        cur = await conn.execute(
            "SELECT COALESCE(SUM(duration_ms), 0) AS s FROM outages "
            "WHERE start_ts_ms >= ? AND start_ts_ms < ? AND duration_ms IS NOT NULL",
            (since_ms, until_ms),
        )
    row = await cur.fetchone()
    await cur.close()
    return int(row["s"]) if row else 0


# ===== Traceroutes =====


async def insert_traceroute(
    conn: aiosqlite.Connection,
    *,
    dest_id: int,
    ts_ms: int,
    trigger: str,
    success: bool,
    hop_count: int,
    signature_hash: str,
    route_changed: bool,
    hops: list[TraceHop],
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO traceroutes(dest_id, ts_ms, trigger, success, hop_count,
                                signature_hash, route_changed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dest_id,
            ts_ms,
            str(trigger),
            1 if success else 0,
            hop_count,
            signature_hash,
            1 if route_changed else 0,
        ),
    )
    trace_id = cast(int, cur.lastrowid)
    if hops:
        await conn.executemany(
            """
            INSERT INTO trace_hops(trace_id, hop_no, host, ip, rtt_us,
                                   rtt_min_us, rtt_max_us, loss_pct, is_suspect)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            [
                (
                    trace_id,
                    h.hop_no,
                    h.host,
                    h.ip,
                    h.rtt_us,
                    h.rtt_min_us,
                    h.rtt_max_us,
                    h.loss_pct,
                )
                for h in hops
            ],
        )
    await conn.commit()
    return trace_id


async def list_traceroutes(
    conn: aiosqlite.Connection,
    dest_id: int,
    since_ms: int,
    until_ms: int,
    *,
    changed_only: bool = False,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, dest_id, ts_ms, trigger, success, hop_count, "
        "signature_hash, route_changed FROM traceroutes "
        "WHERE dest_id = ? AND ts_ms >= ? AND ts_ms < ?"
    )
    params: list[Any] = [dest_id, since_ms, until_ms]
    if changed_only:
        sql += " AND route_changed = 1"
    sql += " ORDER BY ts_ms DESC"
    cur = await conn.execute(sql, tuple(params))
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def get_traceroute(conn: aiosqlite.Connection, trace_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, dest_id, ts_ms, trigger, success, hop_count, "
        "signature_hash, route_changed FROM traceroutes WHERE id = ?",
        (trace_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_trace_hops(conn: aiosqlite.Connection, trace_id: int) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT trace_id, hop_no, host, ip, rtt_us, rtt_min_us, rtt_max_us, "
        "loss_pct, is_suspect FROM trace_hops WHERE trace_id = ? ORDER BY hop_no ASC",
        (trace_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def upsert_route_signature(
    conn: aiosqlite.Connection,
    dest_id: int,
    signature_hash: str,
    signature_text: str,
    ts_ms: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO route_signatures(dest_id, signature_hash, signature_text,
                                     first_seen_ts_ms, last_seen_ts_ms, seen_count)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(dest_id, signature_hash) DO UPDATE SET
          last_seen_ts_ms = excluded.last_seen_ts_ms,
          seen_count      = route_signatures.seen_count + 1
        """,
        (dest_id, signature_hash, signature_text, ts_ms, ts_ms),
    )
    await conn.commit()


async def hop_history(
    conn: aiosqlite.Connection, dest_id: int, hop_no: int, since_ms: int
) -> list[tuple[int, int | None]]:
    cur = await conn.execute(
        """
        SELECT t.ts_ms AS ts_ms, h.rtt_us AS rtt_us
        FROM traceroutes t JOIN trace_hops h ON h.trace_id = t.id
        WHERE t.dest_id = ? AND h.hop_no = ? AND t.ts_ms >= ?
        ORDER BY t.ts_ms ASC
        """,
        (dest_id, hop_no, since_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["rtt_us"]) for r in rows]


# ===== WiFi =====


async def insert_wifi_event(conn: aiosqlite.Connection, ev: WifiEvent) -> int:
    cur = await conn.execute(
        """
        INSERT INTO wifi_events(ts_ms, event_type, ssid, bssid, rssi,
                                channel, duration_ms, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ev.ts_ms,
            str(ev.event_type),
            ev.ssid,
            ev.bssid,
            ev.rssi,
            ev.channel,
            ev.duration_ms,
            ev.raw,
        ),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


async def list_wifi_events(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT id, ts_ms, event_type, ssid, bssid, rssi, channel, duration_ms, raw
        FROM wifi_events WHERE ts_ms >= ? AND ts_ms < ?
        ORDER BY ts_ms DESC
        """,
        (since_ms, until_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def insert_rssi_sample(
    conn: aiosqlite.Connection,
    ts_ms: int,
    rssi: int,
    channel: int | None,
    link_rate_kbps: int | None,
    noise_dbm: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO wifi_rssi_samples(ts_ms, rssi, channel, link_rate_kbps, noise_dbm)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ts_ms) DO UPDATE SET
          rssi = excluded.rssi,
          channel = excluded.channel,
          link_rate_kbps = excluded.link_rate_kbps,
          noise_dbm = excluded.noise_dbm
        """,
        (ts_ms, rssi, channel, link_rate_kbps, noise_dbm),
    )
    await conn.commit()


async def latest_rssi(conn: aiosqlite.Connection) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT ts_ms, rssi, channel, link_rate_kbps, noise_dbm "
        "FROM wifi_rssi_samples ORDER BY ts_ms DESC LIMIT 1"
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def rssi_series(
    conn: aiosqlite.Connection, since_ms: int
) -> list[tuple[int, int]]:
    cur = await conn.execute(
        "SELECT ts_ms, rssi FROM wifi_rssi_samples WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (since_ms,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["rssi"]) for r in rows]


# ===== Stream =====


async def insert_stream_event(conn: aiosqlite.Connection, ev: StreamEvent) -> int:
    cur = await conn.execute(
        """
        INSERT INTO stream_events(ts_ms, event_type, duration_ms,
                                  kbps_before, kbps_after, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ev.ts_ms, str(ev.event_type), ev.duration_ms, ev.kbps_before, ev.kbps_after, ev.reason),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


async def list_stream_events(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT id, ts_ms, event_type, duration_ms, kbps_before, kbps_after, reason
        FROM stream_events WHERE ts_ms >= ? AND ts_ms < ?
        ORDER BY ts_ms DESC
        """,
        (since_ms, until_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def insert_throughput_sample(
    conn: aiosqlite.Connection,
    ts_ms: int,
    kbps: int,
    tcp_rtt_us: int | None,
    bytes_total: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO stream_throughput_samples(ts_ms, kbps, tcp_rtt_us, bytes_total)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ts_ms) DO UPDATE SET
          kbps = excluded.kbps,
          tcp_rtt_us = excluded.tcp_rtt_us,
          bytes_total = excluded.bytes_total
        """,
        (ts_ms, kbps, tcp_rtt_us, bytes_total),
    )
    await conn.commit()


async def throughput_series(
    conn: aiosqlite.Connection, since_ms: int
) -> list[tuple[int, int]]:
    cur = await conn.execute(
        "SELECT ts_ms, kbps FROM stream_throughput_samples "
        "WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (since_ms,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["kbps"]) for r in rows]


async def bytes_today(conn: aiosqlite.Connection) -> int:
    # Local midnight: rely on caller's timezone setting; default to UTC midnight here.
    today_start = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) * 1000)
    cur = await conn.execute(
        """
        SELECT COALESCE(MAX(bytes_total), 0) - COALESCE(MIN(bytes_total), 0) AS b
        FROM stream_throughput_samples WHERE ts_ms >= ?
        """,
        (today_start,),
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row["b"]) if row else 0


# ===== Heartbeat =====


async def insert_heartbeat_event(conn: aiosqlite.Connection, ev: HeartbeatEvent) -> int:
    cur = await conn.execute(
        """
        INSERT INTO heartbeat_events(ts_ms, event_type, duration_ms, missed_count)
        VALUES (?, ?, ?, ?)
        """,
        (ev.ts_ms, str(ev.event_type), ev.duration_ms, ev.missed_count),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


# ===== Notifications =====


async def insert_notification(
    conn: aiosqlite.Connection,
    *,
    severity: str,
    target_kind: str,
    target_id: int | None,
    message: str,
    ts_ms: int,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO notifications(ts_ms, severity, target_kind, target_id, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts_ms, severity, target_kind, target_id, message),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


async def list_unseen_notifications(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT id, ts_ms, severity, target_kind, target_id, message
        FROM notifications WHERE seen_at_ts_ms IS NULL
        ORDER BY ts_ms DESC
        """
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def mark_notifications_seen(
    conn: aiosqlite.Connection, ids: list[int]
) -> None:
    if not ids:
        return
    placeholders = ", ".join(["?"] * len(ids))
    await conn.execute(
        f"UPDATE notifications SET seen_at_ts_ms = ? WHERE id IN ({placeholders})",
        (_now_ms(), *ids),
    )
    await conn.commit()


# ===== Speedtests =====


_SPEEDTEST_COLS = (
    "id, ts_ms, down_mbps, up_mbps, latency_ms, jitter_ms, server, "
    "bytes_down, bytes_up, duration_ms, status, error, task_id"
)


async def insert_speedtest_start(
    conn: aiosqlite.Connection, task_id: str, ts_ms: int
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO speedtests(ts_ms, status, task_id)
        VALUES (?, 'running', ?)
        """,
        (ts_ms, task_id),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


async def update_speedtest_done(
    conn: aiosqlite.Connection, task_id: str, result: dict[str, Any]
) -> None:
    await conn.execute(
        """
        UPDATE speedtests
           SET status      = 'done',
               down_mbps   = ?,
               up_mbps     = ?,
               latency_ms  = ?,
               jitter_ms   = ?,
               server      = COALESCE(?, server),
               bytes_down  = COALESCE(?, bytes_down),
               bytes_up    = COALESCE(?, bytes_up),
               duration_ms = ?
         WHERE task_id = ?
        """,
        (
            result.get("down_mbps"),
            result.get("up_mbps"),
            result.get("latency_ms"),
            result.get("jitter_ms"),
            result.get("server"),
            result.get("bytes_down"),
            result.get("bytes_up"),
            result.get("duration_ms"),
            task_id,
        ),
    )
    await conn.commit()


async def update_speedtest_failed(
    conn: aiosqlite.Connection, task_id: str, error: str
) -> None:
    await conn.execute(
        "UPDATE speedtests SET status = 'failed', error = ? WHERE task_id = ?",
        (error, task_id),
    )
    await conn.commit()


async def speedtest_last(conn: aiosqlite.Connection) -> dict[str, Any] | None:
    cur = await conn.execute(
        f"SELECT {_SPEEDTEST_COLS} FROM speedtests WHERE status = 'done' "
        "ORDER BY ts_ms DESC LIMIT 1"
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def speedtest_by_task(
    conn: aiosqlite.Connection, task_id: str
) -> dict[str, Any] | None:
    cur = await conn.execute(
        f"SELECT {_SPEEDTEST_COLS} FROM speedtests WHERE task_id = ?",
        (task_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def speedtest_history(
    conn: aiosqlite.Connection, limit: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        f"SELECT {_SPEEDTEST_COLS} FROM speedtests "
        "ORDER BY ts_ms DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def cleanup_running_speedtests(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute(
        "UPDATE speedtests SET status = 'aborted' WHERE status = 'running'"
    )
    await conn.commit()
    return cur.rowcount or 0
