"""Persistent TCP heartbeat to a quasi-always-available endpoint.

Every ``heartbeat.interval_ms`` we attempt a tiny write (16 random bytes)
plus a follow-up read. RTT = time from ``write()`` completion to readback.
A separate tick task detects miss windows -- if ``now - last_hb_ts``
exceeds ``heartbeat.miss_threshold_ms`` we emit a LOSS event; once the
next successful heartbeat lands, we emit RECOVER with duration.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
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
    payload_bytes: int = 16
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 30.0


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
        backoff = self._cfg.backoff_initial_s
        host, port = self._parse_target()
        while not self._stop.is_set():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=5.0,
                )
                backoff = self._cfg.backoff_initial_s
                await self._beat_loop(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat.connect_error", error=str(exc))
                await self._sleep_or_stop(backoff)
                backoff = min(self._cfg.backoff_max_s, backoff * 2)

    async def _beat_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while not self._stop.is_set():
                payload = os.urandom(self._cfg.payload_bytes)
                t0 = time.perf_counter()
                writer.write(payload)
                await writer.drain()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(reader.read(self._cfg.payload_bytes), timeout=0.1)
                t1 = time.perf_counter()
                now_ms = int(time.time() * 1000)
                self._last_hb_ts_ms = now_ms
                if self._loss_open:
                    await self._emit_recover(now_ms, int((t1 - t0) * 1_000_000))
                await asyncio.sleep(self._cfg.interval_ms / 1000)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

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
