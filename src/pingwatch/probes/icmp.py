from __future__ import annotations

import time

from icmplib import async_ping

from ..models import PingSample
from .base import Probe


class IcmpProbe(Probe):
    async def probe_once(self) -> PingSample:
        start_ms = int(time.time() * 1000)
        seq = self._next_seq()
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
