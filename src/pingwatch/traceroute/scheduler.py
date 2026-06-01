"""Periodic + outage-triggered traceroute scheduler."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from pingwatch.bus import Bus, get_bus
from pingwatch.db import queries
from pingwatch.models import DestKind, OutageOpened, TraceSnapshot, TraceTrigger
from pingwatch.traceroute.diff import (
    detect_route_change,
    find_suspect_hop,
    signature_hash,
)
from pingwatch.traceroute.mtr_runner import run_mtr

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class TraceSchedulerConfig:
    interval_seconds: int = 300
    on_outage_after_losses: int = 3
    max_hops: int = 30
    timeout_s: float = 30.0
    hop_history_traces: int = 200


class TraceScheduler:
    def __init__(
        self,
        conn: object,
        bus: Bus | None = None,
        config: TraceSchedulerConfig | None = None,
    ) -> None:
        self._conn = conn
        self._bus = bus or get_bus()
        self._cfg = config or TraceSchedulerConfig()
        self._stop = asyncio.Event()
        self._last_run_ts_ms: dict[int, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        async with self._bus.subscribe("outages.new") as q_outage, \
                self._bus.subscribe("targets.address_changed") as q_addr:
            outage_task = asyncio.create_task(self._consume_outages(q_outage))
            addr_task = asyncio.create_task(self._consume_address_changes(q_addr))
            periodic_task = asyncio.create_task(self._periodic_loop())
            try:
                await self._stop.wait()
            finally:
                outage_task.cancel()
                addr_task.cancel()
                periodic_task.cancel()
                for t in list(self._tasks):
                    t.cancel()

    async def _consume_outages(self, q: asyncio.Queue[OutageOpened]) -> None:
        while not self._stop.is_set():
            opened = await q.get()
            dest = await queries.get_destination(self._conn, opened.dest_id)
            if dest is None or dest.kind != DestKind.EXTERNAL:
                continue
            task = asyncio.create_task(self._run_once(dest, TraceTrigger.OUTAGE))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _consume_address_changes(self, q: asyncio.Queue) -> None:
        while not self._stop.is_set():
            msg = await q.get()
            try:
                dest_id = int(msg["dest_id"])
            except (KeyError, TypeError, ValueError):
                continue
            dest = await queries.get_destination(self._conn, dest_id)
            if dest is None or dest.kind != DestKind.EXTERNAL:
                continue
            self._last_run_ts_ms.pop(dest_id, None)
            task = asyncio.create_task(self._run_once(dest, TraceTrigger.SCHEDULED))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _periodic_loop(self) -> None:
        interval = self._cfg.interval_seconds
        while not self._stop.is_set():
            destinations = await queries.list_destinations(self._conn, enabled_only=True)
            now_ms = int(time.time() * 1000)
            for dest in destinations:
                if dest.kind != DestKind.EXTERNAL:
                    continue
                last_run = self._last_run_ts_ms.get(dest.id, 0)
                if now_ms - last_run >= interval * 1000:
                    task = asyncio.create_task(self._run_once(dest, TraceTrigger.SCHEDULED))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass

    async def _run_once(self, dest: object, trigger: TraceTrigger) -> None:
        host = getattr(dest, "address", None)
        dest_id = int(getattr(dest, "id"))
        if not host:
            return
        self._last_run_ts_ms[dest_id] = int(time.time() * 1000)
        try:
            snapshot = await run_mtr(
                host=host,
                dest_id=dest_id,
                trigger=trigger,
                max_hops=self._cfg.max_hops,
                timeout=self._cfg.timeout_s,
            )
        except Exception:  # noqa: BLE001
            log.exception("trace.run_failed", dest_id=dest_id, host=host)
            return
        await self._persist(snapshot)
        await self._bus.publish("traces.snapshot", snapshot)

    async def _persist(self, snap: TraceSnapshot) -> None:
        sig_hash, sig_text = signature_hash(snap.hops)
        changed = await detect_route_change(self._conn, snap.dest_id, sig_hash)
        trace_id = await queries.insert_traceroute(
            self._conn,
            dest_id=snap.dest_id,
            ts_ms=snap.ts_ms,
            trigger=str(snap.trigger),
            success=snap.success,
            hop_count=len(snap.hops),
            signature_hash=sig_hash,
            route_changed=changed,
            hops=list(snap.hops),
        )
        history = await self._build_history_lookup(snap.dest_id)
        suspect = find_suspect_hop(
            snap.hops,
            hop_history_lookup=history,
            trace_succeeded=snap.success,
        )
        if suspect is not None:
            await self._mark_hop_suspect(trace_id, suspect)
        await queries.upsert_route_signature(
            self._conn,
            snap.dest_id,
            sig_hash,
            sig_text,
            snap.ts_ms,
        )

    async def _mark_hop_suspect(self, trace_id: int, hop_no: int) -> None:
        await self._conn.execute(  # type: ignore[attr-defined]
            "UPDATE trace_hops SET is_suspect = 1 WHERE trace_id = ? AND hop_no = ?",
            (trace_id, hop_no),
        )
        await self._conn.commit()  # type: ignore[attr-defined]

    async def _build_history_lookup(self, dest_id: int) -> dict[int, list[int]]:
        history: dict[int, list[int]] = {}
        try:
            since_ms = 0
            for hop_no in range(2, self._cfg.max_hops + 1):
                rows = await queries.hop_history(
                    self._conn,
                    dest_id,
                    hop_no,
                    since_ms,
                )
                rtts = [int(rtt) for _, rtt in rows if rtt is not None]
                if rtts:
                    history[hop_no] = rtts[: self._cfg.hop_history_traces]
        except Exception:  # noqa: BLE001
            return history
        return history

    def stop(self) -> None:
        self._stop.set()


async def run_trace_scheduler(conn, bus) -> None:
    await TraceScheduler(conn, bus).run()
