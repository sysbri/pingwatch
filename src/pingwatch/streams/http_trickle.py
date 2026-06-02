"""Persistent throttled HTTP download used as a "live traffic" canary.

Pulls from a Cloudflare endpoint (or any configured URL), throttling chunk
consumption with sleep-based pacing so the long-term throughput hovers
around ``stream.target_kbps`` (default 20 KB/s). Every 1 s of wall-clock
time we publish a throughput sample on the bus and persist it. A run of
``stream.zero_kbps_min_samples`` consecutive zero-kbps samples is treated
as a stream drop -- a STREAM outage is opened and a DROP event recorded.

The worker stops cleanly at midnight local time when the daily download
budget (``stream.daily_cap_mb``) is exhausted; it resumes the next day.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import zoneinfo
from dataclasses import dataclass, field

import httpx
import structlog

from pingwatch.bus import Bus, get_bus
from pingwatch.db import queries
from pingwatch.models import OutageType, StreamEvent, StreamEventType, StreamSample
from pingwatch.streams.speedtest import trickle_should_pause
from pingwatch.util import sleep_or_stop

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class StreamConfig:
    endpoint: str = "https://speed.cloudflare.com/__down?bytes=99999999"
    target_kbps: int = 20
    daily_cap_mb: int = 2048
    zero_kbps_min_samples: int = 3
    timezone: str = "Europe/Berlin"
    chunk_size: int = 4096
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 30.0


@dataclass(slots=True)
class _WindowState:
    window_start_ms: int = 0
    bytes_in_window: int = 0
    bytes_total: int = 0
    zero_run: int = 0
    open_drop_outage_id: int | None = None
    last_kbps: int = 0
    drop_start_ts_ms: int | None = None
    drop_kbps_before: int | None = None
    samples: list[StreamSample] = field(default_factory=list)


class HttpTrickleWorker:
    def __init__(
        self,
        conn: object,
        bus: Bus | None = None,
        config: StreamConfig | None = None,
    ) -> None:
        self._conn = conn
        self._bus = bus or get_bus()
        self._cfg = config or StreamConfig()
        self._stop = asyncio.Event()
        self._was_connected: bool = False
        self._last_disconnect_ts_ms: int | None = None

    async def run(self) -> None:
        backoff = self._cfg.backoff_initial_s
        while not self._stop.is_set():
            if await self._budget_exceeded():
                wait_s = self._seconds_until_midnight()
                log.info("stream.budget_reached", wait_s=wait_s)
                await self._sleep_or_stop(wait_s)
                continue
            try:
                await self._stream_once()
                backoff = self._cfg.backoff_initial_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("stream.error", error=str(exc))
                self._was_connected = False
                self._last_disconnect_ts_ms = int(time.time() * 1000)
                await self._emit_drop(reason=str(exc))
                await self._sleep_or_stop(backoff)
                backoff = min(self._cfg.backoff_max_s, backoff * 2)

    async def _stream_once(self) -> None:
        cfg = self._cfg
        target_bytes_per_s = cfg.target_kbps * 1024
        chunk = cfg.chunk_size
        chunk_period_s = chunk / target_bytes_per_s if target_bytes_per_s > 0 else 0.1
        state = _WindowState(window_start_ms=int(time.time() * 1000))
        headers = {"User-Agent": "PingWatch/0.1 (+https://github.com/local/pingwatch)"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0), headers=headers
        ) as client, client.stream("GET", cfg.endpoint) as response:
            response.raise_for_status()
            if not self._was_connected:
                if self._last_disconnect_ts_ms is None:
                    await self._publish_event(StreamEventType.CONNECT)
                else:
                    gap_ms = int(time.time() * 1000) - self._last_disconnect_ts_ms
                    if gap_ms > 2000:
                        await self._publish_event(StreamEventType.RECONNECT)
                self._was_connected = True
            async for piece in response.aiter_bytes(chunk_size=chunk):
                if self._stop.is_set():
                    return
                if trickle_should_pause():
                    await asyncio.sleep(0.5)
                    self._was_connected = False
                    continue
                state.bytes_in_window += len(piece)
                state.bytes_total += len(piece)
                now_ms = int(time.time() * 1000)
                while now_ms - state.window_start_ms >= 1000:
                    await self._flush_window(state, state.window_start_ms + 1000)
                    state.window_start_ms += 1000
                    if await self._budget_exceeded():
                        log.info("stream.budget_reached_mid_stream")
                        return
                await asyncio.sleep(chunk_period_s)

    async def _flush_window(self, state: _WindowState, window_end_ms: int) -> None:
        kbps = state.bytes_in_window // 1024
        sample = StreamSample(
            ts_ms=window_end_ms,
            kbps=kbps,
            tcp_rtt_us=None,
            bytes_total=state.bytes_total,
        )
        await queries.insert_throughput_sample(
            self._conn,
            sample.ts_ms,
            sample.kbps,
            sample.tcp_rtt_us,
            sample.bytes_total,
        )
        await self._bus.publish("stream.samples", sample)
        if kbps == 0:
            state.zero_run += 1
            if (state.zero_run >= self._cfg.zero_kbps_min_samples
                    and state.open_drop_outage_id is None):
                state.drop_start_ts_ms = window_end_ms
                state.drop_kbps_before = state.last_kbps
                state.open_drop_outage_id = await self._open_stream_outage(window_end_ms)
                await self._publish_event(
                    StreamEventType.DROP,
                    ts_ms=window_end_ms,
                    kbps_before=state.last_kbps,
                    kbps_after=0,
                )
        else:
            if state.open_drop_outage_id is not None and state.drop_start_ts_ms is not None:
                duration_ms = window_end_ms - state.drop_start_ts_ms
                await queries.close_outage(
                    self._conn,
                    state.open_drop_outage_id,
                    window_end_ms,
                )
                await self._publish_event(
                    StreamEventType.THROUGHPUT_RECOVER,
                    ts_ms=window_end_ms,
                    duration_ms=duration_ms,
                    kbps_before=state.drop_kbps_before,
                    kbps_after=kbps,
                )
                state.open_drop_outage_id = None
                state.drop_start_ts_ms = None
                state.drop_kbps_before = None
            state.zero_run = 0
        state.last_kbps = kbps
        state.bytes_in_window = 0

    async def _publish_event(
        self,
        event_type: StreamEventType,
        ts_ms: int | None = None,
        duration_ms: int | None = None,
        kbps_before: int | None = None,
        kbps_after: int | None = None,
        reason: str | None = None,
    ) -> None:
        ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
        event = StreamEvent(
            ts_ms=ts,
            event_type=event_type,
            duration_ms=duration_ms,
            kbps_before=kbps_before,
            kbps_after=kbps_after,
            reason=reason,
        )
        await queries.insert_stream_event(self._conn, event)
        topic = "drops.stream" if event_type == StreamEventType.DROP else "stream.samples"
        await self._bus.publish(topic, event)

    async def _open_stream_outage(self, ts_ms: int) -> int:
        outage_id = await queries.open_outage(
            self._conn,
            dest_id=None,
            start_ts_ms=ts_ms,
            type_=OutageType.STREAM,
            lost_count=0,
        )
        log.info("stream.outage_opened", outage_id=outage_id, ts_ms=ts_ms)
        return outage_id

    async def _emit_drop(self, reason: str) -> None:
        try:
            ts = int(time.time() * 1000)
            await self._publish_event(StreamEventType.DROP, ts_ms=ts, reason=reason)
        except Exception:  # noqa: BLE001
            log.exception("stream.emit_drop_failed")

    async def _budget_exceeded(self) -> bool:
        cap_bytes = self._cfg.daily_cap_mb * 1024 * 1024
        try:
            used = await queries.bytes_today(self._conn)
        except Exception:  # noqa: BLE001
            return False
        return used >= cap_bytes

    def _seconds_until_midnight(self) -> float:
        tz = zoneinfo.ZoneInfo(self._cfg.timezone)
        now = dt.datetime.now(tz=tz)
        tomorrow = (now + dt.timedelta(days=1)).date()
        midnight = dt.datetime.combine(tomorrow, dt.time(0, 0), tzinfo=tz)
        return max(1.0, (midnight - now).total_seconds())

    async def _sleep_or_stop(self, seconds: float) -> None:
        await sleep_or_stop(self._stop, seconds)

    def stop(self) -> None:
        self._stop.set()


async def run_http_trickle(conn, bus) -> None:
    """Worker entry point. Builds StreamConfig from DB settings so the UI
    can edit endpoint / target_kbps / daily_cap_mb at runtime."""
    from pingwatch.db import queries

    cfg = StreamConfig()
    cfg.endpoint = await queries.get_setting_typed(conn, "stream.endpoint", cfg.endpoint)
    cfg.target_kbps = await queries.get_setting_typed(conn, "stream.target_kbps", cfg.target_kbps)
    cfg.daily_cap_mb = await queries.get_setting_typed(
        conn, "stream.daily_cap_mb", cfg.daily_cap_mb,
    )
    cfg.zero_kbps_min_samples = await queries.get_setting_typed(
        conn, "stream.zero_kbps_min_samples", cfg.zero_kbps_min_samples
    )
    await HttpTrickleWorker(conn, bus, cfg).run()
