"""Periodic retention purge for raw and aggregate tables.

Every 10 minutes we walk each retained table, deleting rows older than
the configured cutoff in 50k-row chunks with brief yields between chunks
so we never block the event loop for long. After deletes we run
``PRAGMA incremental_vacuum`` to actually shrink the file. A separate
5-minute task runs ``PRAGMA wal_checkpoint(TRUNCATE)``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

CHUNK_SIZE = 50_000
PURGE_INTERVAL_S = 600
CHECKPOINT_INTERVAL_S = 300

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
    _Table("wifi_events", "ts_ms", "retention.wifi_rssi_days", 180),
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
        ckpt_task = asyncio.create_task(self._checkpoint_loop())
        try:
            await self._stop.wait()
        finally:
            purge_task.cancel()
            ckpt_task.cancel()

    async def _purge_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.purge_once()
            except Exception:  # noqa: BLE001
                log.exception("retention.purge_failed")
            await self._sleep_or_stop(PURGE_INTERVAL_S)

    async def _checkpoint_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await self._conn.commit()
            except Exception:  # noqa: BLE001
                log.exception("retention.checkpoint_failed")
            await self._sleep_or_stop(CHECKPOINT_INTERVAL_S)

    async def purge_once(self) -> None:
        now_ms = int(time.time() * 1000)
        for table in _TABLES:
            days = await self._read_days(table.setting_key, table.default_days)
            cutoff = self._cutoff_for(table, now_ms, days)
            deleted_total = await self._delete_chunked(table.name, table.ts_column, cutoff)
            if deleted_total:
                log.info(
                    "retention.deleted",
                    table=table.name,
                    rows=deleted_total,
                    days=days,
                )
        try:
            await self._conn.execute("PRAGMA incremental_vacuum(2000)")
            await self._conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("retention.vacuum_failed")

    async def _delete_chunked(self, table: str, column: str, cutoff: int) -> int:
        deleted_total = 0
        while not self._stop.is_set():
            cur = await self._conn.execute(
                f"DELETE FROM {table} "
                f"WHERE rowid IN (SELECT rowid FROM {table} WHERE {column} < ? LIMIT ?)",
                (cutoff, CHUNK_SIZE),
            )
            await self._conn.commit()
            rows = cur.rowcount or 0
            await cur.close()
            deleted_total += rows
            if rows < CHUNK_SIZE:
                break
            await asyncio.sleep(0)
        return deleted_total

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
    def _cutoff_for(table: _Table, now_ms: int, days: int) -> int:
        if table.ts_column == "hour_bucket":
            return (now_ms // 3_600_000) - days * 24
        if table.ts_column == "day_bucket":
            return (now_ms // DAY_MS) - days
        return now_ms - days * DAY_MS

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()


async def run_retention(conn) -> None:
    await RetentionWorker(conn).run()
