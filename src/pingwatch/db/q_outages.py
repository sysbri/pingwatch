"""Outage queries."""

from __future__ import annotations

from typing import Any, cast

import aiosqlite

from ..models import OutageType

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
        "SELECT id, dest_id_primary, start_ts_ms, end_ts_ms, duration_ms, lost_count, "  # noqa: S608  # internal constant identifier, not user input
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
