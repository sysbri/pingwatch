"""Tests for the per-destination outage detector state machine."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiosqlite
import pytest
import pytest_asyncio

from pingwatch.bus import Bus
from pingwatch.models import (
    Destination,
    DestKind,
    OutageClosed,
    OutageOpened,
    PingSample,
    ProbeType,
)
from pingwatch.outages.detector import OutageDetector


@pytest_asyncio.fixture
async def two_dests(db: aiosqlite.Connection) -> AsyncIterator[list[Destination]]:
    destinations = [
        Destination(
            id=1,
            name="Gateway",
            address="192.168.1.1",
            type=ProbeType.ICMP,
            kind=DestKind.GATEWAY,
            interval_ms=1000,
            timeout_ms=2000,
            port=None,
            enabled=True,
            ordering=0,
        ),
        Destination(
            id=2,
            name="Cloudflare",
            address="1.1.1.1",
            type=ProbeType.ICMP,
            kind=DestKind.EXTERNAL,
            interval_ms=1000,
            timeout_ms=2000,
            port=None,
            enabled=True,
            ordering=1,
        ),
    ]
    yield destinations


def _ok(dest_id: int, ts: int) -> PingSample:
    return PingSample(dest_id=dest_id, ts_ms=ts, success=True, latency_us=10_000)


def _loss(dest_id: int, ts: int) -> PingSample:
    return PingSample(dest_id=dest_id, ts_ms=ts, success=False, error_kind="timeout")


async def _collect(bus: Bus, topic: str, n: int, timeout: float = 1.0) -> list[object]:  # noqa: ASYNC109  # explicit timeout param is intentional
    collected: list[object] = []
    async with bus.subscribe(topic) as q:
        for _ in range(n):
            collected.append(await asyncio.wait_for(q.get(), timeout=timeout))
    return collected


@pytest.mark.asyncio
async def test_two_losses_open_outage(
    db: aiosqlite.Connection, two_dests: list[Destination]
) -> None:
    bus = Bus()
    detector = OutageDetector(db, two_dests, bus=bus)
    collector_task = asyncio.create_task(_collect(bus, "outages.new", 1))
    await asyncio.sleep(0)  # let subscriber attach
    await detector.process(_loss(2, 1000))
    await detector.process(_loss(2, 2000))
    events = await collector_task
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, OutageOpened)
    assert ev.dest_id == 2
    # start_ts_ms should backdate to first loss (N=2, interval=1000)
    assert ev.start_ts_ms == 1000

    cur = await db.execute("SELECT id, dest_id_primary, start_ts_ms, end_ts_ms, type FROM outages")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["dest_id_primary"] == 2
    assert rows[0]["start_ts_ms"] == 1000
    assert rows[0]["end_ts_ms"] is None
    assert rows[0]["type"] == "EINZEL"


@pytest.mark.asyncio
async def test_two_oks_close_outage(db: aiosqlite.Connection, two_dests: list[Destination]) -> None:
    bus = Bus()
    detector = OutageDetector(db, two_dests, bus=bus)
    closes_task = asyncio.create_task(_collect(bus, "outages.close", 1))
    await asyncio.sleep(0)

    # Two losses → open
    await detector.process(_loss(2, 1000))
    await detector.process(_loss(2, 2000))
    # Two successes → close. Outage ends at the last loss = 2000.
    await detector.process(_ok(2, 3000))
    await detector.process(_ok(2, 4000))

    events = await closes_task
    assert len(events) == 1
    closed = events[0]
    assert isinstance(closed, OutageClosed)
    assert closed.dest_id == 2
    # first_ok=3000 → end = first_ok - interval = last loss = 2000
    assert closed.end_ts_ms == 2000

    cur = await db.execute("SELECT start_ts_ms, end_ts_ms, lost_count FROM outages")
    rows = await cur.fetchall()
    assert rows[0]["start_ts_ms"] == 1000
    assert rows[0]["end_ts_ms"] == 2000
    assert rows[0]["lost_count"] == 2


@pytest.mark.asyncio
async def test_single_loss_does_not_open(
    db: aiosqlite.Connection, two_dests: list[Destination]
) -> None:
    bus = Bus()
    detector = OutageDetector(db, two_dests, bus=bus)
    await detector.process(_loss(2, 1000))
    await detector.process(_ok(2, 2000))
    cur = await db.execute("SELECT COUNT(*) AS c FROM outages")
    row = await cur.fetchone()
    assert row["c"] == 0


@pytest.mark.asyncio
async def test_lost_count_increments_during_outage(
    db: aiosqlite.Connection, two_dests: list[Destination]
) -> None:
    bus = Bus()
    detector = OutageDetector(db, two_dests, bus=bus)
    for ts in range(1000, 6000, 1000):
        await detector.process(_loss(2, ts))
    await detector.process(_ok(2, 6000))
    await detector.process(_ok(2, 7000))

    cur = await db.execute("SELECT lost_count FROM outages")
    row = await cur.fetchone()
    assert row["lost_count"] == 5


@pytest.mark.asyncio
async def test_crash_recovery_force_closes_with_restart_note(
    db: aiosqlite.Connection, two_dests: list[Destination]
) -> None:
    # Pre-populate an open outage with a stale last ping.
    await db.execute(
        "INSERT INTO outages(dest_id_primary, start_ts_ms, type, lost_count) "
        "VALUES (2, 100, 'EINZEL', 5)"
    )
    await db.execute(
        "INSERT INTO raw_pings(dest_id, ts_ms, success) VALUES (2, 200, 0)"
    )
    await db.commit()

    detector = OutageDetector(db, two_dests, bus=Bus())
    # now_ms is far in the future relative to last_ts (200) -> 60s gap → force close
    await detector.recover_open_outages(now_ms=10_000_000)

    cur = await db.execute("SELECT end_ts_ms, notes FROM outages")
    row = await cur.fetchone()
    assert row["end_ts_ms"] == 200
    assert row["notes"] == "restart"


@pytest.mark.asyncio
async def test_independent_state_per_destination(
    db: aiosqlite.Connection, two_dests: list[Destination]
) -> None:
    bus = Bus()
    detector = OutageDetector(db, two_dests, bus=bus)

    await detector.process(_loss(1, 1000))
    await detector.process(_loss(2, 1000))
    await detector.process(_loss(1, 2000))  # dest1 opens here
    await detector.process(_ok(2, 2000))    # dest2 resets to 0
    await detector.process(_loss(2, 3000))

    cur = await db.execute(
        "SELECT dest_id_primary FROM outages ORDER BY id"
    )
    rows = await cur.fetchall()
    assert [r["dest_id_primary"] for r in rows] == [1]


@pytest.mark.asyncio
async def test_threshold_hot_reload(db: aiosqlite.Connection, two_dests: list[Destination]) -> None:
    bus = Bus()
    detector = OutageDetector(db, two_dests, bus=bus, n_consec_loss=3, k_consec_ok=2)
    await detector.process(_loss(2, 1000))
    await detector.process(_loss(2, 2000))

    cur = await db.execute("SELECT COUNT(*) AS c FROM outages")
    row = await cur.fetchone()
    assert row["c"] == 0  # N=3 -> not yet

    # After clearing the loss streak, lowering N to 2 takes effect on the next pair.
    await detector.process(_ok(2, 3000))
    await detector.process(_ok(2, 4000))
    detector.update_thresholds(n=2)
    assert detector.n_consec_loss == 2

    await detector.process(_loss(2, 5000))
    await detector.process(_loss(2, 6000))
    cur = await db.execute("SELECT COUNT(*) AS c FROM outages")
    row = await cur.fetchone()
    assert row["c"] == 1


@pytest.mark.asyncio
async def test_k3_close_uses_last_loss_as_end(
    db: aiosqlite.Connection, two_dests: list[Destination]
) -> None:
    """Regression: end_ts_ms must be the last loss regardless of K (was wrong K>=3)."""
    bus = Bus()
    detector = OutageDetector(db, two_dests, bus=bus, k_consec_ok=3)
    closes_task = asyncio.create_task(_collect(bus, "outages.close", 1))
    await asyncio.sleep(0)

    await detector.process(_loss(2, 1000))
    await detector.process(_loss(2, 2000))  # open, start=1000, last loss=2000
    await detector.process(_ok(2, 3000))    # first OK
    await detector.process(_ok(2, 4000))
    await detector.process(_ok(2, 5000))    # K=3 → close

    events = await closes_task
    closed = events[0]
    assert isinstance(closed, OutageClosed)
    # first_ok=3000 → end = first_ok - interval = last loss = 2000 (NOT 1000)
    assert closed.end_ts_ms == 2000

    cur = await db.execute("SELECT start_ts_ms, end_ts_ms FROM outages")
    row = await cur.fetchone()
    assert row["start_ts_ms"] == 1000
    assert row["end_ts_ms"] == 2000
