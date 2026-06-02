from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class Bus:
    """In-process pub/sub: subscribers get their own asyncio.Queue, fan-out per topic.

    Topics used in PingWatch:
      samples.<dest_id>       - one PingSample per ping
      drops.stream            - StreamDropEvent
      drops.heartbeat         - HeartbeatLossEvent
      wifi.events             - WifiEvent
      outages.new             - Outage (just opened)
      outages.close           - Outage (just closed)
      traces.snapshot         - TraceSnapshot
      config.changed          - SettingsChange (key, old, new)
      dashboard.tick          - DashboardSnapshot (every 2 s)
    """

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Any]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, message: Any) -> None:
        # Snapshot subscribers to avoid holding the lock while putting.
        async with self._lock:
            queues = list(self._subs.get(topic, ()))
        for q in queues:
            # Non-blocking: if a subscriber is slow, drop the oldest item.
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                q.put_nowait(message)

    @asynccontextmanager
    async def subscribe(
        self, topic: str, maxsize: int = 1024
    ) -> AsyncIterator[asyncio.Queue[Any]]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subs[topic].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subs[topic].discard(queue)

    def topics(self) -> list[str]:
        return list(self._subs.keys())


# Global bus instance used by the running app. Production code reaches for
# this rather than constructing its own bus.
_bus: Bus | None = None


def get_bus() -> Bus:
    global _bus
    if _bus is None:
        _bus = Bus()
    return _bus
