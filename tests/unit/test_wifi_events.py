"""Tests for wifi/events.py – diff_snapshots transitions and monitor._coalesce_reassoc."""

from __future__ import annotations

import pytest

from pingwatch.models import (
    WifiEvent,
    WifiEventType,
    WifiSnapshot,
)
from pingwatch.wifi.events import diff_snapshots
from pingwatch.wifi.monitor import WifiMonitor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snap(
    *,
    ts_ms: int = 1000,
    associated: bool = True,
    ssid: str | None = "HomeNet",
    bssid: str | None = "aa:bb:cc:dd:ee:ff",
    rssi: int | None = -60,
    channel: int | None = 6,
) -> WifiSnapshot:
    return WifiSnapshot(
        ts_ms=ts_ms,
        ssid=ssid,
        bssid=bssid,
        rssi=rssi,
        channel=channel,
        link_rate_kbps=None,
        associated=associated,
    )


def _disconnected(ts_ms: int = 2000) -> WifiSnapshot:
    return WifiSnapshot(
        ts_ms=ts_ms,
        ssid=None,
        bssid=None,
        rssi=None,
        channel=None,
        link_rate_kbps=None,
        associated=False,
    )


# ---------------------------------------------------------------------------
# diff_snapshots – initial connect (prev is None)
# ---------------------------------------------------------------------------


def test_initial_connect_while_associated() -> None:
    snap = _snap(ts_ms=1000)
    events = diff_snapshots(None, snap)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == WifiEventType.CONNECT
    assert ev.ssid == "HomeNet"
    assert ev.bssid == "aa:bb:cc:dd:ee:ff"
    assert ev.ts_ms == 1000


def test_initial_not_associated_produces_no_events() -> None:
    snap = _disconnected(ts_ms=500)
    events = diff_snapshots(None, snap)
    assert events == []


# ---------------------------------------------------------------------------
# diff_snapshots – disconnect
# ---------------------------------------------------------------------------


def test_disconnect_produces_disconnect_event() -> None:
    prev = _snap(ts_ms=1000)
    cur = _disconnected(ts_ms=2000)
    events = diff_snapshots(prev, cur)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == WifiEventType.DISCONNECT
    assert ev.ts_ms == 2000
    # disconnect event carries the previous snap's info
    assert ev.ssid == "HomeNet"
    assert ev.bssid == "aa:bb:cc:dd:ee:ff"


def test_disconnect_stops_after_first_event() -> None:
    prev = _snap(ts_ms=1000)
    cur = _disconnected(ts_ms=2000)
    events = diff_snapshots(prev, cur)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# diff_snapshots – reconnect after disconnect
# ---------------------------------------------------------------------------


def test_reconnect_produces_connect_event() -> None:
    prev = _disconnected(ts_ms=1000)
    cur = _snap(ts_ms=3000)
    events = diff_snapshots(prev, cur)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == WifiEventType.CONNECT
    assert ev.ts_ms == 3000
    assert ev.ssid == "HomeNet"


# ---------------------------------------------------------------------------
# diff_snapshots – BSSID roam
# ---------------------------------------------------------------------------


def test_bssid_roam_produces_roam_event() -> None:
    prev = _snap(ts_ms=1000, bssid="aa:bb:cc:dd:ee:ff")
    cur = _snap(ts_ms=2000, bssid="11:22:33:44:55:66")
    events = diff_snapshots(prev, cur)
    assert any(e.event_type == WifiEventType.ROAM for e in events)
    roam = next(e for e in events if e.event_type == WifiEventType.ROAM)
    assert roam.bssid == "11:22:33:44:55:66"
    assert roam.ts_ms == 2000


def test_bssid_unchanged_no_roam_event() -> None:
    prev = _snap(ts_ms=1000, bssid="aa:bb:cc:dd:ee:ff")
    cur = _snap(ts_ms=2000, bssid="aa:bb:cc:dd:ee:ff")
    events = diff_snapshots(prev, cur)
    assert not any(e.event_type == WifiEventType.ROAM for e in events)


# ---------------------------------------------------------------------------
# diff_snapshots – SSID change
# ---------------------------------------------------------------------------


def test_ssid_change_produces_ssid_change_event() -> None:
    prev = _snap(ts_ms=1000, ssid="HomeNet")
    cur = _snap(ts_ms=2000, ssid="OtherNet")
    events = diff_snapshots(prev, cur)
    assert any(e.event_type == WifiEventType.SSID_CHANGE for e in events)
    change = next(e for e in events if e.event_type == WifiEventType.SSID_CHANGE)
    assert change.ssid == "OtherNet"


def test_ssid_unchanged_no_ssid_change_event() -> None:
    prev = _snap(ts_ms=1000, ssid="HomeNet")
    cur = _snap(ts_ms=2000, ssid="HomeNet")
    events = diff_snapshots(prev, cur)
    assert not any(e.event_type == WifiEventType.SSID_CHANGE for e in events)


# ---------------------------------------------------------------------------
# diff_snapshots – signal-only change (no structural events)
# ---------------------------------------------------------------------------


def test_rssi_only_change_produces_no_events() -> None:
    """Signal strength change alone must not produce any WifiEvent."""
    prev = _snap(ts_ms=1000, rssi=-60, bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet", channel=6)
    cur = _snap(ts_ms=2000, rssi=-75, bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet", channel=6)
    events = diff_snapshots(prev, cur)
    assert events == []


# ---------------------------------------------------------------------------
# diff_snapshots – channel change
# ---------------------------------------------------------------------------


def test_channel_change_produces_channel_change_event() -> None:
    prev = _snap(ts_ms=1000, channel=6)
    cur = _snap(ts_ms=2000, channel=11)
    events = diff_snapshots(prev, cur)
    assert any(e.event_type == WifiEventType.CHANNEL_CHANGE for e in events)


def test_channel_unchanged_no_channel_change_event() -> None:
    prev = _snap(ts_ms=1000, channel=6)
    cur = _snap(ts_ms=2000, channel=6)
    events = diff_snapshots(prev, cur)
    assert not any(e.event_type == WifiEventType.CHANNEL_CHANGE for e in events)


# ---------------------------------------------------------------------------
# monitor._coalesce_reassoc – quick disconnect+reconnect becomes REASSOC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coalesce_reassoc_short_gap_becomes_reassoc() -> None:
    """DISCONNECT followed by CONNECT within reassoc_min_duration_ms → REASSOC."""
    from pingwatch.wifi.monitor import WifiConfig

    cfg = WifiConfig(reassoc_min_duration_ms=2000)
    monitor = WifiMonitor(conn=object(), config=cfg)

    disconnect_ev = WifiEvent(
        ts_ms=1000, event_type=WifiEventType.DISCONNECT, ssid="HomeNet", bssid="aa:bb:cc:dd:ee:ff"
    )
    connect_ev = WifiEvent(
        ts_ms=1500,  # 500 ms later – within the 2000 ms threshold
        event_type=WifiEventType.CONNECT,
        ssid="HomeNet",
        bssid="aa:bb:cc:dd:ee:ff",
    )
    snap = _snap(ts_ms=1500)
    out = await monitor._coalesce_reassoc([disconnect_ev, connect_ev], snap)

    # We expect: DISCONNECT (preserved), REASSOC (replacing CONNECT)
    assert len(out) == 2
    assert out[0].event_type == WifiEventType.DISCONNECT
    assert out[1].event_type == WifiEventType.REASSOC
    assert out[1].duration_ms == 500


@pytest.mark.asyncio
async def test_coalesce_reassoc_long_gap_stays_connect() -> None:
    """DISCONNECT + CONNECT beyond reassoc_min_duration_ms stays as a normal CONNECT."""
    from pingwatch.wifi.monitor import WifiConfig

    cfg = WifiConfig(reassoc_min_duration_ms=2000)
    monitor = WifiMonitor(conn=object(), config=cfg)

    disconnect_ev = WifiEvent(
        ts_ms=1000, event_type=WifiEventType.DISCONNECT, ssid="HomeNet"
    )
    connect_ev = WifiEvent(
        ts_ms=5000,  # 4000 ms later – beyond the 2000 ms threshold
        event_type=WifiEventType.CONNECT,
        ssid="HomeNet",
    )
    snap = _snap(ts_ms=5000)
    out = await monitor._coalesce_reassoc([disconnect_ev, connect_ev], snap)

    assert len(out) == 2
    assert out[0].event_type == WifiEventType.DISCONNECT
    assert out[1].event_type == WifiEventType.CONNECT
    assert out[1].duration_ms == 4000


@pytest.mark.asyncio
async def test_coalesce_reassoc_no_pending_disconnect_passes_connect_through() -> None:
    """A CONNECT without a preceding DISCONNECT is passed through unchanged."""
    from pingwatch.wifi.monitor import WifiConfig

    cfg = WifiConfig(reassoc_min_duration_ms=2000)
    monitor = WifiMonitor(conn=object(), config=cfg)

    connect_ev = WifiEvent(ts_ms=1000, event_type=WifiEventType.CONNECT, ssid="HomeNet")
    snap = _snap(ts_ms=1000)
    out = await monitor._coalesce_reassoc([connect_ev], snap)

    assert len(out) == 1
    assert out[0].event_type == WifiEventType.CONNECT
