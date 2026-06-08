"""Monitor records a wifi_source_switches row when the interface changes."""
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.models import WifiSnapshot
from pingwatch.wifi.monitor import WifiMonitor


def _snap(ts: int, iface: str) -> WifiSnapshot:
    return WifiSnapshot(
        ts_ms=ts, ssid="Cafe-Free", bssid="aa:bb:cc:dd:ee:ff",
        rssi=-55, channel=36, link_rate_kbps=144_000, associated=True, interface=iface,
    )


@pytest.mark.asyncio
async def test_records_switch_on_interface_change(db: aiosqlite.Connection) -> None:
    mon = WifiMonitor(db)
    await mon._handle(_snap(1000, "wlan0"))   # first: sets baseline, no marker
    await mon._handle(_snap(2000, "wlan1"))   # change: one marker
    cur = await db.execute(
        "SELECT ts_ms, from_if, to_if FROM wifi_source_switches ORDER BY ts_ms"
    )
    assert [dict(r) for r in await cur.fetchall()] == [
        {"ts_ms": 2000, "from_if": "wlan0", "to_if": "wlan1"},
    ]


@pytest.mark.asyncio
async def test_no_marker_when_interface_stable(db: aiosqlite.Connection) -> None:
    mon = WifiMonitor(db)
    await mon._handle(_snap(1000, "wlan1"))
    await mon._handle(_snap(2000, "wlan1"))
    cur = await db.execute("SELECT COUNT(*) AS n FROM wifi_source_switches")
    assert (await cur.fetchone())["n"] == 0
