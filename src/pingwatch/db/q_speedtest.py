"""Speedtest queries."""

from __future__ import annotations

from typing import Any, cast

import aiosqlite

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
        f"SELECT {_SPEEDTEST_COLS} FROM speedtests WHERE status = 'done' "  # noqa: S608  # internal constant identifier, not user input
        "ORDER BY ts_ms DESC LIMIT 1"
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def speedtest_by_task(
    conn: aiosqlite.Connection, task_id: str
) -> dict[str, Any] | None:
    cur = await conn.execute(
        f"SELECT {_SPEEDTEST_COLS} FROM speedtests WHERE task_id = ?",  # noqa: S608  # internal constant identifier, not user input
        (task_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def speedtest_history(
    conn: aiosqlite.Connection, limit: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        f"SELECT {_SPEEDTEST_COLS} FROM speedtests "  # noqa: S608  # internal constant identifier, not user input
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
