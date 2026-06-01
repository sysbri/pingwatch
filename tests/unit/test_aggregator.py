from __future__ import annotations

import random
import time

import pytest

from pingwatch.db.connection import open_db
from pingwatch.metrics.aggregator import MetricsAggregator
from pingwatch.models import PingSample


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest.mark.asyncio
async def test_percentiles_within_tolerance(tmp_path):
    random.seed(42)
    async with open_db(tmp_path / "agg.db") as conn:
        agg = MetricsAggregator(conn)
        await agg._refresh_settings()
        now = _now_ms()
        # 1000 OK samples 10..10009 us
        for i in range(1000):
            await agg.ingest(
                PingSample(
                    dest_id=1,
                    ts_ms=now - (1000 - i) * 10,
                    success=True,
                    latency_us=10 + i,
                )
            )
        snap = await agg.snapshot(1)
        # With 1000 reservoir samples drawn from 1000 values, expect ~close to true percentiles.
        assert snap.p50_us is not None
        assert 400 <= snap.p50_us <= 1500   # generous bound
        assert snap.p95_us is not None
        assert snap.p95_us > snap.p50_us
        assert snap.p99_us is not None
        assert snap.p99_us >= snap.p95_us


@pytest.mark.asyncio
async def test_loss_pct_and_jitter(tmp_path):
    async with open_db(tmp_path / "agg.db") as conn:
        agg = MetricsAggregator(conn)
        await agg._refresh_settings()
        now = _now_ms()
        # 80 OK 10ms, 20 losses
        for i in range(100):
            ok = i % 5 != 0
            await agg.ingest(
                PingSample(
                    dest_id=2,
                    ts_ms=now - (100 - i) * 100,
                    success=ok,
                    latency_us=10_000 if ok else None,
                    error_kind=None if ok else "timeout",
                )
            )
        snap = await agg.snapshot(2)
        assert snap.sample_count == 100
        assert 18.0 <= snap.loss_pct_24h <= 22.0
        # All OK samples are 10_000 → jitter mean(|Δ|) = 0
        assert snap.jitter_us == 0


@pytest.mark.asyncio
async def test_sparkline_window(tmp_path):
    async with open_db(tmp_path / "agg.db") as conn:
        agg = MetricsAggregator(conn)
        await agg._refresh_settings()
        now = _now_ms()
        # one old sample (90 min ago) — should be evicted
        await agg.ingest(
            PingSample(dest_id=3, ts_ms=now - 90 * 60 * 1000, success=True, latency_us=15_000)
        )
        # 10 recent samples
        for i in range(10):
            await agg.ingest(
                PingSample(dest_id=3, ts_ms=now - i * 1000, success=True, latency_us=15_000)
            )
        snap = await agg.snapshot(3)
        # Last entry pushes eviction of the 90-min-old one
        assert all(now - t <= 60 * 60 * 1000 for t, _, _ in snap.sparkline)
        assert len(snap.sparkline) == 10


@pytest.mark.asyncio
async def test_all_snapshots(tmp_path):
    async with open_db(tmp_path / "agg.db") as conn:
        agg = MetricsAggregator(conn)
        await agg._refresh_settings()
        now = _now_ms()
        await agg.ingest(PingSample(dest_id=1, ts_ms=now, success=True, latency_us=10_000))
        await agg.ingest(PingSample(dest_id=2, ts_ms=now, success=True, latency_us=20_000))
        snaps = await agg.all_snapshots()
        assert set(snaps.keys()) == {1, 2}
        assert snaps[1].last_latency_us == 10_000
        assert snaps[2].last_latency_us == 20_000
