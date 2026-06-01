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

    return {
        "ts_ms": now_ms,
        "hero": hero,
        "stream": stream,
        "wifi": wifi,
        "cards": cards,
        "timeline": timeline,
        "events": events,
        "worst_trace": worst_trace,
    }


def _classify(kpi: dict[str, Any]) -> str:
    if kpi["loss_pct"] > 1.0:
        return "flaky"
    return "ok"


@router.get("/dashboard")
async def get_dashboard(conn: ConnDep) -> dict[str, Any]:
    return await build_dashboard_payload(conn)
