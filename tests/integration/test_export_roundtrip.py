"""End-to-end ZIP export round trip from a seeded DB."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import aiosqlite
import pytest

from pingwatch.export.zip_bundle import build_export_zip


@pytest.mark.asyncio
async def test_export_zip_roundtrip(db: aiosqlite.Connection, tmp_export_dir: Path) -> None:
    # Seed a few rows.
    now_ms = 1_700_000_000_000
    await db.execute(
        "INSERT INTO raw_pings(dest_id, ts_ms, success, latency_us) VALUES (?,?,?,?)",
        (1, now_ms, 1, 12000),
    )
    await db.execute(
        "INSERT INTO raw_pings(dest_id, ts_ms, success, latency_us) VALUES (?,?,?,?)",
        (1, now_ms + 1000, 0, None),
    )
    await db.execute(
        "INSERT INTO outages(dest_id_primary, start_ts_ms, end_ts_ms, duration_ms, "
        "lost_count, type) VALUES (?,?,?,?,?,?)",
        (1, now_ms, now_ms + 2000, 2000, 2, "EINZEL"),
    )
    await db.commit()

    zip_path = await build_export_zip(db, since_ms=now_ms - 1000, until_ms=now_ms + 10000)
    assert zip_path.exists()

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "destinations.csv" in names
        assert "outages.csv" in names
        assert "traces.csv" in names
        assert "all.json" in names
        assert "config.json" in names
        assert "manifest.json" in names

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["kind"] == "pingwatch-export"

        all_json = json.loads(zf.read("all.json"))
        assert len(all_json["destinations"]) >= 1
        assert len(all_json["outages"]) == 1
        assert all_json["outages"][0]["type"] == "EINZEL"

        outages_csv = zf.read("outages.csv").decode()
        assert "EINZEL" in outages_csv

    zip_path.unlink()
