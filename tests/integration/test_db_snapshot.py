"""hot_db_snapshot produces a valid SQLite file with rows intact."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from pingwatch.export.db_snapshot import hot_db_snapshot


@pytest.mark.asyncio
async def test_hot_snapshot_preserves_rows(db: aiosqlite.Connection, tmp_path: Path) -> None:
    await db.execute(
        "INSERT INTO raw_pings(dest_id, ts_ms, success, latency_us) VALUES (?,?,?,?)",
        (1, 1_700_000_000_000, 1, 11111),
    )
    await db.commit()

    dst = tmp_path / "snap.db"
    await hot_db_snapshot(db, dst)
    assert dst.exists() and dst.stat().st_size > 0

    async with aiosqlite.connect(dst) as ro:
        cur = await ro.execute("SELECT COUNT(*) FROM raw_pings")
        row = await cur.fetchone()
        assert row is not None and row[0] == 1

        cur = await ro.execute("SELECT name FROM destinations WHERE id=1")
        row = await cur.fetchone()
        assert row is not None and row[0] == "Gateway"
