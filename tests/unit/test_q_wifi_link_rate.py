from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.db import queries


@pytest.mark.asyncio
async def test_link_rate_series_skips_nulls(db: aiosqlite.Connection) -> None:
    await queries.insert_rssi_sample(db, 1000, -50, 36, 144_000)
    await queries.insert_rssi_sample(db, 2000, -52, 36, None)   # no link rate
    await queries.insert_rssi_sample(db, 3000, -48, 36, 86_000)
    series = await queries.link_rate_series(db, since_ms=0)
    assert series == [(1000, 144_000), (3000, 86_000)]
