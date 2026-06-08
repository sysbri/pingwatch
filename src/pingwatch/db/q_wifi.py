"""WiFi queries."""

from __future__ import annotations

from typing import Any, cast

import aiosqlite

from ..models import WifiEvent

# ===== WiFi =====


async def insert_wifi_event(conn: aiosqlite.Connection, ev: WifiEvent) -> int:
    cur = await conn.execute(
        """
        INSERT INTO wifi_events(ts_ms, event_type, ssid, bssid, rssi,
                                channel, duration_ms, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ev.ts_ms,
            str(ev.event_type),
            ev.ssid,
            ev.bssid,
            ev.rssi,
            ev.channel,
            ev.duration_ms,
            ev.raw,
        ),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


async def list_wifi_events(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT id, ts_ms, event_type, ssid, bssid, rssi, channel, duration_ms, raw
        FROM wifi_events WHERE ts_ms >= ? AND ts_ms < ?
        ORDER BY ts_ms DESC
        """,
        (since_ms, until_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def insert_rssi_sample(
    conn: aiosqlite.Connection,
    ts_ms: int,
    rssi: int,
    channel: int | None,
    link_rate_kbps: int | None,
    noise_dbm: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO wifi_rssi_samples(ts_ms, rssi, channel, link_rate_kbps, noise_dbm)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ts_ms) DO UPDATE SET
          rssi = excluded.rssi,
          channel = excluded.channel,
          link_rate_kbps = excluded.link_rate_kbps,
          noise_dbm = excluded.noise_dbm
        """,
        (ts_ms, rssi, channel, link_rate_kbps, noise_dbm),
    )
    await conn.commit()


async def latest_rssi(conn: aiosqlite.Connection) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT ts_ms, rssi, channel, link_rate_kbps, noise_dbm "
        "FROM wifi_rssi_samples ORDER BY ts_ms DESC LIMIT 1"
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def rssi_series(
    conn: aiosqlite.Connection, since_ms: int
) -> list[tuple[int, int]]:
    cur = await conn.execute(
        "SELECT ts_ms, rssi FROM wifi_rssi_samples WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (since_ms,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["rssi"]) for r in rows]


async def insert_source_switch(
    conn: aiosqlite.Connection, ts_ms: int, from_if: str | None, to_if: str
) -> None:
    await conn.execute(
        "INSERT INTO wifi_source_switches(ts_ms, from_if, to_if) VALUES (?, ?, ?)",
        (ts_ms, from_if, to_if),
    )
    await conn.commit()


async def source_switches(
    conn: aiosqlite.Connection, since_ms: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT ts_ms, from_if, to_if FROM wifi_source_switches "
        "WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (since_ms,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def link_rate_series(
    conn: aiosqlite.Connection, since_ms: int
) -> list[tuple[int, int]]:
    cur = await conn.execute(
        "SELECT ts_ms, link_rate_kbps FROM wifi_rssi_samples "
        "WHERE ts_ms >= ? AND link_rate_kbps IS NOT NULL ORDER BY ts_ms ASC",
        (since_ms,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["link_rate_kbps"]) for r in rows]
