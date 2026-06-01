"""Pure helper functions for WiFi state-machine transitions.

The monitor passes successive ``WifiSnapshot`` values to :func:`diff_snapshots`
which returns a list of ``WifiEvent`` describing the transition. ``classify_gap``
distinguishes a brief reassoc (short disassoc followed by quick reassociation)
from a real disconnect using a configurable minimum-duration threshold.
"""

from __future__ import annotations

from pingwatch.models import WifiEvent, WifiEventType, WifiSnapshot


def diff_snapshots(prev: WifiSnapshot | None, cur: WifiSnapshot) -> list[WifiEvent]:
    """Return ordered list of events implied by the transition ``prev -> cur``.

    First call (``prev is None``) emits a CONNECT event iff currently associated.
    """
    events: list[WifiEvent] = []
    if prev is None:
        if cur.associated:
            events.append(
                WifiEvent(
                    ts_ms=cur.ts_ms,
                    event_type=WifiEventType.CONNECT,
                    ssid=cur.ssid,
                    bssid=cur.bssid,
                    rssi=cur.rssi,
                    channel=cur.channel,
                )
            )
        return events

    if prev.associated and not cur.associated:
        events.append(
            WifiEvent(
                ts_ms=cur.ts_ms,
                event_type=WifiEventType.DISCONNECT,
                ssid=prev.ssid,
                bssid=prev.bssid,
                rssi=prev.rssi,
                channel=prev.channel,
            )
        )
        return events

    if not prev.associated and cur.associated:
        events.append(
            WifiEvent(
                ts_ms=cur.ts_ms,
                event_type=WifiEventType.CONNECT,
                ssid=cur.ssid,
                bssid=cur.bssid,
                rssi=cur.rssi,
                channel=cur.channel,
            )
        )
        return events

    if prev.associated and cur.associated:
        if prev.bssid and cur.bssid and prev.bssid != cur.bssid:
            events.append(
                WifiEvent(
                    ts_ms=cur.ts_ms,
                    event_type=WifiEventType.ROAM,
                    ssid=cur.ssid,
                    bssid=cur.bssid,
                    rssi=cur.rssi,
                    channel=cur.channel,
                )
            )
        if prev.ssid != cur.ssid:
            events.append(
                WifiEvent(
                    ts_ms=cur.ts_ms,
                    event_type=WifiEventType.SSID_CHANGE,
                    ssid=cur.ssid,
                    bssid=cur.bssid,
                    rssi=cur.rssi,
                    channel=cur.channel,
                )
            )
        if prev.channel != cur.channel:
            events.append(
                WifiEvent(
                    ts_ms=cur.ts_ms,
                    event_type=WifiEventType.CHANNEL_CHANGE,
                    ssid=cur.ssid,
                    bssid=cur.bssid,
                    rssi=cur.rssi,
                    channel=cur.channel,
                )
            )
    return events


def classify_gap(
    disconnect_ts_ms: int,
    reconnect_ts_ms: int,
    reassoc_min_duration_ms: int,
) -> WifiEvent:
    """Decide if a disassoc->assoc round-trip is a reassoc or a full disconnect.

    Caller passes the ts of the original disconnect and the subsequent connect.
    Returns a single :class:`WifiEvent` describing the round-trip. Duration is
    always populated.
    """
    duration_ms = max(0, reconnect_ts_ms - disconnect_ts_ms)
    if duration_ms < reassoc_min_duration_ms:
        return WifiEvent(
            ts_ms=reconnect_ts_ms,
            event_type=WifiEventType.REASSOC,
            duration_ms=duration_ms,
        )
    return WifiEvent(
        ts_ms=reconnect_ts_ms,
        event_type=WifiEventType.DISCONNECT,
        duration_ms=duration_ms,
    )


def is_reassoc(duration_ms: int, reassoc_min_duration_ms: int) -> bool:
    return duration_ms >= reassoc_min_duration_ms
