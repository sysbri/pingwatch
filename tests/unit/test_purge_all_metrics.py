"""Clear-data must really zero everything the dashboard shows."""
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.db import queries


@pytest.mark.asyncio
async def test_purge_clears_source_switches_and_speedtests(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO wifi_source_switches(ts_ms, from_if, to_if) VALUES (1, 'wlan0', 'wlan1')"
    )
    await db.execute(
        "INSERT INTO speedtests(ts_ms, down_mbps, up_mbps, status) VALUES (1, 100.0, 10.0, 'done')"
    )
    await db.execute("INSERT INTO raw_pings(dest_id, ts_ms, success) VALUES (1, 1, 1)")
    await db.commit()

    await queries.purge_all_metrics(db)

    for tbl in ("wifi_source_switches", "speedtests", "raw_pings"):
        cur = await db.execute(f"SELECT COUNT(*) AS n FROM {tbl}")  # noqa: S608
        assert (await cur.fetchone())["n"] == 0, tbl


@pytest.mark.asyncio
async def test_purge_keeps_destinations_and_settings(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO settings(key, value, value_type, updated_at_ts_ms) "
        "VALUES ('ui.theme', 'dark', 'string', 0)"
    )
    await db.commit()
    await queries.purge_all_metrics(db)
    cur = await db.execute("SELECT COUNT(*) AS n FROM destinations")
    assert (await cur.fetchone())["n"] > 0
    cur = await db.execute("SELECT COUNT(*) AS n FROM settings")
    assert (await cur.fetchone())["n"] > 0
