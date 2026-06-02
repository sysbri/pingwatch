"""Stream (HTTP trickle + throughput), heartbeat, and notification queries."""

from __future__ import annotations

import time
from typing import Any, cast

import aiosqlite

from ..models import HeartbeatEvent, StreamEvent

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


def _now_ms() -> int:
    return int(time.time() * 1000)


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
        f"UPDATE notifications SET seen_at_ts_ms = ? WHERE id IN ({placeholders})",  # noqa: S608  # internal constant identifier, not user input
        (_now_ms(), *ids),
    )
    await conn.commit()
