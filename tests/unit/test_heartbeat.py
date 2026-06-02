"""TCP heartbeat: connect-time measurement + miss detection (no payload writes)."""

from __future__ import annotations

import asyncio
import contextlib

import aiosqlite
import pytest

from pingwatch.streams.tcp_heartbeat import HeartbeatConfig, TcpHeartbeatWorker


async def _accept_and_close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


async def _start_server() -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(_accept_and_close, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _wait_until(predicate, max_s: float = 1.0) -> bool:
    for _ in range(int(max_s / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_heartbeat_measures_connect(db: aiosqlite.Connection) -> None:
    server, port = await _start_server()
    cfg = HeartbeatConfig(target=f"127.0.0.1:{port}", interval_ms=10, miss_threshold_ms=10_000)
    w = TcpHeartbeatWorker(db, config=cfg)
    task = asyncio.create_task(w.run())
    try:
        assert await _wait_until(lambda: w._last_hb_ts_ms is not None)  # connect-time beat landed
    finally:
        w.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_heartbeat_emits_loss_when_target_dies(db: aiosqlite.Connection) -> None:
    server, port = await _start_server()
    cfg = HeartbeatConfig(target=f"127.0.0.1:{port}", interval_ms=10, miss_threshold_ms=40)
    w = TcpHeartbeatWorker(db, config=cfg)
    task = asyncio.create_task(w.run())
    try:
        assert await _wait_until(lambda: w._last_hb_ts_ms is not None)
        server.close()
        await server.wait_closed()  # connects now fail -> miss-watcher should fire LOSS

        async def _has_event() -> bool:
            cur = await db.execute("SELECT COUNT(*) AS n FROM heartbeat_events")
            return (await cur.fetchone())["n"] >= 1

        for _ in range(100):
            if await _has_event():
                break
            await asyncio.sleep(0.01)
        cur = await db.execute("SELECT event_type FROM heartbeat_events")
        kinds = [r["event_type"] for r in await cur.fetchall()]
        assert kinds, "expected a LOSS event after the target went away"
    finally:
        w.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
