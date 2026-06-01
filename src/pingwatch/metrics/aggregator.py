from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field

import aiosqlite
import structlog

from ..bus import Bus, get_bus
from ..db import queries as q
from ..models import FLAG_SPIKE, PingSample

log = structlog.get_logger(__name__)


_SPARKLINE_WINDOW_MS = 60 * 60 * 1000      # 60 min
_RESERVOIR_SIZE_24H = 1000
_RESERVOIR_WINDOW_MS = 24 * 60 * 60 * 1000
_ROLLING_P50_WINDOW = 600                  # last 600 OK samples
_LOSS_24H_WINDOW_MS = _RESERVOIR_WINDOW_MS


def _percentile(sorted_vals: list[int], pct: float) -> int | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return int(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


@dataclass(slots=True)
class DestSnapshot:
    dest_id: int
    last_latency_us: int | None
    last_success: bool
    p50_us: int | None
    p95_us: int | None
    p99_us: int | None
    loss_pct_24h: float
    jitter_us: int | None
    sparkline: list[tuple[int, int | None, bool]]
    rolling_p50_us: int | None
    sample_count: int


@dataclass(slots=True)
class _DestState:
    dest_id: int
    sparkline: deque[tuple[int, int | None, bool]] = field(default_factory=deque)
    reservoir: list[tuple[int, int]] = field(default_factory=list)  # (ts_ms, latency_us)
    reservoir_counter: int = 0
    rolling_ok: deque[int] = field(default_factory=lambda: deque(maxlen=_ROLLING_P50_WINDOW))
    last_ok_latency_us: int | None = None
    last_sample: PingSample | None = None
    losses_24h: deque[int] = field(default_factory=deque)
    totals_24h: deque[int] = field(default_factory=deque)
    jitter_acc: float = 0.0
    jitter_n: int = 0


class MetricsAggregator:
    """Owns rolling per-destination state. Subscribes to samples.all."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: Bus | None = None,
    ) -> None:
        self.conn = conn
        self.bus = bus or get_bus()
        self._state: dict[int, _DestState] = {}
        self._task: asyncio.Task[None] | None = None
        self._spike_abs_us: int = 80_000
        self._spike_p50_mult: float = 3.0
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self._refresh_settings()
        self._task = asyncio.create_task(self._consume(), name="aggregator.consume")
        asyncio.create_task(self._watch_config_changes(), name="aggregator.config_watch")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _refresh_settings(self) -> None:
        self._spike_abs_us = await q.get_setting_typed(
            self.conn, "spike.absolute_threshold_us", 80_000
        )
        self._spike_p50_mult = await q.get_setting_typed(
            self.conn, "spike.p50_multiplier", 3.0
        )

    async def _watch_config_changes(self) -> None:
        async with self.bus.subscribe("config.changed") as queue:
            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                key = msg.get("key", "") if isinstance(msg, dict) else ""
                if key.startswith("spike.") or key.startswith("flaky."):
                    await self._refresh_settings()

    async def _consume(self) -> None:
        async with self.bus.subscribe("samples.all", maxsize=4096) as queue:
            while not self._stop.is_set():
                try:
                    sample = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(sample, PingSample):
                    await self.ingest(sample)

    async def ingest(self, sample: PingSample) -> None:
        async with self._lock:
            self._ingest_sync(sample)

    def _ingest_sync(self, sample: PingSample) -> None:
        st = self._state.get(sample.dest_id)
        if st is None:
            st = _DestState(dest_id=sample.dest_id)
            self._state[sample.dest_id] = st

        # 60-min sparkline
        st.sparkline.append((sample.ts_ms, sample.latency_us, sample.success))
        cutoff_60m = sample.ts_ms - _SPARKLINE_WINDOW_MS
        while st.sparkline and st.sparkline[0][0] < cutoff_60m:
            st.sparkline.popleft()

        # 24h totals/losses for loss_pct + reservoir TTL
        st.totals_24h.append(sample.ts_ms)
        if not sample.success:
            st.losses_24h.append(sample.ts_ms)
        cutoff_24h = sample.ts_ms - _LOSS_24H_WINDOW_MS
        while st.totals_24h and st.totals_24h[0] < cutoff_24h:
            st.totals_24h.popleft()
        while st.losses_24h and st.losses_24h[0] < cutoff_24h:
            st.losses_24h.popleft()

        if sample.success and sample.latency_us is not None:
            # Reservoir sample (Algorithm R)
            st.reservoir_counter += 1
            if len(st.reservoir) < _RESERVOIR_SIZE_24H:
                st.reservoir.append((sample.ts_ms, sample.latency_us))
            else:
                j = random.randint(0, st.reservoir_counter - 1)
                if j < _RESERVOIR_SIZE_24H:
                    st.reservoir[j] = (sample.ts_ms, sample.latency_us)
            # Drop expired entries lazily
            st.reservoir = [(t, v) for t, v in st.reservoir if t >= cutoff_24h]

            # rolling p50 (last 600 OK)
            st.rolling_ok.append(sample.latency_us)

            # jitter (mean |Δ| of consecutive OK pairs)
            if st.last_ok_latency_us is not None:
                st.jitter_acc += abs(sample.latency_us - st.last_ok_latency_us)
                st.jitter_n += 1
            st.last_ok_latency_us = sample.latency_us

        st.last_sample = sample

    def rolling_p50_us(self, dest_id: int) -> int | None:
        st = self._state.get(dest_id)
        if not st or not st.rolling_ok:
            return None
        sorted_vals = sorted(st.rolling_ok)
        return _percentile(sorted_vals, 0.5)

    def is_spike(self, dest_id: int, latency_us: int) -> bool:
        if latency_us is None:
            return False
        if latency_us >= self._spike_abs_us:
            return True
        p50 = self.rolling_p50_us(dest_id)
        if p50 is None or p50 <= 0:
            return False
        return latency_us >= p50 * self._spike_p50_mult

    def flag_for_sample(self, sample: PingSample) -> int:
        if not sample.success or sample.latency_us is None:
            return 0
        return FLAG_SPIKE if self.is_spike(sample.dest_id, sample.latency_us) else 0

    async def snapshot(self, dest_id: int) -> DestSnapshot:
        async with self._lock:
            st = self._state.get(dest_id)
            if st is None:
                return DestSnapshot(
                    dest_id=dest_id,
                    last_latency_us=None,
                    last_success=False,
                    p50_us=None,
                    p95_us=None,
                    p99_us=None,
                    loss_pct_24h=0.0,
                    jitter_us=None,
                    sparkline=[],
                    rolling_p50_us=None,
                    sample_count=0,
                )
            vals = sorted(v for _, v in st.reservoir)
            total = len(st.totals_24h)
            loss_pct = (len(st.losses_24h) / total * 100.0) if total else 0.0
            jitter = int(st.jitter_acc / st.jitter_n) if st.jitter_n else None
            last = st.last_sample
            return DestSnapshot(
                dest_id=dest_id,
                last_latency_us=last.latency_us if last else None,
                last_success=last.success if last else False,
                p50_us=_percentile(vals, 0.5),
                p95_us=_percentile(vals, 0.95),
                p99_us=_percentile(vals, 0.99),
                loss_pct_24h=loss_pct,
                jitter_us=jitter,
                sparkline=list(st.sparkline),
                rolling_p50_us=self.rolling_p50_us(dest_id),
                sample_count=total,
            )

    async def all_snapshots(self) -> dict[int, DestSnapshot]:
        async with self._lock:
            ids = list(self._state.keys())
        out: dict[int, DestSnapshot] = {}
        for dest_id in ids:
            out[dest_id] = await self.snapshot(dest_id)
        return out


def _hour_bucket_ms(ts_ms: int) -> int:
    return (ts_ms // 3_600_000) * 3_600_000


def now_ms() -> int:
    return int(time.time() * 1000)


async def run_aggregator(conn, bus) -> None:
    agg = MetricsAggregator(conn, bus)
    await agg.start()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await agg.stop()
        raise
