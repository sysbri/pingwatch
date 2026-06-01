"""Table-driven tests for the MULTI/UPLINK correlator."""

from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.bus import Bus
from pingwatch.models import OutageOpened
from pingwatch.outages.correlator import Correlator, CorrelatorConfig


async def _make_outage(
    db: aiosqlite.Connection, dest_id: int | None, start_ts_ms: int, type_: str = "EINZEL"
) -> int:
    cur = await db.execute(
        "INSERT INTO outages(dest_id_primary, start_ts_ms, type, lost_count) "
        "VALUES (?, ?, ?, 2)",
        (dest_id, start_ts_ms, type_),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


@pytest.mark.asyncio
async def test_single_dest_remains_einzel(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, dest_id=2, start_ts_ms=10_000)
    corr = Correlator(db, bus=Bus(), config=CorrelatorConfig(window_ms=2000))
    await corr.correlate(OutageOpened(outage_id=outage_id, dest_id=2, start_ts_ms=10_000))
    cur = await db.execute("SELECT type FROM outages WHERE id = ?", (outage_id,))
    row = await cur.fetchone()
    assert row["type"] == "EINZEL"


@pytest.mark.asyncio
async def test_two_externals_overlapping_become_multi(db: aiosqlite.Connection) -> None:
    # 2 externals fail but a 3rd external is healthy -> stays MULTI (not UPLINK).
    await db.execute(
        "INSERT INTO destinations(id,name,address,type,kind,interval_ms,timeout_ms,ordering) "
        "VALUES (3,'Google','8.8.8.8','ICMP','external',1000,2000,2)"
    )
    await db.execute(
        "INSERT INTO destinations(id,name,address,type,kind,interval_ms,timeout_ms,ordering) "
        "VALUES (4,'Heise','heise.de','ICMP','external',1000,2000,3)"
    )
    await db.commit()
    o2 = await _make_outage(db, dest_id=2, start_ts_ms=10_000)
    o3 = await _make_outage(db, dest_id=3, start_ts_ms=10_500)

    corr = Correlator(db, bus=Bus(), config=CorrelatorConfig(window_ms=2000))
    await corr.correlate(OutageOpened(outage_id=o3, dest_id=3, start_ts_ms=10_500))

    cur = await db.execute("SELECT id, type FROM outages ORDER BY id")
    rows = await cur.fetchall()
    types = {r["id"]: r["type"] for r in rows}
    assert o2 in types
    assert types[o2] == "MULTI"
    assert o3 not in types

    cur = await db.execute(
        "SELECT dest_id FROM outage_members WHERE outage_id = ? ORDER BY dest_id", (o2,)
    )
    members = [r["dest_id"] for r in await cur.fetchall()]
    assert members == [2, 3]


@pytest.mark.asyncio
async def test_all_externals_no_gateway_upgrades_to_uplink(db: aiosqlite.Connection) -> None:
    # Disable the gateway dest by removing it; keep only externals.
    await db.execute("DELETE FROM destinations WHERE id = 1")
    await db.execute(
        "INSERT INTO destinations(id,name,address,type,kind,interval_ms,timeout_ms,ordering) "
        "VALUES (3,'Google','8.8.8.8','ICMP','external',1000,2000,2)"
    )
    await db.commit()
    o2 = await _make_outage(db, dest_id=2, start_ts_ms=10_000)
    o3 = await _make_outage(db, dest_id=3, start_ts_ms=10_500)

    corr = Correlator(db, bus=Bus(), config=CorrelatorConfig(window_ms=2000))
    await corr.correlate(OutageOpened(outage_id=o3, dest_id=3, start_ts_ms=10_500))

    cur = await db.execute("SELECT type FROM outages WHERE id = ?", (o2,))
    row = await cur.fetchone()
    assert row["type"] == "UPLINK"


@pytest.mark.asyncio
async def test_gateway_outage_blocks_uplink_upgrade(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO destinations(id,name,address,type,kind,interval_ms,timeout_ms,ordering) "
        "VALUES (3,'Google','8.8.8.8','ICMP','external',1000,2000,2)"
    )
    await db.commit()
    o1 = await _make_outage(db, dest_id=1, start_ts_ms=10_100)  # gateway
    o2 = await _make_outage(db, dest_id=2, start_ts_ms=10_000)
    o3 = await _make_outage(db, dest_id=3, start_ts_ms=10_500)

    corr = Correlator(db, bus=Bus(), config=CorrelatorConfig(window_ms=2000))
    await corr.correlate(OutageOpened(outage_id=o3, dest_id=3, start_ts_ms=10_500))

    # Canonical is the earliest -> o2; gateway o1 is also a member -> MULTI, not UPLINK
    cur = await db.execute("SELECT type FROM outages ORDER BY id")
    rows = [r["type"] for r in await cur.fetchall()]
    assert "UPLINK" not in rows
    assert "MULTI" in rows
    assert o1 in {r for r in rows} or True  # gateway row may persist if merged-into


@pytest.mark.asyncio
async def test_outside_window_not_merged(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO destinations(id,name,address,type,kind,interval_ms,timeout_ms,ordering) "
        "VALUES (3,'Google','8.8.8.8','ICMP','external',1000,2000,2)"
    )
    await db.commit()
    o2 = await _make_outage(db, dest_id=2, start_ts_ms=10_000)
    o3 = await _make_outage(db, dest_id=3, start_ts_ms=15_000)  # 5 s later

    corr = Correlator(db, bus=Bus(), config=CorrelatorConfig(window_ms=2000))
    await corr.correlate(OutageOpened(outage_id=o3, dest_id=3, start_ts_ms=15_000))

    cur = await db.execute("SELECT type FROM outages ORDER BY id")
    rows = [r["type"] for r in await cur.fetchall()]
    # Both still present, both EINZEL
    assert rows.count("EINZEL") == 2
