"""Final outage-type classification and WLAN tagging.

Precedence (single tag per outage row):
    WLAN > UPLINK > MULTI > EINZEL

STREAM outages are produced directly by the stream worker and are not
upgraded here.
"""

from __future__ import annotations

import structlog

from pingwatch.db import queries
from pingwatch.models import OutageType, WifiEvent, WifiEventType

log = structlog.get_logger(__name__)

WLAN_SLACK_MS = 1000


_PRECEDENCE: dict[OutageType, int] = {
    OutageType.EINZEL: 1,
    OutageType.MULTI: 2,
    OutageType.UPLINK: 3,
    OutageType.WLAN: 4,
    OutageType.STREAM: 4,
}


def stronger(new: OutageType, old: OutageType) -> bool:
    return _PRECEDENCE[new] > _PRECEDENCE[old]


async def maybe_tag_wlan(
    conn: object,
    outage_id: int,
    reassoc_min_duration_ms: int = 2000,
    slack_ms: int = WLAN_SLACK_MS,
) -> bool:
    """Tag an outage as WLAN if a wifi disconnect/reassoc overlaps it.

    Returns True when the type was updated to WLAN.
    """
    outage = await queries.get_outage(conn, outage_id)
    if outage is None:
        return False
    start_ts = int(outage["start_ts_ms"])
    end_ts_raw = outage["end_ts_ms"]
    end_ts = int(end_ts_raw) if end_ts_raw is not None else start_ts
    cur_type = OutageType(outage["type"])

    window_start = start_ts - slack_ms
    window_end = end_ts + slack_ms
    events = await _wifi_events_between(conn, window_start, window_end)
    for ev in events:
        event_type = ev["event_type"]
        if event_type == WifiEventType.DISCONNECT.value:
            if _within(int(ev["ts_ms"]), window_start, window_end):
                await _apply_wlan(conn, outage_id, cur_type)
                return True
        elif event_type == WifiEventType.REASSOC.value:
            duration = ev["duration_ms"] or 0
            if duration >= reassoc_min_duration_ms and _within(
                int(ev["ts_ms"]), window_start, window_end
            ):
                await _apply_wlan(conn, outage_id, cur_type)
                return True
    return False


async def tag_from_wifi_event(
    conn: object,
    event: WifiEvent,
    reassoc_min_duration_ms: int = 2000,
    slack_ms: int = WLAN_SLACK_MS,
) -> list[int]:
    """When a wifi event arrives, re-check any outage overlapping ±slack."""
    if event.event_type == WifiEventType.REASSOC and (event.duration_ms or 0) < reassoc_min_duration_ms:
        return []
    if event.event_type not in (WifiEventType.DISCONNECT, WifiEventType.REASSOC):
        return []
    ts = event.ts_ms
    rows = await _outages_overlapping(conn, ts - slack_ms, ts + slack_ms)
    tagged: list[int] = []
    for row in rows:
        cur_type = OutageType(row["type"])
        if cur_type is OutageType.STREAM:
            continue
        await _apply_wlan(conn, int(row["id"]), cur_type)
        tagged.append(int(row["id"]))
    return tagged


async def _apply_wlan(conn: object, outage_id: int, cur_type: OutageType) -> None:
    if not stronger(OutageType.WLAN, cur_type):
        return
    await queries.update_outage_type(conn, outage_id, OutageType.WLAN)
    log.info("outage.tagged_wlan", outage_id=outage_id, previous=cur_type.value)


def _within(ts: int, lo: int, hi: int) -> bool:
    return lo <= ts <= hi


async def _wifi_events_between(conn: object, start_ts_ms: int, end_ts_ms: int) -> list[dict[str, object]]:
    cur = await conn.execute(  # type: ignore[attr-defined]
        "SELECT id, ts_ms, event_type, duration_ms, ssid, bssid FROM wifi_events "
        "WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms ASC",
        (start_ts_ms, end_ts_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _outages_overlapping(
    conn: object, window_start_ms: int, window_end_ms: int
) -> list[dict[str, object]]:
    cur = await conn.execute(  # type: ignore[attr-defined]
        "SELECT id, dest_id_primary, start_ts_ms, end_ts_ms, type, lost_count "
        "FROM outages "
        "WHERE start_ts_ms <= ? AND (end_ts_ms IS NULL OR end_ts_ms >= ?)",
        (window_end_ms, window_start_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def run_classifier_subscriber(conn: object, bus: object) -> None:
    """Subscribe to wifi.events and apply WLAN tag to overlapping outages.

    Spawned as a worker from main.py — closes the gap between the wifi monitor
    and the classifier so a disconnect retroactively retags an in-flight outage.
    """
    sub = bus.subscribe("wifi.events")  # type: ignore[attr-defined]
    async with sub as queue:
        while True:
            event = await queue.get()
            if not isinstance(event, WifiEvent):
                continue
            try:
                tagged = await tag_from_wifi_event(conn, event)
                if tagged:
                    log.info("classifier.wlan_tagged", outage_ids=tagged, ts_ms=event.ts_ms)
            except Exception as exc:  # noqa: BLE001
                log.error("classifier.tag_failed", error=repr(exc), ts_ms=event.ts_ms)
