"""Captive-portal aware connectivity check (phone-style HTTP-204 probe).

ICMP pings often pass through captive portals, so the ping-based "Internet
OK" can lie on a free WLAN. This worker probes a well-known generate_204
endpoint over plain HTTP every ~30 s:

- 204 (empty)            -> "full"     real internet
- 30x redirect           -> "portal"   the Location header IS the portal URL
- anything else answered -> "portal"   response was hijacked
- no answer at all       -> "offline"

The latest state is process-global (API workers share the process) and the
dashboard renders it as an honest PORTAL/OFFLINE pill; the portal URL feeds
the "Portal oeffnen" button.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# Plain HTTP on purpose: portals can only hijack unencrypted probes.
PROBE_URL = "http://connectivitycheck.gstatic.com/generate_204"
CHECK_INTERVAL_S = 30.0
_PROBE_TIMEOUT_S = 6.0


@dataclass(slots=True, frozen=True)
class ConnectivityState:
    status: str = "unknown"  # full | portal | offline | unknown
    portal_url: str | None = None
    checked_ts_ms: int = 0


def classify_response(
    status_code: int | None, location: str | None, body_len: int
) -> tuple[str, str | None]:
    if status_code is None:
        return "offline", None
    if status_code == 204:
        return "full", None
    if 300 <= status_code < 400 and location:
        return "portal", location
    # Any other answer (hijacked 200 page, portal 5xx, ...) means something
    # intercepted the probe.
    return "portal", None


class Monitor:
    """Holds the current state and publishes transitions on the bus."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self.state = ConnectivityState()

    async def apply(self, status: str, portal_url: str | None, ts_ms: int) -> None:
        prev = self.state
        # A 200-hijack probe carries no Location; keep the portal URL we
        # already learned from an earlier redirect.
        if status == "portal" and portal_url is None and prev.status == "portal":
            portal_url = prev.portal_url
        self.state = ConnectivityState(
            status=status, portal_url=portal_url, checked_ts_ms=ts_ms
        )
        if status != prev.status:
            log.info("connectivity.changed", status=status, portal_url=portal_url)
            await self._bus.publish(
                "connectivity.changed",
                {"status": status, "portal_url": portal_url, "ts_ms": ts_ms},
            )


# Process-global: written by the worker, read by the dashboard/wifi routes.
_monitor: Monitor | None = None


def get_state() -> ConnectivityState:
    return _monitor.state if _monitor is not None else ConnectivityState()


async def _probe(client: httpx.AsyncClient) -> tuple[int | None, str | None, int]:
    try:
        resp = await client.get(PROBE_URL, follow_redirects=False)
    except (httpx.HTTPError, OSError):
        return None, None, 0
    return resp.status_code, resp.headers.get("location"), len(resp.content)


async def run_connectivity(conn: Any, bus: Any) -> None:  # noqa: ARG001 (worker signature)
    global _monitor
    _monitor = Monitor(bus)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_PROBE_TIMEOUT_S, connect=3.0)
    ) as client:
        while True:
            try:
                code, location, body_len = await _probe(client)
                status, portal_url = classify_response(code, location, body_len)
                await _monitor.apply(status, portal_url, int(time.time() * 1000))
            except Exception:  # noqa: BLE001
                log.exception("connectivity.error")
            await asyncio.sleep(CHECK_INTERVAL_S)
