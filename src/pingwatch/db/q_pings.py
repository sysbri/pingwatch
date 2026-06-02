"""Raw pings queries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

import aiosqlite

from ..models import PingSample

_PingStatus = Literal["ok", "spike", "loss"]


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
        "SELECT id, dest_id, ts_ms, success, latency_us, ttl, sequence, error_kind, flags "  # noqa: S608  # internal constant identifier, not user input
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
