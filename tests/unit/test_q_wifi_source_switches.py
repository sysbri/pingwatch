# tests/unit/test_q_wifi_source_switches.py
"""Tests for wifi_source_switches persistence + queries."""
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.db import queries


@pytest.mark.asyncio
async def test_table_exists_on_fresh_db(db: aiosqlite.Connection) -> None:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='wifi_source_switches'"
    )
    assert await cur.fetchone() is not None


@pytest.mark.asyncio
async def test_insert_and_query_source_switches(db: aiosqlite.Connection) -> None:
    await queries.insert_source_switch(db, 1000, "wlan0", "wlan1")
    await queries.insert_source_switch(db, 2000, "wlan1", "wlan0")
    rows = await queries.source_switches(db, since_ms=0)
    assert rows == [
        {"ts_ms": 1000, "from_if": "wlan0", "to_if": "wlan1"},
        {"ts_ms": 2000, "from_if": "wlan1", "to_if": "wlan0"},
    ]
    # since_ms filters
    assert await queries.source_switches(db, since_ms=1500) == [
        {"ts_ms": 2000, "from_if": "wlan1", "to_if": "wlan0"},
    ]
