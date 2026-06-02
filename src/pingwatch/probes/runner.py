from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import aiosqlite
import structlog

from ..bus import Bus, get_bus
from ..db import queries as q
from ..models import Destination, PingSample, ProbeType
from .base import Probe
from .dns_query import DnsQueryProbe
from .http_head import HttpHeadProbe
from .icmp import IcmpProbe
from .tcp import TcpProbe

log = structlog.get_logger(__name__)


def build_probe(dest: Destination) -> Probe:
    match dest.type:
        case ProbeType.ICMP:
            return IcmpProbe(dest)
        case ProbeType.TCP:
            return TcpProbe(dest)
        case ProbeType.HTTP:
            return HttpHeadProbe(dest)
        case ProbeType.DNS:
            return DnsQueryProbe(dest)


class ProbeRunner:
    """Owns one task per enabled destination. Hot-reloads on config.changed."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: Bus | None = None,
        probe_factory: Callable[[Destination], Probe] = build_probe,
    ) -> None:
        self.conn = conn
        self.bus = bus or get_bus()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._dests: dict[int, Destination] = {}
        self._probe_factory = probe_factory
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        await self._reload()
        asyncio.create_task(self._watch_config_changes(), name="probe_runner.config_watch")

    async def stop(self) -> None:
        self._stop_event.set()
        for t in self._tasks.values():
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._dests.clear()

    async def _reload(self) -> None:
        dests = await q.list_destinations(self.conn, enabled_only=True)
        new_ids = {d.id for d in dests}

        # Cancel tasks for removed/changed dests.
        for dest_id in list(self._tasks.keys()):
            if dest_id not in new_ids:
                await self._cancel_task(dest_id)
                continue
            old = self._dests.get(dest_id)
            new = next((d for d in dests if d.id == dest_id), None)
            if old != new:
                await self._cancel_task(dest_id)

        # Spawn tasks for new/changed dests.
        for dest in dests:
            if dest.id not in self._tasks:
                self._spawn(dest)
                self._dests[dest.id] = dest

    async def _cancel_task(self, dest_id: int) -> None:
        task = self._tasks.pop(dest_id, None)
        self._dests.pop(dest_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await task

    def _spawn(self, dest: Destination) -> None:
        probe = self._probe_factory(dest)
        self._tasks[dest.id] = asyncio.create_task(
            self._run_probe(dest, probe), name=f"probe.{dest.id}"
        )

    async def _run_probe(self, dest: Destination, probe: Probe) -> None:
        try:
            async for sample in probe.run():
                await self.bus.publish(f"samples.{dest.id}", sample)
                await self.bus.publish("samples.all", sample)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("probe_crashed", dest_id=dest.id, name=dest.name)

    async def _watch_config_changes(self) -> None:
        async with self.bus.subscribe("config.changed") as queue:
            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                key = msg.get("key", "") if isinstance(msg, dict) else ""
                if key.startswith("destinations.") or key.startswith("probe."):
                    await self._reload()


async def one_shot(dest: Destination) -> PingSample:
    """Run a single probe attempt for the Settings live-test button."""
    probe = build_probe(dest)
    return await probe.probe_once()


async def run_probe_runner(conn, bus) -> None:
    r = ProbeRunner(conn, bus)
    await r.start()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await r.stop()
        raise
