from __future__ import annotations

import asyncio
import ipaddress
import random
import time
from collections.abc import AsyncIterator

import dns.asyncresolver
import dns.exception
import structlog

from ..models import Destination, PingSample
from .base import Probe

log = structlog.get_logger(__name__)

# Convention used by the runner:
#  - if dest.address is an IP, treat it as the resolver and query a fixed canary name
#  - if dest.address is a name, ask the system resolver for that name
_CANARY_NAME = "cloudflare.com"


def _is_ip(addr: str) -> bool:
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        return False


class DnsQueryProbe(Probe):
    def __init__(self, dest: Destination) -> None:
        super().__init__(dest)
        self._sequence = 0
        self._resolver = dns.asyncresolver.Resolver()
        if _is_ip(dest.address):
            self._resolver.nameservers = [dest.address]
            self._query_name = _CANARY_NAME
        else:
            self._query_name = dest.address
        self._resolver.lifetime = dest.timeout_ms / 1000.0
        self._resolver.timeout = dest.timeout_ms / 1000.0

    async def probe_once(self) -> PingSample:
        self._sequence += 1
        seq = self._sequence
        start_ms = int(time.time() * 1000)
        start = time.monotonic()
        try:
            await self._resolver.resolve(self._query_name, "A")
            latency_us = int((time.monotonic() - start) * 1_000_000)
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=True,
                latency_us=latency_us,
                sequence=seq,
            )
        except (dns.exception.DNSException, asyncio.TimeoutError) as exc:
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=False,
                sequence=seq,
                error_kind=type(exc).__name__,
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
