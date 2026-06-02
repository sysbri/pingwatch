"""Connectivity heartbeat over TCP.

Every ``heartbeat.interval_ms`` we open a TCP connection to the target, measure
the connect RTT, and close it. A miss-watcher emits a LOSS event once
``now - last_hb_ts`` exceeds ``heartbeat.miss_threshold_ms``; the next successful
connect emits RECOVER with the gap duration.

We measure *connect* time rather than writing a payload: the default target is a
TLS port (e.g. 1.1.1.1:443) which resets arbitrary bytes, so the old
write-and-readback approach produced constant "connection reset by peer" noise.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

import structlog

from pingwatch.bus import Bus, get_bus
from pingwatch.db import queries
from pingwatch.models import HeartbeatEvent, HeartbeatEventType
from pingwatch.util import sleep_or_stop

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class HeartbeatConfig:
    target: str = "1.1.1.1:443"
    interval_ms: int = 200
    miss_threshold_ms: int = 800
    connect_timeout_s: float = 5.0


class TcpHeartbeatWorker:
    def __init__(
        self,
        conn: object,
        bus: Bus | None = None,
        config: HeartbeatConfig | None = None,
    ) -> None:
        self._conn = conn
        self._bus = bus or get_bus()
        self._cfg = config or HeartbeatConfig()
        self._stop = asyncio.Event()
        self._last_hb_ts_ms: int | None = None
        self._loss_open: bool = False
        self._loss_start_ts_ms: int | None = None
        self._missed_count: int = 0

    async def run(self) -> None:
        watcher = asyncio.create_task(self._miss_watcher())
        try:
            await self._loop()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def _loop(self) -> None:
        host, port = self._parse_target()
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=self._cfg.connect_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # A failed connect is a missed beat; the miss-watcher emits LOSS
                # once the gap exceeds the threshold. No per-attempt log spam.
                await self._sleep_or_stop(self._cfg.interval_ms / 1000)
                continue
            rtt_us = int((time.perf_counter() - t0) * 1_000_000)
            writer.close()
            with contextlib.suppress(Exception):  # noqa: BLE001
                await writer.wait_closed()
            now_ms = int(time.time() * 1000)
            self._last_hb_ts_ms = now_ms
            if self._loss_open:
                await self._emit_recover(now_ms, rtt_us)
            await self._sleep_or_stop(self._cfg.interval_ms / 1000)

    async def _miss_watcher(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._cfg.interval_ms / 1000)
            now_ms = int(time.time() * 1000)
            last = self._last_hb_ts_ms
            if last is None:
                continue
            gap = now_ms - last
            if gap > self._cfg.miss_threshold_ms and not self._loss_open:
                await self._emit_loss(now_ms)

    async def _emit_loss(self, ts_ms: int) -> None:
        self._loss_open = True
        self._loss_start_ts_ms = ts_ms
        self._missed_count = 1
        event = HeartbeatEvent(
            ts_ms=ts_ms,
            event_type=HeartbeatEventType.LOSS,
            missed_count=1,
        )
        await queries.insert_heartbeat_event(self._conn, event)
        await self._bus.publish("drops.heartbeat", event)
        log.info("heartbeat.loss", ts_ms=ts_ms)

    async def _emit_recover(self, ts_ms: int, rtt_us: int) -> None:
        duration_ms = (
            ts_ms - self._loss_start_ts_ms if self._loss_start_ts_ms is not None else None
        )
        event = HeartbeatEvent(
            ts_ms=ts_ms,
            event_type=HeartbeatEventType.RECOVER,
            duration_ms=duration_ms,
            missed_count=self._missed_count,
        )
        await queries.insert_heartbeat_event(self._conn, event)
        await self._bus.publish("drops.heartbeat", event)
        log.info("heartbeat.recover", ts_ms=ts_ms, duration_ms=duration_ms, rtt_us=rtt_us)
        self._loss_open = False
        self._loss_start_ts_ms = None
        self._missed_count = 0

    def _parse_target(self) -> tuple[str, int]:
        host, _, port_s = self._cfg.target.rpartition(":")
        return host, int(port_s)

    async def _sleep_or_stop(self, seconds: float) -> None:
        await sleep_or_stop(self._stop, seconds)

    def stop(self) -> None:
        self._stop.set()


async def run_tcp_heartbeat(conn, bus) -> None:
    await TcpHeartbeatWorker(conn, bus).run()
