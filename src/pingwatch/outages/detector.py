"""Per-destination outage detector.

State machine:
  * N consecutive losses open an EINZEL outage and emit OutageOpened on
    ``outages.new``.
  * K consecutive successes close the outage with
    ``end_ts_ms = first_ok_ts_ms - (K-1) * interval_ms``.

Crash recovery: on startup any open outage older than 60 s w.r.t. its
last known ping is force-closed with ``notes='restart'``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from pingwatch.bus import Bus, get_bus
from pingwatch.db import queries
from pingwatch.db.q_destinations import list_destinations as _list_destinations_typed
from pingwatch.models import (
    Destination,
    OutageClosed,
    OutageOpened,
    OutageType,
    PingSample,
)

log = structlog.get_logger(__name__)

_DEFAULT_N = 2
_DEFAULT_K = 2
_RESTART_GAP_MS = 60_000


@dataclass(slots=True)
class _DestState:
    dest: Destination
    consec_loss: int = 0
    consec_ok: int = 0
    open_outage_id: int | None = None
    open_start_ts_ms: int | None = None
    lost_count: int = 0
    last_ts_ms: int | None = None


class OutageDetector:
    """One instance manages every destination's state machine.

    The detector subscribes to ``samples.all`` and dispatches per-dest.
    Workers should publish each sample under both ``samples.<dest_id>``
    and ``samples.all``. We use ``samples.all`` to avoid one task per dest.
    """

    def __init__(
        self,
        conn: object,
        destinations: list[Destination],
        bus: Bus | None = None,
        n_consec_loss: int = _DEFAULT_N,
        k_consec_ok: int = _DEFAULT_K,
    ) -> None:
        self._conn = conn
        self._bus = bus or get_bus()
        self._n = n_consec_loss
        self._k = k_consec_ok
        self._state: dict[int, _DestState] = {d.id: _DestState(dest=d) for d in destinations}
        self._stop = asyncio.Event()

    @property
    def n_consec_loss(self) -> int:
        return self._n

    @property
    def k_consec_ok(self) -> int:
        return self._k

    def update_thresholds(self, n: int | None = None, k: int | None = None) -> None:
        if n is not None and n > 0:
            self._n = n
        if k is not None and k > 0:
            self._k = k

    async def recover_open_outages(self, now_ms: int | None = None) -> None:
        """Force-close any outage left open across a restart."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        rows = await queries.open_outages(self._conn)
        for row in rows:
            outage_id = int(row["id"])
            dest_id = (
                row.get("dest_id_primary") if isinstance(row, dict) else row["dest_id_primary"]
            )
            start_ts_ms = int(row["start_ts_ms"])
            last_ts_ms = await _last_ping_ts(self._conn, dest_id) if dest_id else None
            ref_ts = last_ts_ms if last_ts_ms is not None else start_ts_ms
            if now_ms - ref_ts > _RESTART_GAP_MS:
                end_ts = ref_ts
                await queries.close_outage(self._conn, outage_id, end_ts)
                await _update_outage_notes(self._conn, outage_id, "restart")
                log.info(
                    "outage.recovered",
                    outage_id=outage_id,
                    dest_id=dest_id,
                    end_ts_ms=end_ts,
                )

    async def process(self, sample: PingSample) -> None:
        state = self._state.get(sample.dest_id)
        if state is None:
            return
        state.last_ts_ms = sample.ts_ms
        if not sample.success:
            await self._on_loss(state, sample)
        else:
            await self._on_ok(state, sample)

    async def _on_loss(self, state: _DestState, sample: PingSample) -> None:
        state.consec_ok = 0
        state.consec_loss += 1
        if state.open_outage_id is not None:
            state.lost_count += 1
            return
        if state.consec_loss >= self._n:
            start_ts_ms = sample.ts_ms - (self._n - 1) * state.dest.interval_ms
            outage_id = await queries.open_outage(
                self._conn,
                dest_id=state.dest.id,
                start_ts_ms=start_ts_ms,
                type_=OutageType.EINZEL,
                lost_count=self._n,
            )
            state.open_outage_id = outage_id
            state.open_start_ts_ms = start_ts_ms
            state.lost_count = self._n
            opened = OutageOpened(
                outage_id=outage_id,
                dest_id=state.dest.id,
                start_ts_ms=start_ts_ms,
            )
            await self._bus.publish("outages.new", opened)
            log.info(
                "outage.opened",
                outage_id=outage_id,
                dest_id=state.dest.id,
                start_ts_ms=start_ts_ms,
            )

    async def _on_ok(self, state: _DestState, sample: PingSample) -> None:
        state.consec_loss = 0
        state.consec_ok += 1
        if state.open_outage_id is None:
            return
        if state.consec_ok < self._k:
            return
        first_ok_ts_ms = sample.ts_ms - (self._k - 1) * state.dest.interval_ms
        outage_id = state.open_outage_id
        start_ts_ms = state.open_start_ts_ms or first_ok_ts_ms
        # Outage ends at the last loss = one interval before the first OK.
        # first_ok_ts_ms already backs out the K-1 confirming OKs, so the end is
        # independent of K. (The old code subtracted (K-1) twice -> wrong for K>=3.)
        end_ts_ms = first_ok_ts_ms - state.dest.interval_ms
        await queries.close_outage(self._conn, outage_id, end_ts_ms)
        await _update_outage_lost_count(self._conn, outage_id, state.lost_count)
        closed = OutageClosed(
            outage_id=outage_id,
            dest_id=state.dest.id,
            start_ts_ms=start_ts_ms,
            end_ts_ms=end_ts_ms,
            duration_ms=end_ts_ms - start_ts_ms,
            outage_type=OutageType.EINZEL,
        )
        await self._bus.publish("outages.close", closed)
        log.info(
            "outage.closed",
            outage_id=outage_id,
            dest_id=state.dest.id,
            duration_ms=closed.duration_ms,
        )
        state.open_outage_id = None
        state.open_start_ts_ms = None
        state.lost_count = 0

    async def run(self) -> None:
        await self.recover_open_outages()
        async with self._bus.subscribe("samples.all") as samples_q, \
                self._bus.subscribe("config.changed") as cfg_q:
            samples_task = asyncio.create_task(self._consume_samples(samples_q))
            cfg_task = asyncio.create_task(self._consume_config(cfg_q))
            try:
                await self._stop.wait()
            finally:
                samples_task.cancel()
                cfg_task.cancel()

    async def _consume_samples(self, q: asyncio.Queue[PingSample]) -> None:
        while not self._stop.is_set():
            sample = await q.get()
            try:
                await self.process(sample)
            except Exception:  # noqa: BLE001
                log.exception("outage.detector.error", dest_id=sample.dest_id)

    async def _consume_config(self, q: asyncio.Queue[object]) -> None:
        while not self._stop.is_set():
            change = await q.get()
            key = getattr(change, "key", None)
            value = getattr(change, "new", None) or getattr(change, "value", None)
            if key == "outage.N_consec_loss" and value is not None:
                self.update_thresholds(n=int(value))
                log.info("outage.detector.threshold_updated", N=self._n)
            elif key == "outage.K_consec_ok" and value is not None:
                self.update_thresholds(k=int(value))
                log.info("outage.detector.threshold_updated", K=self._k)

    def stop(self) -> None:
        self._stop.set()


async def _last_ping_ts(conn: object, dest_id: int) -> int | None:
    cur = await conn.execute(  # type: ignore[attr-defined]
        "SELECT ts_ms FROM raw_pings WHERE dest_id = ? ORDER BY ts_ms DESC LIMIT 1",
        (dest_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row[0]) if row else None


async def _update_outage_notes(conn: object, outage_id: int, notes: str) -> None:
    await conn.execute(  # type: ignore[attr-defined]
        "UPDATE outages SET notes = ? WHERE id = ?", (notes, outage_id)
    )
    await conn.commit()  # type: ignore[attr-defined]


async def _update_outage_lost_count(conn: object, outage_id: int, lost_count: int) -> None:
    await conn.execute(  # type: ignore[attr-defined]
        "UPDATE outages SET lost_count = ? WHERE id = ?", (lost_count, outage_id)
    )
    await conn.commit()  # type: ignore[attr-defined]


async def run_outage_detector(conn, bus) -> None:
    dests = await _list_destinations_typed(conn, enabled_only=True)
    await OutageDetector(conn, dests, bus).run()
