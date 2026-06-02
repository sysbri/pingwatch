"""Hot DB snapshot using SQLite's online backup API.

Online backup is the only safe way to copy a WAL-mode SQLite database while
writers are active — file copies of `.db` + `.wal` are racy.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite


async def hot_db_snapshot(conn: aiosqlite.Connection, dst: Path) -> None:
    """Transactionally-consistent copy of `conn`'s DB into `dst`.

    `aiosqlite.Connection.backup(target)` wraps `sqlite3.Connection.backup()`,
    which uses the online backup API. The target is created if missing.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
    if dst.exists():  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
        dst.unlink()  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
    async with aiosqlite.connect(dst) as target:
        await conn.backup(target)
        await target.commit()
