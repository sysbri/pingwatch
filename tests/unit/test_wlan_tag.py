"""Tests for the WLAN tag classifier."""

from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.models import OutageType, WifiEvent, WifiEventType
from pingwatch.outages.classifier import maybe_tag_wlan, tag_from_wifi_event


async def _make_outage(
    db: aiosqlite.Connection, start: int, end: int | None, type_: str = "EINZEL"
) -> int:
    cur = await db.execute(
        "INSERT INTO outages(dest_id_primary, start_ts_ms, end_ts_ms, type, lost_count) "
        "VALUES (2, ?, ?, ?, 2)",
        (start, end, type_),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


async def _make_wifi_event(
    db: aiosqlite.Connection, ts: int, event_type: str, duration_ms: int | None = None
) -> None:
    await db.execute(
        "INSERT INTO wifi_events(ts_ms, event_type, duration_ms) VALUES (?, ?, ?)",
        (ts, event_type, duration_ms),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_disconnect_overlap_tags_wlan(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, 10_000, 12_000)
    await _make_wifi_event(db, 10_500, "disconnect")
    tagged = await maybe_tag_wlan(db, outage_id)
    assert tagged is True
    cur = await db.execute("SELECT type FROM outages WHERE id = ?", (outage_id,))
    row = await cur.fetchone()
    assert row["type"] == OutageType.WLAN.value


@pytest.mark.asyncio
async def test_reassoc_below_threshold_not_wlan(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, 10_000, 12_000)
    await _make_wifi_event(db, 10_500, "reassoc", duration_ms=500)
    tagged = await maybe_tag_wlan(db, outage_id, reassoc_min_duration_ms=2000)
    assert tagged is False
    cur = await db.execute("SELECT type FROM outages WHERE id = ?", (outage_id,))
    row = await cur.fetchone()
    assert row["type"] == "EINZEL"


@pytest.mark.asyncio
async def test_reassoc_long_enough_tags_wlan(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, 10_000, 12_000)
    await _make_wifi_event(db, 10_500, "reassoc", duration_ms=3000)
    tagged = await maybe_tag_wlan(db, outage_id, reassoc_min_duration_ms=2000)
    assert tagged is True


@pytest.mark.asyncio
async def test_wifi_event_within_slack_after_outage(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, 10_000, 12_000)
    # 800 ms after end -> within 1000 ms slack
    await _make_wifi_event(db, 12_800, "disconnect")
    tagged = await maybe_tag_wlan(db, outage_id)
    assert tagged is True


@pytest.mark.asyncio
async def test_wifi_event_outside_slack_no_tag(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, 10_000, 12_000)
    await _make_wifi_event(db, 14_000, "disconnect")  # 2 s after end -> outside slack
    tagged = await maybe_tag_wlan(db, outage_id)
    assert tagged is False


@pytest.mark.asyncio
async def test_tag_from_wifi_event_updates_open_outage(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, 10_000, None, "EINZEL")
    ev = WifiEvent(ts_ms=10_500, event_type=WifiEventType.DISCONNECT)
    tagged = await tag_from_wifi_event(db, ev)
    assert outage_id in tagged
    cur = await db.execute("SELECT type FROM outages WHERE id = ?", (outage_id,))
    row = await cur.fetchone()
    assert row["type"] == OutageType.WLAN.value


@pytest.mark.asyncio
async def test_wlan_overrides_multi(db: aiosqlite.Connection) -> None:
    outage_id = await _make_outage(db, 10_000, 12_000, type_="MULTI")
    await _make_wifi_event(db, 10_500, "disconnect")
    tagged = await maybe_tag_wlan(db, outage_id)
    assert tagged is True
    cur = await db.execute("SELECT type FROM outages WHERE id = ?", (outage_id,))
    row = await cur.fetchone()
    assert row["type"] == OutageType.WLAN.value
