"""Traceroute queries."""

from __future__ import annotations

from typing import Any, cast

import aiosqlite

from ..models import TraceHop

# ===== Traceroutes =====


async def insert_traceroute(
    conn: aiosqlite.Connection,
    *,
    dest_id: int,
    ts_ms: int,
    trigger: str,
    success: bool,
    hop_count: int,
    signature_hash: str,
    route_changed: bool,
    hops: list[TraceHop],
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO traceroutes(dest_id, ts_ms, trigger, success, hop_count,
                                signature_hash, route_changed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dest_id,
            ts_ms,
            str(trigger),
            1 if success else 0,
            hop_count,
            signature_hash,
            1 if route_changed else 0,
        ),
    )
    trace_id = cast(int, cur.lastrowid)
    if hops:
        await conn.executemany(
            """
            INSERT INTO trace_hops(trace_id, hop_no, host, ip, rtt_us,
                                   rtt_min_us, rtt_max_us, loss_pct, is_suspect)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            [
                (
                    trace_id,
                    h.hop_no,
                    h.host,
                    h.ip,
                    h.rtt_us,
                    h.rtt_min_us,
                    h.rtt_max_us,
                    h.loss_pct,
                )
                for h in hops
            ],
        )
    await conn.commit()
    return trace_id


async def list_traceroutes(
    conn: aiosqlite.Connection,
    dest_id: int,
    since_ms: int,
    until_ms: int,
    *,
    changed_only: bool = False,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, dest_id, ts_ms, trigger, success, hop_count, "
        "signature_hash, route_changed FROM traceroutes "
        "WHERE dest_id = ? AND ts_ms >= ? AND ts_ms < ?"
    )
    params: list[Any] = [dest_id, since_ms, until_ms]
    if changed_only:
        sql += " AND route_changed = 1"
    sql += " ORDER BY ts_ms DESC"
    cur = await conn.execute(sql, tuple(params))
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def get_traceroute(conn: aiosqlite.Connection, trace_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, dest_id, ts_ms, trigger, success, hop_count, "
        "signature_hash, route_changed FROM traceroutes WHERE id = ?",
        (trace_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_trace_hops(conn: aiosqlite.Connection, trace_id: int) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT trace_id, hop_no, host, ip, rtt_us, rtt_min_us, rtt_max_us, "
        "loss_pct, is_suspect FROM trace_hops WHERE trace_id = ? ORDER BY hop_no ASC",
        (trace_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def upsert_route_signature(
    conn: aiosqlite.Connection,
    dest_id: int,
    signature_hash: str,
    signature_text: str,
    ts_ms: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO route_signatures(dest_id, signature_hash, signature_text,
                                     first_seen_ts_ms, last_seen_ts_ms, seen_count)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(dest_id, signature_hash) DO UPDATE SET
          last_seen_ts_ms = excluded.last_seen_ts_ms,
          seen_count      = route_signatures.seen_count + 1
        """,
        (dest_id, signature_hash, signature_text, ts_ms, ts_ms),
    )
    await conn.commit()


async def hop_history(
    conn: aiosqlite.Connection, dest_id: int, hop_no: int, since_ms: int
) -> list[tuple[int, int | None]]:
    cur = await conn.execute(
        """
        SELECT t.ts_ms AS ts_ms, h.rtt_us AS rtt_us
        FROM traceroutes t JOIN trace_hops h ON h.trace_id = t.id
        WHERE t.dest_id = ? AND h.hop_no = ? AND t.ts_ms >= ?
        ORDER BY t.ts_ms ASC
        """,
        (dest_id, hop_no, since_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["rtt_us"]) for r in rows]
