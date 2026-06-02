from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..models import Destination, PingSample


class Probe(ABC):
    """Long-running probe yielding one PingSample per scheduled attempt."""

    def __init__(self, dest: Destination) -> None:
        self.dest = dest
        self._sequence = 0

    def _next_seq(self) -> int:
        self._sequence += 1
        return self._sequence

    @abstractmethod
    async def probe_once(self) -> PingSample:
        """Perform one probe attempt (also used by the Settings live-test)."""

    async def cleanup(self) -> None:  # noqa: B027  # optional hook, intentionally a no-op default
        """Release per-probe resources at shutdown. Default: nothing."""

    async def run(self) -> AsyncIterator[PingSample]:
        """Yield a PingSample every ``dest.interval_ms`` until cancelled.

        The scheduling is identical for every probe type: an initial random
        offset so concurrent probes don't fire in lockstep, then ``probe_once``
        each cycle with +/-5% jitter on the inter-probe sleep. Subclasses
        implement ``probe_once`` and may override ``cleanup``.
        """
        interval_s = self.dest.interval_ms / 1000.0
        # Stagger the first run so concurrent probes don't all fire on one tick.
        await asyncio.sleep(random.uniform(0.0, interval_s))  # noqa: S311  # non-crypto jitter
        try:
            while True:
                t0 = time.monotonic()
                yield await self.probe_once()
                sleep_for = interval_s - (time.monotonic() - t0)
                if sleep_for > 0:
                    jitter = random.uniform(-0.05, 0.05) * interval_s  # noqa: S311  # non-crypto jitter
                    await asyncio.sleep(max(0.0, sleep_for + jitter))
        finally:
            await self.cleanup()
