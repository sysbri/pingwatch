"""Dashboard card status: a currently unreachable target must show 'down',
even when its 24h aggregate loss is low (e.g. gateway IP became stale after
moving to a new network)."""
from __future__ import annotations

import time

import aiosqlite
import pytest

from pingwatch.api.routes.dashboard import build_dashboard_payload


async def _seed_pings(
    db: aiosqlite.Connection, dest_id: int, *, ts_from_ms: int, count: int,
    interval_ms: int, success: bool,
) -> None:
    for i in range(count):
        await db.execute(
            "INSERT INTO raw_pings(dest_id, ts_ms, success, latency_us) VALUES (?, ?, ?, ?)",
            (dest_id, ts_from_ms + i * interval_ms, 1 if success else 0,
             1000 if success else None),
        )
    await db.commit()


def _card(payload: dict, dest_id: int) -> dict:
    return next(c for c in payload["cards"] if c["id"] == dest_id)


@pytest.mark.asyncio
async def test_recent_total_loss_is_down_despite_good_day(db: aiosqlite.Connection) -> None:
    now_ms = int(time.time() * 1000)
    # Enough perfect pings that the 24h aggregate loss stays under the 1%
    # flaky threshold (20/3020 = 0.66%) — without the down-rule this card
    # would show the stale "ok" from the original bug report...
    await _seed_pings(db, 1, ts_from_ms=now_ms - 4 * 3_600_000, count=3000,
                      interval_ms=1000, success=True)
    # ...but the last 60s are 100% lost (network changed, gateway gone).
    await _seed_pings(db, 1, ts_from_ms=now_ms - 60_000, count=20,
                      interval_ms=1000, success=False)
    payload = await build_dashboard_payload(db)
    assert _card(payload, 1)["status"] == "down"


@pytest.mark.asyncio
async def test_recent_ok_with_some_day_loss_is_flaky_not_down(db: aiosqlite.Connection) -> None:
    now_ms = int(time.time() * 1000)
    # Lossy earlier today (>1%)...
    await _seed_pings(db, 1, ts_from_ms=now_ms - 4 * 3_600_000, count=50,
                      interval_ms=1000, success=False)
    await _seed_pings(db, 1, ts_from_ms=now_ms - 3 * 3_600_000, count=600,
                      interval_ms=1000, success=True)
    # ...but currently reachable.
    await _seed_pings(db, 1, ts_from_ms=now_ms - 60_000, count=20,
                      interval_ms=1000, success=True)
    payload = await build_dashboard_payload(db)
    assert _card(payload, 1)["status"] == "flaky"


@pytest.mark.asyncio
async def test_clean_target_stays_ok(db: aiosqlite.Connection) -> None:
    now_ms = int(time.time() * 1000)
    await _seed_pings(db, 2, ts_from_ms=now_ms - 120_000, count=100,
                      interval_ms=1000, success=True)
    payload = await build_dashboard_payload(db)
    assert _card(payload, 2)["status"] == "ok"


@pytest.mark.asyncio
async def test_too_few_recent_attempts_does_not_flip_down(db: aiosqlite.Connection) -> None:
    now_ms = int(time.time() * 1000)
    # Good day, only 2 attempts (both failed) in the recent window —
    # not enough evidence to call it down.
    await _seed_pings(db, 2, ts_from_ms=now_ms - 4 * 3_600_000, count=600,
                      interval_ms=1000, success=True)
    await _seed_pings(db, 2, ts_from_ms=now_ms - 30_000, count=2,
                      interval_ms=1000, success=False)
    payload = await build_dashboard_payload(db)
    assert _card(payload, 2)["status"] == "ok"
