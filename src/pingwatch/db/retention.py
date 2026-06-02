"""Periodic retention purge for raw and aggregate tables.

Every 10 minutes we delete rows older than the configured cutoff from each
retained table, then run ``PRAGMA incremental_vacuum`` to shrink the file.

Retention runs on its OWN database connection so its bulk DELETEs / VACUUM
don't interleave with the shared connection's high-frequency writes
(``busy_timeout`` lets a write wait its turn).

We deliberately do NOT issue an explicit ``wal_checkpoint``: connections are
opened with ``wal_autocheckpoint = 4000`` (see db/connection.py), so SQLite
checkpoints the WAL automatically and keeps it bounded. An explicit checkpoint
here only ever raised SQLITE_LOCKED ("database table is locked").
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiosqlite
import structlog

from pingwatch.util import sleep_or_stop

log = structlog.get_logger(__name__)

PURGE_INTERVAL_S = 600

DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class _Table:
    name: str
    ts_column: str
    setting_key: str
    default_days: int


_TABLES: tuple[_Table, ...] = (
    _Table("raw_pings", "ts_ms", "retention.raw_pings_days", 7),
    _Table("hourly_aggregates", "hour_bucket", "retention.hourly_aggregates_days", 90),
    _Table("daily_aggregates", "day_bucket", "retention.daily_aggregates_days", 730),
    _Table("outages", "start_ts_ms", "retention.outages_days", 180),
    _Table("traceroutes", "ts_ms", "retention.traces_days", 30),
    _Table("wifi_rssi_samples", "ts_ms", "retention.wifi_rssi_days", 7),
    _Table("wifi_events", "ts_ms", "retention.wifi_events_days", 180),
    _Table("stream_throughput_samples", "ts_ms", "retention.stream_samples_days", 7),
    _Table("stream_events", "ts_ms", "retention.stream_samples_days", 180),
    _Table("heartbeat_events", "ts_ms", "retention.heartbeat_days", 30),
    _Table("notifications", "ts_ms", "retention.notifications_days", 30),
)


class RetentionWorker:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._stop = asyncio.Event()

    async def run(self) -> None:
        purge_task = asyncio.create_task(self._purge_loop())
        try:
            await self._stop.wait()
        finally:
            purge_task.cancel()

    async def _purge_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.purge_once()
            except Exception:  # noqa: BLE001
                log.exception("retention.purge_failed")
            await self._sleep_or_stop(PURGE_INTERVAL_S)

    async def purge_once(self) -> None:
        now_ms = int(time.time() * 1000)
        for table in _TABLES:
            days = await self._read_days(table.setting_key, table.default_days)
            cutoff = self._cutoff_for(now_ms, days)
            deleted = await self._delete_expired(table.name, table.ts_column, cutoff)
            if deleted:
                log.info("retention.deleted", table=table.name, rows=deleted, days=days)
        try:
            await self._conn.execute("PRAGMA incremental_vacuum(2000)")
            await self._conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("retention.vacuum_failed")

    async def _delete_expired(self, table: str, column: str, cutoff: int) -> int:
        # Delete directly by the timestamp column. Works for both rowid and
        # WITHOUT ROWID tables (the previous rowid-subquery raised
        # "no such column: rowid" on WITHOUT ROWID tables). aiosqlite runs the
        # statement off the event loop, so a single DELETE is fine.
        cur = await self._conn.execute(
            f"DELETE FROM {table} WHERE {column} < ?",  # noqa: S608  # internal constant identifiers
            (cutoff,),
        )
        await self._conn.commit()
        deleted = cur.rowcount or 0
        await cur.close()
        return deleted

    async def _read_days(self, key: str, default: int) -> int:
        try:
            cur = await self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
            await cur.close()
        except Exception:  # noqa: BLE001
            return default
        if row is None:
            return default
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _cutoff_for(now_ms: int, days: int) -> int:
        # Every retained timestamp column (ts_ms, and the hour_bucket/day_bucket
        # aggregate keys) is stored in ms (see metrics._math.hour_bucket_ms), so
        # a single ms cutoff is correct for all tables.
        return now_ms - days * DAY_MS

    async def _sleep_or_stop(self, seconds: float) -> None:
        await sleep_or_stop(self._stop, seconds)

    def stop(self) -> None:
        self._stop.set()


async def run_retention(conn) -> None:  # noqa: ARG001  # retention opens its own connection
    """Run retention on a dedicated connection (see module docstring)."""
    from pingwatch.config import get_settings

    settings = get_settings()
    own = await aiosqlite.connect(str(settings.paths.db))
    own.row_factory = aiosqlite.Row
    await own.execute("PRAGMA busy_timeout = 5000")
    await own.execute("PRAGMA foreign_keys = ON")
    try:
        await RetentionWorker(own).run()
    finally:
        await own.close()
