"""overview endpoint exposes link-rate series, interface, and switch markers."""
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.api.routes import wifi as wifi_routes
from pingwatch.db import queries


@pytest.mark.asyncio
async def test_overview_includes_link_series_and_switches(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # Live status file → current.interface
    monkeypatch.setattr(
        wifi_routes, "_read_wifi_status_file",
        lambda: {"connected": True, "ssid": "Cafe-Free", "rssi_dbm": -55,
                 "bitrate_mbps": 144.0, "interface": "wlan1", "interface_label": "usb"},
    )
    now_ms = 10_000_000
    monkeypatch.setattr(wifi_routes.time, "time", lambda: now_ms / 1000)

    await queries.insert_rssi_sample(db, now_ms - 5000, -50, 36, 144_000)
    await queries.insert_rssi_sample(db, now_ms - 4000, -52, 36, 86_000)
    await queries.insert_source_switch(db, now_ms - 4500, "wlan0", "wlan1")

    payload = await wifi_routes.overview_endpoint(conn=db, range_="24h")

    assert payload["current"]["interface"] == "wlan1"
    assert payload["current"]["interface_label"] == "usb"
    assert payload["link_series"] == [
        {"ts_ms": now_ms - 5000, "link_rate_kbps": 144_000},
        {"ts_ms": now_ms - 4000, "link_rate_kbps": 86_000},
    ]
    assert payload["source_switches"] == [
        {"ts_ms": now_ms - 4500, "from_if": "wlan0", "to_if": "wlan1"},
    ]
