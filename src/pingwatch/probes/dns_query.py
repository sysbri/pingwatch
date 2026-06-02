from __future__ import annotations

import ipaddress
import time

import dns.asyncresolver
import dns.exception

from ..models import Destination, PingSample
from .base import Probe

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
        self._resolver = dns.asyncresolver.Resolver()
        if _is_ip(dest.address):
            self._resolver.nameservers = [dest.address]
            self._query_name = _CANARY_NAME
        else:
            self._query_name = dest.address
        self._resolver.lifetime = dest.timeout_ms / 1000.0
        self._resolver.timeout = dest.timeout_ms / 1000.0

    async def probe_once(self) -> PingSample:
        seq = self._next_seq()
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
        except (TimeoutError, dns.exception.DNSException) as exc:
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
