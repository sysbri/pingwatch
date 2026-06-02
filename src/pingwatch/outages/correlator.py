"""Multi-target correlation and UPLINK upgrade.

When an outage opens, schedule a deferred task at ``now + W_ms``. When the
deferred task fires:
  * pull every outage whose start_ts overlaps the canonical's start_ts by
    less than W_ms (in either direction);
  * if 2+ distinct destinations are involved, choose the earliest start_ts
    as the canonical outage, insert ``outage_members`` rows for every
    member, delete the merged-away outages, and update the canonical's
    type to MULTI.
  * Then try to upgrade to UPLINK: every enabled ``external`` destination
    must be a member and no ``gateway`` outage may overlap.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from pingwatch.bus import Bus, get_bus
from pingwatch.db import queries
from pingwatch.models import DestKind, OutageOpened, OutageType
from pingwatch.outages.classifier import (
    _outages_overlapping,
    maybe_tag_wlan,
    stronger,
)

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CorrelatorConfig:
    window_ms: int = 2000
    uplink_min_externals: int = 2
    wlan_reassoc_min_duration_ms: int = 2000


class Correlator:
    def __init__(
        self,
        conn: object,
        bus: Bus | None = None,
        config: CorrelatorConfig | None = None,
    ) -> None:
        self._conn = conn
        self._bus = bus or get_bus()
        self._cfg = config or CorrelatorConfig()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._processed: set[int] = set()

    def update_config(self, window_ms: int | None = None) -> None:
        if window_ms is not None and window_ms > 0:
            self._cfg.window_ms = window_ms

    async def run(self) -> None:
        async with self._bus.subscribe("outages.new") as q_new, \
                self._bus.subscribe("config.changed") as q_cfg:
            new_task = asyncio.create_task(self._consume_new(q_new))
            cfg_task = asyncio.create_task(self._consume_config(q_cfg))
            try:
                await self._stop.wait()
            finally:
                new_task.cancel()
                cfg_task.cancel()
                for t in list(self._tasks):
                    t.cancel()

    async def _consume_new(self, q: asyncio.Queue[OutageOpened]) -> None:
        while not self._stop.is_set():
            opened = await q.get()
            task = asyncio.create_task(self._deferred(opened))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _consume_config(self, q: asyncio.Queue[object]) -> None:
        while not self._stop.is_set():
            change = await q.get()
            key = getattr(change, "key", None)
            value = getattr(change, "new", None) or getattr(change, "value", None)
            if key == "multi.window_ms" and value is not None:
                self.update_config(window_ms=int(value))
            elif key == "uplink.min_externals" and value is not None:
                self._cfg.uplink_min_externals = int(value)
            elif key == "wlan.reassoc_min_duration_ms" and value is not None:
                self._cfg.wlan_reassoc_min_duration_ms = int(value)

    async def _deferred(self, opened: OutageOpened) -> None:
        try:
            await asyncio.sleep(self._cfg.window_ms / 1000)
            await self.process(opened)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("correlator.deferred.error", outage_id=opened.outage_id)

    async def process(self, opened: OutageOpened) -> None:
        if opened.outage_id in self._processed:
            return
        await self.correlate(opened)

    async def correlate(self, opened: OutageOpened) -> int | None:
        """Run MULTI/UPLINK passes on the outage that just matured.

        Returns the id of the canonical (possibly merged) outage, or
        ``None`` if it was merged away.
        """
        w = self._cfg.window_ms
        start_ts = opened.start_ts_ms
        rows = await _outages_with_start_in(
            self._conn,
            start_ts - w,
            start_ts + w,
        )
        candidates: list[dict[str, object]] = [
            dict(r) for r in rows if r["type"] != OutageType.STREAM.value
        ]
        if not candidates:
            return opened.outage_id
        by_dest: dict[int | None, dict[str, object]] = {}
        for row in candidates:
            dest_id = row.get("dest_id_primary")
            existing = by_dest.get(dest_id)  # type: ignore[arg-type]
            if existing is None or int(row["start_ts_ms"]) < int(existing["start_ts_ms"]):
                by_dest[dest_id] = row  # type: ignore[index]
        distinct_dests = [d for d in by_dest if d is not None]
        if len(distinct_dests) < 2:
            # Still tag WLAN if applicable on the single outage.
            await maybe_tag_wlan(
                self._conn,
                opened.outage_id,
                reassoc_min_duration_ms=self._cfg.wlan_reassoc_min_duration_ms,
            )
            self._processed.add(opened.outage_id)
            return opened.outage_id

        canonical = min(candidates, key=lambda r: int(r["start_ts_ms"]))
        canonical_id = int(canonical["id"])

        for row in candidates:
            row_id = int(row["id"])
            dest_id = row.get("dest_id_primary")
            if dest_id is not None:
                await queries.add_outage_member(
                    self._conn,
                    canonical_id,
                    int(dest_id),  # type: ignore[arg-type]
                    int(row["start_ts_ms"]),
                    row.get("end_ts_ms"),  # type: ignore[arg-type]
                    int(row.get("lost_count") or 0),  # type: ignore[arg-type]
                )
            if row_id != canonical_id:
                await _delete_outage(self._conn, row_id)
                self._processed.add(row_id)

        cur_type = OutageType(canonical["type"])  # type: ignore[arg-type]
        if stronger(OutageType.MULTI, cur_type):
            await queries.update_outage_type(self._conn, canonical_id, OutageType.MULTI)
            cur_type = OutageType.MULTI

        upgraded = await self._try_upgrade_uplink(canonical_id, distinct_dests, start_ts, w)
        if upgraded:
            cur_type = OutageType.UPLINK

        await maybe_tag_wlan(
            self._conn,
            canonical_id,
            reassoc_min_duration_ms=self._cfg.wlan_reassoc_min_duration_ms,
        )

        self._processed.add(canonical_id)
        return canonical_id

    async def _try_upgrade_uplink(
        self,
        outage_id: int,
        member_dest_ids: list[int],
        center_ts_ms: int,
        window_ms: int,
    ) -> bool:
        destinations = await queries.list_destinations(self._conn, enabled_only=True)
        externals = [d for d in destinations if d.kind == DestKind.EXTERNAL]
        gateways = [d for d in destinations if d.kind == DestKind.GATEWAY]
        if len(externals) < self._cfg.uplink_min_externals:
            return False
        member_set = set(member_dest_ids)
        if not all(d.id in member_set for d in externals):
            return False
        gateway_ids = {d.id for d in gateways}
        if gateway_ids & member_set:
            return False
        if gateway_ids:
            overlapping = await _outages_overlapping(
                self._conn,
                center_ts_ms - window_ms,
                center_ts_ms + window_ms,
            )
            for row in overlapping:
                dest_id = row.get("dest_id_primary")
                if dest_id is not None and int(dest_id) in gateway_ids:
                    return False
        await queries.update_outage_type(self._conn, outage_id, OutageType.UPLINK)
        log.info("outage.upgraded_uplink", outage_id=outage_id)
        return True

    def stop(self) -> None:
        self._stop.set()


async def _delete_outage(conn: object, outage_id: int) -> None:
    await conn.execute("DELETE FROM outages WHERE id = ?", (outage_id,))  # type: ignore[attr-defined]
    await conn.commit()  # type: ignore[attr-defined]


async def _outages_with_start_in(
    conn: object, lo_ms: int, hi_ms: int
) -> list[dict[str, object]]:
    cur = await conn.execute(  # type: ignore[attr-defined]
        "SELECT id, dest_id_primary, start_ts_ms, end_ts_ms, type, lost_count "
        "FROM outages "
        "WHERE start_ts_ms >= ? AND start_ts_ms <= ?",
        (lo_ms, hi_ms),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def run_outage_correlator(conn, bus) -> None:
    await Correlator(conn, bus).run()
