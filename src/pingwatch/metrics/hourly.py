from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import structlog

from ..bus import Bus, get_bus
from ..db import queries as q
from ..models import FLAG_SPIKE, PingSample

log = structlog.get_logger(__name__)


def _hour_bucket(ts_ms: int) -> int:
    return (ts_ms // 3_600_000) * 3_600_000


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
class _HourBucket:
    dest_id: int
    hour_bucket: int
    count_total: int = 0
    count_ok: int = 0
    count_spike: int = 0
    latencies: list[int] = field(default_factory=list)
    min_us: int | None = None
    max_us: int | None = None
    sum_us: int = 0
    jitter_acc: float = 0.0
    jitter_n: int = 0
    last_ok_latency_us: int | None = None

    def add(self, success: bool, latency_us: int | None, is_spike: bool) -> None:
        self.count_total += 1
        if success:
            self.count_ok += 1
            if is_spike:
                self.count_spike += 1
            if latency_us is not None:
                self.latencies.append(latency_us)
                self.sum_us += latency_us
                self.min_us = latency_us if self.min_us is None else min(self.min_us, latency_us)
                self.max_us = latency_us if self.max_us is None else max(self.max_us, latency_us)
                if self.last_ok_latency_us is not None:
                    self.jitter_acc += abs(latency_us - self.last_ok_latency_us)
                    self.jitter_n += 1
                self.last_ok_latency_us = latency_us

    def to_row(self, finalized: bool) -> dict[str, object]:
        sorted_lat = sorted(self.latencies)
        loss_pct = (
            (self.count_total - self.count_ok) / self.count_total * 100.0
            if self.count_total
            else 0.0
        )
        mean_us = int(self.sum_us / len(sorted_lat)) if sorted_lat else None
        return {
            "dest_id": self.dest_id,
            "hour_bucket": self.hour_bucket,
            "count_total": self.count_total,
            "count_ok": self.count_ok,
            "count_spike": self.count_spike,
            "p50_us": _percentile(sorted_lat, 0.50),
            "p95_us": _percentile(sorted_lat, 0.95),
            "p99_us": _percentile(sorted_lat, 0.99),
            "min_us": self.min_us,
            "max_us": self.max_us,
            "mean_us": mean_us,
            "loss_pct": loss_pct,
            "jitter_us": int(self.jitter_acc / self.jitter_n) if self.jitter_n else None,
            "outage_count": 0,
            "outage_ms": 0,
            "finalized": 1 if finalized else 0,
        }


class HourlyRollupWorker:
    """Maintains per-(dest_id, hour) in-memory buckets, flushing every 60s.

    On boot, rebuilds the current-hour bucket from raw_pings since the last finalized hour.
    """

    FLUSH_INTERVAL_S = 60

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: Bus | None = None,
        timezone: str = "Europe/Berlin",
    ) -> None:
        self.conn = conn
        self.bus = bus or get_bus()
        self._buckets: dict[tuple[int, int], _HourBucket] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._tz = ZoneInfo(timezone)
        self._last_day_rollover: int | None = None

    async def start(self) -> None:
        await self._boot_recovery()
        self._task = asyncio.create_task(self._run(), name="hourly_rollup")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._flush(finalize_old=True)

    async def _boot_recovery(self) -> None:
        dests = await q.list_destinations(self.conn, enabled_only=False)
        now = int(time.time() * 1000)
        cur_hour = _hour_bucket(now)
        for d in dests:
            last_final = await q.latest_finalized_hour(self.conn, d.id)
            start_ms = last_final + 3_600_000 if last_final is not None else cur_hour
            cur = await self.conn.execute(
                """
                SELECT ts_ms, success, latency_us, flags FROM raw_pings
                WHERE dest_id = ? AND ts_ms >= ? ORDER BY ts_ms ASC
                """,
                (d.id, start_ms),
            )
            rows = await cur.fetchall()
            await cur.close()
            for r in rows:
                hb = _hour_bucket(r["ts_ms"])
                key = (d.id, hb)
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = _HourBucket(dest_id=d.id, hour_bucket=hb)
                    self._buckets[key] = bucket
                bucket.add(
                    success=bool(r["success"]),
                    latency_us=r["latency_us"],
                    is_spike=bool(r["flags"] & FLAG_SPIKE),
                )

    def ingest(self, sample: PingSample, is_spike: bool) -> None:
        hb = _hour_bucket(sample.ts_ms)
        key = (sample.dest_id, hb)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _HourBucket(dest_id=sample.dest_id, hour_bucket=hb)
            self._buckets[key] = bucket
        bucket.add(sample.success, sample.latency_us, is_spike)

    async def _run(self) -> None:
        async with self.bus.subscribe("samples.all", maxsize=4096) as queue:
            next_flush = time.monotonic() + self.FLUSH_INTERVAL_S
            while not self._stop.is_set():
                timeout = max(0.05, next_flush - time.monotonic())
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                    if isinstance(msg, PingSample):
                        # Spike flag may have been set by persister; reading flags off raw_pings
                        # isn't possible mid-flight, so treat as non-spike here.
                        self.ingest(msg, is_spike=False)
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() >= next_flush:
                    await self._flush()
                    await self._maybe_day_rollover()
                    next_flush = time.monotonic() + self.FLUSH_INTERVAL_S

    async def _flush(self, finalize_old: bool = False) -> None:
        now = int(time.time() * 1000)
        cur_hour = _hour_bucket(now)
        keys = list(self._buckets.keys())
        for key in keys:
            bucket = self._buckets[key]
            is_old = bucket.hour_bucket < cur_hour
            finalized = is_old or finalize_old
            await q.upsert_hourly_aggregate(self.conn, bucket.to_row(finalized=finalized))
            if finalized:
                self._buckets.pop(key, None)

    async def _maybe_day_rollover(self) -> None:
        now_local = datetime.now(self._tz)
        today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_ms = int(today_start_local.timestamp() * 1000)
        if self._last_day_rollover is None:
            self._last_day_rollover = today_start_ms
            return
        if today_start_ms <= self._last_day_rollover:
            return
        # Rollup yesterday.
        yesterday_start = today_start_local - timedelta(days=1)
        ystart_ms = int(yesterday_start.timestamp() * 1000)
        yend_ms = today_start_ms
        dests = await q.list_destinations(self.conn, enabled_only=False)
        for d in dests:
            hourly = await q.list_hourly_aggregates(self.conn, d.id, ystart_ms, yend_ms)
            if not hourly:
                continue
            tot = sum(h["count_total"] for h in hourly)
            ok = sum(h["count_ok"] for h in hourly)
            spike = sum(h["count_spike"] for h in hourly)
            mins = [h["min_us"] for h in hourly if h["min_us"] is not None]
            maxs = [h["max_us"] for h in hourly if h["max_us"] is not None]
            means = [h["mean_us"] for h in hourly if h["mean_us"] is not None]
            p50s = [h["p50_us"] for h in hourly if h["p50_us"] is not None]
            p95s = [h["p95_us"] for h in hourly if h["p95_us"] is not None]
            p99s = [h["p99_us"] for h in hourly if h["p99_us"] is not None]
            jits = [h["jitter_us"] for h in hourly if h["jitter_us"] is not None]
            loss_pct = ((tot - ok) / tot * 100.0) if tot else 0.0
            await q.upsert_daily_aggregate(
                self.conn,
                {
                    "dest_id": d.id,
                    "day_bucket": ystart_ms,
                    "count_total": tot,
                    "count_ok": ok,
                    "count_spike": spike,
                    "p50_us": int(sum(p50s) / len(p50s)) if p50s else None,
                    "p95_us": max(p95s) if p95s else None,
                    "p99_us": max(p99s) if p99s else None,
                    "min_us": min(mins) if mins else None,
                    "max_us": max(maxs) if maxs else None,
                    "mean_us": int(sum(means) / len(means)) if means else None,
                    "loss_pct": loss_pct,
                    "jitter_us": int(sum(jits) / len(jits)) if jits else None,
                    "outage_count": sum(h["outage_count"] for h in hourly),
                    "outage_ms": sum(h["outage_ms"] for h in hourly),
                },
            )
        self._last_day_rollover = today_start_ms


async def run_hourly_rollup(conn, bus) -> None:
    w = HourlyRollupWorker(conn, bus)
    await w.start()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await w.stop()
        raise
