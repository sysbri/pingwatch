"""Tests for the gateway auto-sync worker (follow the real default route)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiosqlite
import pytest

from pingwatch.gateway_sync import _read_gateway_ip, sync_once


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))


async def _gateway_address(db: aiosqlite.Connection) -> str:
    cur = await db.execute("SELECT address FROM destinations WHERE kind = 'gateway'")
    row = await cur.fetchone()
    return row["address"]


@pytest.mark.asyncio
async def test_updates_gateway_address_and_resets_history(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO raw_pings(dest_id, ts_ms, success) VALUES (1, 1000, 0)"
    )
    await db.commit()
    bus = _Bus()

    changed = await sync_once(db, bus, "10.99.0.1")

    assert changed is True
    assert await _gateway_address(db) == "10.99.0.1"
    # History of the old address is gone (clean start, same as manual edit).
    cur = await db.execute("SELECT COUNT(*) AS n FROM raw_pings WHERE dest_id = 1")
    assert (await cur.fetchone())["n"] == 0
    topics = [t for t, _ in bus.published]
    assert "config.changed" in topics
    assert "targets.address_changed" in topics


@pytest.mark.asyncio
async def test_noop_when_address_already_current(db: aiosqlite.Connection) -> None:
    bus = _Bus()
    changed = await sync_once(db, bus, "192.168.1.1")
    assert changed is False
    assert bus.published == []


@pytest.mark.asyncio
async def test_noop_without_gateway_ip(db: aiosqlite.Connection) -> None:
    bus = _Bus()
    assert await sync_once(db, bus, None) is False
    assert await _gateway_address(db) == "192.168.1.1"


@pytest.mark.asyncio
async def test_respects_auto_update_setting_off(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO settings(key, value, value_type, updated_at_ts_ms) "
        "VALUES ('gateway.auto_update', 'false', 'bool', 0)"
    )
    await db.commit()
    bus = _Bus()
    changed = await sync_once(db, bus, "10.99.0.1")
    assert changed is False
    assert await _gateway_address(db) == "192.168.1.1"


def test_read_gateway_ip_missing_file(tmp_path: Path) -> None:
    assert _read_gateway_ip(tmp_path / "nope.json") is None


def test_read_gateway_ip_fresh_file(tmp_path: Path) -> None:
    f = tmp_path / "wifi-status.json"
    f.write_text(json.dumps({"gateway_ip": "10.1.98.1"}))
    assert _read_gateway_ip(f) == "10.1.98.1"


def test_read_gateway_ip_stale_file(tmp_path: Path) -> None:
    f = tmp_path / "wifi-status.json"
    f.write_text(json.dumps({"gateway_ip": "10.1.98.1"}))
    old = time.time() - 600
    os.utime(f, (old, old))
    assert _read_gateway_ip(f) is None
