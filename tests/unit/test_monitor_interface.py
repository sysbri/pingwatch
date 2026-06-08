# tests/unit/test_monitor_interface.py
"""Monitor carries the active interface from the host-helper status file."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pingwatch.wifi.monitor import WifiConfig, WifiMonitor


def _write_status(path: Path, **fields: object) -> None:
    base = {"ts_ms": int(time.time() * 1000), "connected": True,
            "ssid": "Cafe-Free", "bssid": "aa:bb:cc:dd:ee:ff",
            "rssi_dbm": -55, "bitrate_mbps": 144.0, "channel": 36}
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")


@pytest.mark.asyncio
async def test_snapshot_carries_interface_from_file(tmp_path: Path) -> None:
    status = tmp_path / "wifi-status.json"
    _write_status(status, interface="wlan1")
    mon = WifiMonitor(None, config=WifiConfig(status_file=str(status)))
    snap = mon._sample_from_file(int(time.time() * 1000))
    assert snap is not None
    assert snap.interface == "wlan1"
    assert snap.link_rate_kbps == 144_000
