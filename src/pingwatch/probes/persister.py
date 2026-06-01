from __future__ import annotations

import asyncio
import time

import aiosqlite
import structlog

from ..bus import Bus, get_bus
from ..models import PingSample

log = structlog.get_logger(__name__)


class PersisterWorker:
    """Single SQLite writer for raw_pings. Batches every BATCH_INTERVAL_MS."""

    BATCH_INTERVAL_MS = 250

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: Bus | None = None,
        flag_lookup: "FlagLookup | None" = None,
    ) -> None:
        self.conn = conn
        self.bus = bus or get_bus()
        self._buffer: list[tuple[int, int, int, int | None, int | None, int | None,
                                  str | None, int]] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._flag_lookup = flag_lookup

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="persister")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._flush()

    async def _run(self) -> None:
        async with self.bus.subscribe("samples.all", maxsize=8192) as queue:
            interval_s = self.BATCH_INTERVAL_MS / 1000.0
            next_flush = time.monotonic() + interval_s
            while not self._stop.is_set():
                timeout = max(0.005, next_flush - time.monotonic())
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                    if isinstance(msg, PingSample):
                        flag = 0
                        if self._flag_lookup is not None:
                            flag = self._flag_lookup.flag_for_sample(msg)
                        self._buffer.append(
                            (
                                msg.dest_id,
                                msg.ts_ms,
                                1 if msg.success else 0,
                                msg.latency_us,
                                msg.ttl,
                                msg.sequence,
                                msg.error_kind,
                                flag,
                            )
                        )
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() >= next_flush:
                    await self._flush()
                    next_flush = time.monotonic() + interval_s

    async def _flush(self) -> None:
        if not self._buffer:
            return
        rows = self._buffer
        self._buffer = []
        try:
            await self.conn.executemany(
                """
                INSERT INTO raw_pings(dest_id, ts_ms, success, latency_us, ttl,
                                      sequence, error_kind, flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await self.conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("persister_flush_failed", batch_size=len(rows))


class FlagLookup:
    """Duck-typed protocol — anything implementing flag_for_sample(sample) -> int.

    The aggregator implements this so the persister can stamp FLAG_SPIKE pre-INSERT.
    """

    def flag_for_sample(self, sample: PingSample) -> int:  # pragma: no cover
        return 0


async def run_persister(conn, bus) -> None:
    worker = PersisterWorker(conn, bus)
    await worker.start()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await worker.stop()
        raise
