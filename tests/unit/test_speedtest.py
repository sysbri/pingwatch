"""Tests for streams/speedtest.py – _run_provider lifecycle template."""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest

from pingwatch.streams.speedtest import _run_provider, trickle_should_pause

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _speedtest_row(conn: aiosqlite.Connection, task_id: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT status, error, down_mbps, up_mbps FROM speedtests WHERE task_id = ?",
        (task_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_provider_success_records_done(db: aiosqlite.Connection) -> None:
    """On a successful measure_fn, status is 'done' and DB row is updated."""

    async def measure_fn() -> dict[str, Any]:
        return {
            "down_mbps": 50.0,
            "up_mbps": 10.0,
            "latency_ms": 12.5,
            "jitter_ms": 1.2,
            "bytes_down": 1_000_000,
            "bytes_up": 200_000,
            "server": "fake-server",
        }

    result = await _run_provider(db, "task-ok-1", "fake_provider", "default-server", measure_fn)

    assert result["status"] == "done"
    assert result["task_id"] == "task-ok-1"
    assert result["down_mbps"] == 50.0
    assert result["up_mbps"] == 10.0
    assert result["server"] == "fake-server"
    assert result["error"] is None

    row = await _speedtest_row(db, "task-ok-1")
    assert row is not None
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_run_provider_success_inserts_start_row(db: aiosqlite.Connection) -> None:
    """insert_speedtest_start is called; the row exists even if queried mid-run."""

    started = False

    async def measure_fn() -> dict[str, Any]:
        nonlocal started
        # Verify the row already exists before measure_fn returns.
        row = await _speedtest_row(db, "task-start-check")
        assert row is not None
        started = True
        return {
            "down_mbps": 1.0,
            "up_mbps": 1.0,
            "bytes_down": 0,
            "bytes_up": 0,
        }

    await _run_provider(db, "task-start-check", "fake", "srv", measure_fn)
    assert started


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_provider_exception_records_failed(db: aiosqlite.Connection) -> None:
    """When measure_fn raises, status is 'failed' and error is captured."""

    async def measure_fn() -> dict[str, Any]:
        raise RuntimeError("network error")

    result = await _run_provider(db, "task-fail-1", "fake_provider", "default-server", measure_fn)

    assert result["status"] == "failed"
    assert "network error" in (result["error"] or "")

    row = await _speedtest_row(db, "task-fail-1")
    assert row is not None
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_run_provider_timeout_records_failed(db: aiosqlite.Connection) -> None:
    """TimeoutError is treated as a failure (not a crash)."""

    async def measure_fn() -> dict[str, Any]:
        raise TimeoutError

    result = await _run_provider(db, "task-timeout-1", "fake", "srv", measure_fn)

    assert result["status"] == "failed"
    assert result["error"] == "timeout"


# ---------------------------------------------------------------------------
# _pause_trickle lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_trickle_set_during_and_cleared_after(db: aiosqlite.Connection) -> None:
    """_pause_trickle must be set while measure_fn runs and cleared afterwards."""
    pause_seen_inside = False

    async def measure_fn() -> dict[str, Any]:
        nonlocal pause_seen_inside
        pause_seen_inside = trickle_should_pause()
        return {"down_mbps": 1.0, "up_mbps": 1.0, "bytes_down": 0, "bytes_up": 0}

    await _run_provider(db, "task-pause-1", "fake", "srv", measure_fn)

    assert pause_seen_inside is True
    assert trickle_should_pause() is False


@pytest.mark.asyncio
async def test_pause_trickle_cleared_even_on_failure(db: aiosqlite.Connection) -> None:
    """_pause_trickle must be cleared even when measure_fn raises."""

    async def measure_fn() -> dict[str, Any]:
        raise RuntimeError("boom")

    await _run_provider(db, "task-pause-fail-1", "fake", "srv", measure_fn)

    assert trickle_should_pause() is False


# ---------------------------------------------------------------------------
# trickle_should_pause reflects the module-level event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trickle_should_pause_reflects_pause_event(db: aiosqlite.Connection) -> None:
    """trickle_should_pause() returns True during run and False before/after."""
    from pingwatch.streams import speedtest as st

    # Ensure clean state before test.
    st._pause_trickle.clear()
    assert trickle_should_pause() is False

    observations: list[bool] = []

    async def measure_fn() -> dict[str, Any]:
        observations.append(trickle_should_pause())
        return {"down_mbps": 2.0, "up_mbps": 2.0, "bytes_down": 0, "bytes_up": 0}

    await _run_provider(db, "task-trickle-obs", "fake", "srv", measure_fn)

    assert observations == [True]
    assert trickle_should_pause() is False


# ---------------------------------------------------------------------------
# result dict contains all expected top-level keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_provider_result_shape(db: aiosqlite.Connection) -> None:
    """Result dict must include task_id, status, ts_ms, and payload fields."""

    async def measure_fn() -> dict[str, Any]:
        return {
            "down_mbps": 20.0,
            "up_mbps": 5.0,
            "latency_ms": 8.0,
            "bytes_down": 1024,
            "bytes_up": 512,
        }

    result = await _run_provider(db, "task-shape-1", "fake", "srv", measure_fn)

    for key in ("task_id", "status", "ts_ms", "down_mbps", "up_mbps", "error"):
        assert key in result, f"Missing key: {key}"
