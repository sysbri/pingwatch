"""Shared pytest fixtures for PingWatch tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest_asyncio

# Ensure config defaults point to writable paths even before app boot.
os.environ.setdefault("PINGWATCH_DB", str(Path(tempfile.gettempdir()) / "pingwatch-test.db"))
os.environ.setdefault("PINGWATCH_CONFIG", str(Path(tempfile.gettempdir()) / "pingwatch-test.yaml"))

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "src" / "pingwatch" / "db" / "schema.sql"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    """In-memory aiosqlite connection with schema applied + seeded destinations."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    schema = SCHEMA_FILE.read_text()
    await conn.executescript(schema)

    # Minimal seed for tests.
    await conn.execute(
        "INSERT INTO destinations(id,name,address,type,kind,interval_ms,timeout_ms,ordering) "
        "VALUES (1,'Gateway','192.168.1.1','ICMP','gateway',1000,2000,0)"
    )
    await conn.execute(
        "INSERT INTO destinations(id,name,address,type,kind,interval_ms,timeout_ms,ordering) "
        "VALUES (2,'Cloudflare','1.1.1.1','ICMP','external',1000,2000,1)"
    )
    await conn.commit()
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def tmp_export_dir(tmp_path: Path) -> Path:
    p = tmp_path / "exports"
    p.mkdir()
    return p
