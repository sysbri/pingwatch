from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator

import structlog
from icmplib import async_ping

from ..models import Destination, PingSample
from .base import Probe

log = structlog.get_logger(__name__)


class IcmpProbe(Probe):
    def __init__(self, dest: Destination) -> None:
        super().__init__(dest)
        self._sequence = 0

    async def probe_once(self) -> PingSample:
        start_ms = int(time.time() * 1000)
        self._sequence += 1
        seq = self._sequence
        try:
            host = await async_ping(
                self.dest.address,
                count=1,
                timeout=self.dest.timeout_ms / 1000.0,
                privileged=False,
            )
            if host.is_alive and host.avg_rtt > 0:
                return PingSample(
                    dest_id=self.dest.id,
                    ts_ms=start_ms,
                    success=True,
                    latency_us=int(host.avg_rtt * 1000),
                    ttl=None,
                    sequence=seq,
                )
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=False,
                sequence=seq,
                error_kind="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=False,
                sequence=seq,
                error_kind=type(exc).__name__,
            )

    async def run(self) -> AsyncIterator[PingSample]:
        # Jitter the first run so concurrent probes don't all fire on the same tick.
        await asyncio.sleep(random.uniform(0.0, self.dest.interval_ms / 1000.0))
        while True:
            t0 = time.monotonic()
            sample = await self.probe_once()
            yield sample
            elapsed = time.monotonic() - t0
            interval_s = self.dest.interval_ms / 1000.0
            sleep_for = interval_s - elapsed
            if sleep_for > 0:
                # +/- 5% jitter to avoid lockstep behaviour
                jitter = random.uniform(-0.05, 0.05) * interval_s
                await asyncio.sleep(max(0.0, sleep_for + jitter))
