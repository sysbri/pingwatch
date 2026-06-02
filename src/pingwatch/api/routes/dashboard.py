"""Dashboard payload — single endpoint feeds the entire dashboard screen."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from pingwatch.api import _queries_compat as q
from pingwatch.api.deps import ConnDep

router = APIRouter(prefix="/api", tags=["dashboard"])


async def build_dashboard_payload(conn: Any) -> dict[str, Any]:
    """Assemble the full dashboard payload. Reused by HTTP + WebSocket tick."""
    now_ms = int(time.time() * 1000)
    day_start_ms = now_ms - 24 * 3_600_000

    hero = await q.hero_stats(conn, since_ms=day_start_ms)
    stream = await q.stream_summary(conn, since_ms=day_start_ms)
    wifi = await q.wifi_current(conn)

    destinations = await q.list_destinations(conn)
    # Nur enabled-Ziele auf dem Dashboard (User-Toggle in Settings -> Ziele).
    destinations = [d for d in destinations if d.get("enabled")]
    cards: list[dict[str, Any]] = []
    for dest in destinations:
        kpi = await q.dest_kpis(conn, dest["id"], since_ms=day_start_ms)
        spark = await q.latency_sparkline(conn, dest["id"], points=60)
        outs = await q.outages_today_for_dest(
            conn, dest["id"], since_ms=day_start_ms
        )
        cards.append(
            {
                "id": dest["id"],
                "name": dest["name"],
                "address": dest["address"],
                "type": dest["type"],
                "kind": dest["kind"],
                "enabled": bool(dest["enabled"]),
                "kpi": kpi,
                "sparkline": spark,
                "outages_today": outs,
                "status": _classify(kpi),
            }
        )

    # Bottom panels
    events = await q.recent_events(conn, limit=10)
    # 24h gaps timeline per dest + Stream + WLAN
    timeline = []
    for dest in destinations:
        outs = await q.outages_today_for_dest(
            conn, dest["id"], since_ms=day_start_ms
        )
        timeline.append({
            "id": dest["id"],
            "name": dest["name"],
            "gaps": [
                {"start_ms": o["start_ts_ms"], "end_ms": o.get("end_ts_ms"),
                 "type": o["type"]}
                for o in outs
            ],
        })
    # Stream gaps
    s_cur = await conn.execute(
        "SELECT ts_ms AS start_ms, "
        "ts_ms + COALESCE(duration_ms, 1000) AS end_ms "
        "FROM stream_events "
        "WHERE event_type IN ('drop','throughput_low') AND ts_ms >= ? "
        "ORDER BY ts_ms ASC",
        (day_start_ms,),
    )
    stream_gaps = [
        {"start_ms": r["start_ms"], "end_ms": r["end_ms"], "type": "STREAM"}
        for r in await s_cur.fetchall()
    ]
    timeline.append({"id": -1, "name": "Stream", "gaps": stream_gaps})
    # WLAN gaps
    w_cur = await conn.execute(
        "SELECT ts_ms AS start_ms, "
        "ts_ms + COALESCE(duration_ms, 1000) AS end_ms "
        "FROM wifi_events "
        "WHERE event_type IN ('disconnect','reassoc') AND ts_ms >= ? "
        "ORDER BY ts_ms ASC",
        (day_start_ms,),
    )
    wifi_gaps = [
        {"start_ms": r["start_ms"], "end_ms": r["end_ms"], "type": "WLAN"}
        for r in await w_cur.fetchall()
    ]
    timeline.append({"id": -2, "name": "WLAN", "gaps": wifi_gaps})

    # "Aktuelle Route"-Panel: Auswahl durch User-Setting moeglich.
    # Setting `dashboard.trace_dest_id` (int) = bevorzugtes Ziel.
    # Wenn nicht gesetzt oder "auto": waehlt das schlechteste externe Ziel
    # (hoechster Loss, dann hoechste mean Latenz).
    traces = await q.latest_traces_per_dest(conn)
    external_cards = [c for c in cards if c["kind"] != "gateway"]

    preferred_id_raw = await conn.execute(
        "SELECT value FROM settings WHERE key = 'dashboard.trace_dest_id'"
    )
    pref_row = await preferred_id_raw.fetchone()
    preferred_id: int | None = None
    if pref_row and pref_row["value"] and pref_row["value"] not in ("auto", ""):
        try:
            preferred_id = int(pref_row["value"])
        except (TypeError, ValueError):
            preferred_id = None

    worst_trace = None
    if preferred_id is not None and traces.get(preferred_id):
        worst_trace = traces.get(preferred_id)
    else:
        candidates = [c for c in external_cards if traces.get(c["id"])]
        if candidates:
            candidates.sort(
                key=lambda c: (c["kpi"]["loss_pct"], c["kpi"]["mean_us"] or 0),
                reverse=True,
            )
            worst_trace = traces.get(candidates[0]["id"])

    # Konnektivitaets-Status fuer dicke Header-Pills
    external_cards = [c for c in cards if c["kind"] == "external"]
    # WLAN-Status bevorzugt aus dem live-File des host-helper, fallback DB.
    import json as _json
    import os as _os
    wifi_ok = bool(wifi and wifi.get("rssi") is not None)
    wifi_ssid = wifi.get("ssid") if wifi else None
    wifi_rssi = wifi.get("rssi") if wifi else None
    try:
        _f = "/run/pingwatch-shared/wifi-status.json"
        _file_age_ms = (now_ms - _os.path.getmtime(_f) * 1000) if _os.path.exists(_f) else 999_999_999
        # Trigger fresh refresh wenn file aelter als 30s (fire-and-forget via FIFO)
        if _file_age_ms > 30_000:
            try:
                _fd = _os.open("/run/pingwatch-host.fifo", _os.O_WRONLY | _os.O_NONBLOCK)
                try:
                    _os.write(_fd, b"wifi_status\n")
                finally:
                    _os.close(_fd)
            except OSError:
                pass
        # Lese was da ist (auch wenn etwas stale, besser als nichts)
        if _os.path.exists(_f):
            with open(_f) as _fh:
                _live = _json.load(_fh)
            wifi_ok = bool(_live.get("connected"))
            wifi_ssid = _live.get("ssid")
            wifi_rssi = _live.get("rssi_dbm")
    except (OSError, ValueError):
        pass
    # Internet ok = mindestens 1 externes Ziel mit loss_pct < 50% in 24h
    internet_ok = any(
        (c["kpi"].get("loss_pct") or 0) < 50.0 and (c["kpi"].get("total") or 0) > 0
        for c in external_cards
    )
    internet_loss = (
        min((c["kpi"].get("loss_pct") or 0) for c in external_cards)
        if external_cards else None
    )
    # WLAN-Adapter-Card mit ssid/rssi/signal/bitrate/freq + events heute.
    # Daten primaer aus dem live wifi-status.json (gleicher Pfad wie pills oben).
    wifi_card_data = {
        "id": "wifi",
        "name": "WLAN-Adapter",
        "address": (wifi_ssid or "—"),
        "type": "WIFI",
        "kind": "wifi",
        "rssi_dbm": wifi_rssi,
        "signal_pct": None,
        "bitrate_mbps": None,
        "freq_mhz": None,
        "channel": None,
        "status": "ok" if wifi_ok else "down",
    }
    # Live-File parsen fuer Bitrate/Freq (falls vorhanden)
    try:
        _f2 = "/run/pingwatch-shared/wifi-status.json"
        if _os.path.exists(_f2):
            with open(_f2) as _fh2:
                _live2 = _json.load(_fh2)
            wifi_card_data["bitrate_mbps"] = _live2.get("bitrate_mbps")
            wifi_card_data["freq_mhz"] = _live2.get("freq")
            wifi_card_data["signal_pct"] = _live2.get("signal_pct")
    except (OSError, ValueError):
        pass
    # WLAN-Events heute (disconnect/reassoc)
    wev_cur = await conn.execute(
        "SELECT id, ts_ms, event_type, duration_ms FROM wifi_events "
        "WHERE event_type IN ('disconnect','reassoc') AND ts_ms >= ? "
        "ORDER BY ts_ms DESC LIMIT 5",
        (day_start_ms,),
    )
    wifi_card_data["events_today"] = [dict(r) for r in await wev_cur.fetchall()]
    # Aussetzer-Status: rot wenn >0 disconnects/reassocs in 24h
    if wifi_card_data["events_today"]:
        wifi_card_data["status"] = "flaky"

    return {
        "ts_ms": now_ms,
        "hero": hero,
        "stream": stream,
        "wifi": wifi,
        "wifi_card": wifi_card_data,
        "cards": cards,
        "timeline": timeline,
        "events": events,
        "worst_trace": worst_trace,
        "connectivity": {
            "wifi_ok": wifi_ok,
            "wifi_ssid": wifi_ssid,
            "wifi_rssi": wifi_rssi,
            "internet_ok": internet_ok,
            "internet_loss_pct": internet_loss,
        },
    }


def _classify(kpi: dict[str, Any]) -> str:
    if kpi["loss_pct"] > 1.0:
        return "flaky"
    return "ok"


@router.get("/dashboard")
async def get_dashboard(conn: ConnDep) -> dict[str, Any]:
    return await build_dashboard_payload(conn)
