from __future__ import annotations

import asyncio
import contextlib
import time

from ..models import Destination, PingSample
from .base import Probe


class TcpProbe(Probe):
    def __init__(self, dest: Destination) -> None:
        super().__init__(dest)
        if dest.port is None:
            raise ValueError(f"TCP probe needs a port: {dest.name}")

    async def probe_once(self) -> PingSample:
        seq = self._next_seq()
        start_ms = int(time.time() * 1000)
        start = time.monotonic()
        port = self.dest.port
        assert port is not None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.dest.address, port),
                timeout=self.dest.timeout_ms / 1000.0,
            )
            latency_us = int((time.monotonic() - start) * 1_000_000)
            writer.close()
            with contextlib.suppress(Exception):  # noqa: BLE001
                await writer.wait_closed()
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=True,
                latency_us=latency_us,
                sequence=seq,
            )
        except TimeoutError:
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
