"""Tests for shared utilities from R2: util.sleep_or_stop, metrics._math, api/deps.RANGE_TO_MS."""

from __future__ import annotations

import asyncio
import time

import pytest

from pingwatch.api.deps import RANGE_TO_MS
from pingwatch.metrics._math import hour_bucket_ms, percentile
from pingwatch.util import sleep_or_stop

# ---------------------------------------------------------------------------
# util.sleep_or_stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_or_stop_returns_immediately_when_stop_already_set() -> None:
    """If the stop event is already set, sleep_or_stop must return without delay."""
    stop = asyncio.Event()
    stop.set()
    t0 = time.monotonic()
    await sleep_or_stop(stop, 60.0)  # Would take 60 s if stop weren't set
    elapsed = time.monotonic() - t0
    # Must complete well under 1 second
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_sleep_or_stop_waits_when_stop_not_set() -> None:
    """If stop is never set, sleep_or_stop sleeps for ~the requested duration."""
    stop = asyncio.Event()
    t0 = time.monotonic()
    await sleep_or_stop(stop, 0.05)  # 50 ms tiny timeout
    elapsed = time.monotonic() - t0
    # Must have waited at least some fraction of the 50 ms
    assert elapsed >= 0.04


@pytest.mark.asyncio
async def test_sleep_or_stop_stop_set_mid_sleep() -> None:
    """Cancels early when stop is set from another coroutine mid-sleep."""
    stop = asyncio.Event()
    results: list[float] = []

    async def setter() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    t0 = time.monotonic()
    setter_task = asyncio.create_task(setter())
    await sleep_or_stop(stop, 60.0)
    elapsed = time.monotonic() - t0
    results.append(elapsed)
    await setter_task

    # Should complete around 50 ms, definitely not 60 s
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# metrics._math.percentile
# ---------------------------------------------------------------------------


def test_percentile_empty_returns_none() -> None:
    assert percentile([], 0.5) is None


def test_percentile_single_element_returns_that_element() -> None:
    assert percentile([42], 0.0) == 42
    assert percentile([42], 0.5) == 42
    assert percentile([42], 1.0) == 42


def test_percentile_p50_of_sorted_list() -> None:
    vals = list(range(1, 11))  # 1..10
    result = percentile(vals, 0.5)
    assert result is not None
    assert 5 <= result <= 6


def test_percentile_p0_is_minimum() -> None:
    vals = [10, 20, 30, 40, 50]
    assert percentile(vals, 0.0) == 10


def test_percentile_p100_is_maximum() -> None:
    vals = [10, 20, 30, 40, 50]
    assert percentile(vals, 1.0) == 50


def test_percentile_p95_of_known_distribution() -> None:
    # 100 elements 1..100; p95 should be near 95
    vals = list(range(1, 101))
    result = percentile(vals, 0.95)
    assert result is not None
    assert 94 <= result <= 96


def test_percentile_two_elements_interpolates() -> None:
    # p50 of [0, 100] → 50
    vals = [0, 100]
    result = percentile(vals, 0.5)
    assert result == 50


def test_percentile_three_elements_p50() -> None:
    vals = [1, 2, 3]
    result = percentile(vals, 0.5)
    assert result == 2


# ---------------------------------------------------------------------------
# metrics._math.hour_bucket_ms
# ---------------------------------------------------------------------------


def test_hour_bucket_ms_on_exact_hour() -> None:
    # 1 hour in ms
    one_hour_ms = 3_600_000
    assert hour_bucket_ms(one_hour_ms) == one_hour_ms


def test_hour_bucket_ms_rounds_down() -> None:
    # 1.5 hours in ms → bucket is the 1-hour mark
    ts = 3_600_000 + 1_800_000
    assert hour_bucket_ms(ts) == 3_600_000


def test_hour_bucket_ms_zero() -> None:
    assert hour_bucket_ms(0) == 0


def test_hour_bucket_ms_late_in_hour() -> None:
    # 2 hours + 59 minutes → bucket is the 2-hour mark
    ts = 2 * 3_600_000 + 59 * 60_000
    assert hour_bucket_ms(ts) == 2 * 3_600_000


def test_hour_bucket_ms_is_multiple_of_hour() -> None:
    ts = 7_654_321
    bucket = hour_bucket_ms(ts)
    assert bucket % 3_600_000 == 0
    assert bucket <= ts


# ---------------------------------------------------------------------------
# api/deps.RANGE_TO_MS
# ---------------------------------------------------------------------------


def test_range_to_ms_has_required_keys() -> None:
    for key in ("1h", "24h", "7d", "30d", "all"):
        assert key in RANGE_TO_MS, f"Missing key: {key}"


def test_range_to_ms_values_are_positive_ints() -> None:
    for key, val in RANGE_TO_MS.items():
        assert isinstance(val, int), f"{key}: expected int, got {type(val)}"
        assert val > 0, f"{key}: value must be positive"


def test_range_to_ms_1h_is_3600000() -> None:
    assert RANGE_TO_MS["1h"] == 3_600_000


def test_range_to_ms_24h_is_86400000() -> None:
    assert RANGE_TO_MS["24h"] == 86_400_000


def test_range_to_ms_7d_is_7_times_24h() -> None:
    assert RANGE_TO_MS["7d"] == 7 * 86_400_000


def test_range_to_ms_30d_is_30_times_24h() -> None:
    assert RANGE_TO_MS["30d"] == 30 * 86_400_000


def test_range_to_ms_ordering() -> None:
    """Longer ranges must map to larger values."""
    assert RANGE_TO_MS["1h"] < RANGE_TO_MS["24h"]
    assert RANGE_TO_MS["24h"] < RANGE_TO_MS["7d"]
    assert RANGE_TO_MS["7d"] < RANGE_TO_MS["30d"]
    assert RANGE_TO_MS["30d"] <= RANGE_TO_MS["all"]
