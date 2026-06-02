from __future__ import annotations

import time

import pytest

from pingwatch.db import queries as q
from pingwatch.db.connection import open_db
from pingwatch.metrics.aggregator import MetricsAggregator
from pingwatch.models import FLAG_SPIKE, PingSample


def _now_ms() -> int:
    return int(time.time() * 1000)


# Table-driven cases:
# (description, rolling_p50_us, sample_latency_us, abs_threshold, p50_mult, expected_spike)
CASES = [
    ("absolute threshold trips alone",          30_000,   250_000,  200_000, 3.0, True),
    ("just below absolute threshold no spike",  10_000,   29_999,    80_000, 3.0, False),
    ("multiplier trips spike",                  10_000,   30_001,    80_000, 3.0, True),
    ("just below 3x p50 not a spike",           10_000,   29_999,    80_000, 3.0, False),
    ("low p50, fast sample, no spike",          5_000,    12_000,   80_000, 3.0, False),
    ("low p50, but abs threshold fires",        5_000,    81_000,   80_000, 3.0, True),
    ("changing multiplier 5x not triggered",    10_000,   45_000,   80_000, 5.0, False),
    ("changing multiplier 5x triggered",        10_000,   60_000,   80_000, 5.0, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("desc", "p50_us", "sample_us", "abs_us", "mult", "expected"), CASES)
async def test_spike_detection_table(
    tmp_path, desc, p50_us, sample_us, abs_us, mult, expected
):
    async with open_db(tmp_path / f"sp_{abs(hash(desc))}.db") as conn:
        await q.set_setting(conn, "spike.absolute_threshold_us", abs_us)
        await q.set_setting(conn, "spike.p50_multiplier", mult)

        agg = MetricsAggregator(conn)
        await agg._refresh_settings()

        now = _now_ms()
        # Prime the rolling p50 with 100 OK samples at p50_us.
        for i in range(100):
            await agg.ingest(
                PingSample(
                    dest_id=1,
                    ts_ms=now - (100 - i) * 10,
                    success=True,
                    latency_us=p50_us,
                )
            )

        sample = PingSample(dest_id=1, ts_ms=now, success=True, latency_us=sample_us)
        assert agg.is_spike(1, sample_us) is expected, desc
        flag = agg.flag_for_sample(sample)
        if expected:
            assert flag & FLAG_SPIKE == FLAG_SPIKE
        else:
            assert flag & FLAG_SPIKE == 0


@pytest.mark.asyncio
async def test_no_spike_when_no_rolling_data_below_abs(tmp_path):
    async with open_db(tmp_path / "sp_empty.db") as conn:
        agg = MetricsAggregator(conn)
        await agg._refresh_settings()
        # No prior samples — only absolute threshold can fire.
        assert agg.is_spike(99, 50_000) is False
        assert agg.is_spike(99, 200_000) is True


@pytest.mark.asyncio
async def test_failed_sample_never_spike(tmp_path):
    async with open_db(tmp_path / "sp_fail.db") as conn:
        agg = MetricsAggregator(conn)
        await agg._refresh_settings()
        s = PingSample(dest_id=1, ts_ms=_now_ms(), success=False, error_kind="timeout")
        assert agg.flag_for_sample(s) == 0
