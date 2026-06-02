"""Tests for WifiMonitor._sample_from_file (host-helper shared status file).

On the Pi the container cannot read the WLAN hardware; it consumes the
host-helper's ``/run/pingwatch-shared/wifi-status.json`` instead. These tests
pin the parsing + staleness behaviour of that path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pingwatch.wifi.monitor import WifiConfig, WifiMonitor


def _monitor(tmp_path: Path) -> WifiMonitor:
    cfg = WifiConfig(status_file=str(tmp_path / "wifi-status.json"))
    return WifiMonitor(conn=object(), config=cfg)


def _write(path: Path, payload: dict, *, age_s: float = 0.0) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    if age_s:
        past = time.time() - age_s
        os.utime(path, (past, past))


def test_fresh_connected_file_yields_snapshot(tmp_path: Path) -> None:
    mon = _monitor(tmp_path)
    _write(
        mon._status_path,
        {
            "ts_ms": 1234,
            "connected": True,
            "ssid": "FreeWiFi",
            "bssid": "aa:bb:cc:dd:ee:ff",
            "rssi_dbm": -42,
            "channel": 128,
            "bitrate_mbps": 24.0,
        },
    )
    snap = mon._sample_from_file(9999)
    assert snap is not None
    assert snap.associated is True
    assert snap.ts_ms == 1234  # the file's ts wins over the call-time ts
    assert snap.ssid == "FreeWiFi"
    assert snap.bssid == "aa:bb:cc:dd:ee:ff"
    assert snap.rssi == -42
    assert snap.channel == 128
    assert snap.link_rate_kbps == 24000


def test_fresh_disconnected_file_yields_disassociated_snapshot(tmp_path: Path) -> None:
    mon = _monitor(tmp_path)
    _write(mon._status_path, {"ts_ms": 5000, "connected": False})
    snap = mon._sample_from_file(9999)
    assert snap is not None
    assert snap.associated is False
    assert snap.rssi is None
    assert snap.ts_ms == 5000


def test_stale_file_returns_none(tmp_path: Path) -> None:
    """A lagging host-helper must not look like a disconnect -> skip the tick."""
    mon = _monitor(tmp_path)
    _write(
        mon._status_path,
        {"ts_ms": 1, "connected": True, "rssi_dbm": -50},
        age_s=mon._cfg.status_stale_after_s + 5,
    )
    assert mon._sample_from_file(9999) is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    mon = _monitor(tmp_path)
    assert mon._sample_from_file(9999) is None


def test_rssi_fallback_key(tmp_path: Path) -> None:
    """Older status files used ``rssi`` instead of ``rssi_dbm``."""
    mon = _monitor(tmp_path)
    _write(mon._status_path, {"ts_ms": 2, "connected": True, "rssi": -55})
    snap = mon._sample_from_file(9999)
    assert snap is not None
    assert snap.rssi == -55


def test_garbage_file_returns_none(tmp_path: Path) -> None:
    mon = _monitor(tmp_path)
    mon._status_path.write_text("not json{{", encoding="utf-8")
    assert mon._sample_from_file(9999) is None
