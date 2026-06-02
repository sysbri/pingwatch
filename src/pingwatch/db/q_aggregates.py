"""Hourly and daily aggregate queries."""

from __future__ import annotations

from typing import Any

import aiosqlite

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
        """,  # noqa: S608  # internal constant identifier, not user input
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
        """,  # noqa: S608  # internal constant identifier, not user input
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
