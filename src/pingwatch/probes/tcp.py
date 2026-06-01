from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator

import structlog

from ..models import Destination, PingSample
from .base import Probe

log = structlog.get_logger(__name__)


class TcpProbe(Probe):
    def __init__(self, dest: Destination) -> None:
        super().__init__(dest)
        self._sequence = 0
        if dest.port is None:
            raise ValueError(f"TCP probe needs a port: {dest.name}")

    async def probe_once(self) -> PingSample:
        self._sequence += 1
        seq = self._sequence
        start_ms = int(time.time() * 1000)
        start = time.monotonic()
        port = self.dest.port
        assert port is not None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.dest.address, port),
                timeout=self.dest.timeout_ms / 1000.0,
            )
            latency_us = int((time.monotonic() - start) * 1_000_000)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            del reader
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=True,
                latency_us=latency_us,
                sequence=seq,
            )
        except asyncio.TimeoutError:
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
        await asyncio.sleep(random.uniform(0.0, self.dest.interval_ms / 1000.0))
        while True:
            t0 = time.monotonic()
            yield await self.probe_once()
            elapsed = time.monotonic() - t0
            interval_s = self.dest.interval_ms / 1000.0
            sleep_for = interval_s - elapsed
            if sleep_for > 0:
                jitter = random.uniform(-0.05, 0.05) * interval_s
                await asyncio.sleep(max(0.0, sleep_for + jitter))
