"""Retention purge tests — regression for the WITHOUT ROWID crash and the
ms-cutoff fix (hour_bucket/day_bucket are stored in ms, not hours/days)."""

from __future__ import annotations

import time

import aiosqlite
import pytest

from pingwatch.db.retention import DAY_MS, RetentionWorker


@pytest.mark.asyncio
async def test_purge_rowid_and_without_rowid_tables(db: aiosqlite.Connection) -> None:
    now = int(time.time() * 1000)
    old = now - 400 * DAY_MS      # older than every retention window
    recent = now - 1 * DAY_MS     # well within every window

    # raw_pings has a rowid; hourly_aggregates is WITHOUT ROWID (the table that
    # raised "no such column: rowid" before the fix) and keys on hour_bucket (ms).
    await db.execute("INSERT INTO raw_pings(dest_id, ts_ms, success) VALUES (1, ?, 1)", (old,))
    await db.execute("INSERT INTO raw_pings(dest_id, ts_ms, success) VALUES (1, ?, 1)", (recent,))
    for hb in (old, recent):
        await db.execute(
            "INSERT INTO hourly_aggregates(dest_id, hour_bucket, count_total, count_ok, loss_pct) "
            "VALUES (1, ?, 1, 1, 0.0)",
            (hb,),
        )
    await db.commit()

    # Must not raise (was: sqlite3.OperationalError: no such column: rowid).
    await RetentionWorker(db).purge_once()

    rp = [r["ts_ms"] for r in await (await db.execute("SELECT ts_ms FROM raw_pings")).fetchall()]
    ha = [
        r["hour_bucket"]
        for r in await (await db.execute("SELECT hour_bucket FROM hourly_aggregates")).fetchall()
    ]
    assert rp == [recent], rp                 # rowid table purged correctly
    assert ha == [recent], ha                 # WITHOUT ROWID + ms-cutoff both fixed


@pytest.mark.asyncio
async def test_purge_respects_recent_data(db: aiosqlite.Connection) -> None:
    """Nothing within the retention window is deleted."""
    now = int(time.time() * 1000)
    for ts in (now, now - 3 * DAY_MS):  # raw_pings window default = 7 days
        await db.execute("INSERT INTO raw_pings(dest_id, ts_ms, success) VALUES (2, ?, 1)", (ts,))
    await db.commit()

    await RetentionWorker(db).purge_once()

    cur = await db.execute("SELECT COUNT(*) AS n FROM raw_pings")
    assert (await cur.fetchone())["n"] == 2
