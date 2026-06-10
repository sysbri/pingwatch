"""Keep the gateway destination pointed at the real default route.

The host-helper stamps the current default-route gateway into
``wifi-status.json`` (``gateway_ip``). When the Pi moves to a new network the
seeded/old gateway address goes stale and the probe pings a dead IP forever.
This worker follows the actual route: it updates the ``kind='gateway'``
destination (same semantics as a manual address edit in Settings -> Ziele:
reset history, clear resolved_ip, notify the probe runner) whenever the live
gateway differs. Opt-out via the ``gateway.auto_update`` setting.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from pathlib import Path
from typing import Any

import structlog

from pingwatch.db import queries as q

log = structlog.get_logger(__name__)

STATUS_FILE = Path("/run/pingwatch-shared/wifi-status.json")
CHECK_INTERVAL_S = 10.0
# Older than this and the host-helper is not refreshing — don't act on it.
_STALE_AFTER_S = 60.0
# A new gateway must be observed this many consecutive ticks before we act.
# Guards against a flapping default route (e.g. a marginal USB stick toggling
# in/out) repeatedly wiping the gateway history via reset_destination_data.
_STABLE_TICKS = 3


def _read_gateway_ip(path: Path = STATUS_FILE) -> str | None:
    try:
        st = path.stat()
        if time.time() - st.st_mtime > _STALE_AFTER_S:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    gw = data.get("gateway_ip")
    if not isinstance(gw, str) or not gw:
        return None
    # Defense in depth: a `default dev ppp0`-style route (no `via`) must never
    # leak a device name in here — only act on a real IP address.
    try:
        ipaddress.ip_address(gw)
    except ValueError:
        return None
    return gw


class GatewayDebouncer:
    """Yield a value only after it was observed *required* consecutive times."""

    def __init__(self, required: int = _STABLE_TICKS) -> None:
        self._required = required
        self._candidate: str | None = None
        self._seen = 0

    def observe(self, value: str | None) -> str | None:
        if value is None or value != self._candidate:
            self._candidate = value
            self._seen = 1 if value else 0
            return None
        self._seen += 1
        return value if self._seen >= self._required else None


async def sync_once(conn: Any, bus: Any, gateway_ip: str | None) -> bool:
    """Update the gateway destination to *gateway_ip* if it changed.

    Returns True when an update was applied.
    """
    if not gateway_ip:
        return False
    if not await q.get_setting_typed(conn, "gateway.auto_update", True):
        return False
    # The first gateway-kind target (ordering ASC) is the auto-managed one;
    # additional user-created gateway-kind targets are left alone.
    gateways = [d for d in await q.list_destinations(conn) if d.get("kind") == "gateway"]
    if not gateways:
        return False
    dest = gateways[0]
    if dest["address"] == gateway_ip:
        return False
    await q.update_destination(
        conn, dest["id"], {"address": gateway_ip, "resolved_ip": None}
    )
    # New address = new slot: drop the old IP's history so the dashboard
    # starts clean (mirrors the manual edit flow in api/routes/targets.py).
    # Note: a few in-flight samples probed against the old address can land
    # after this reset (probe runner reload + persister flush are async) —
    # bounded to 1-3 rows, same race as the manual edit flow; do not "fix"
    # the ordering, the events must fire after the DB state is final.
    await q.reset_destination_data(conn, dest["id"])
    await bus.publish(
        "config.changed", {"key": f"destinations.{dest['id']}", "action": "update"}
    )
    await bus.publish("targets.address_changed", {"dest_id": dest["id"]})
    log.info(
        "gateway_sync.updated",
        old=dest["address"],
        new=gateway_ip,
        dest_id=dest["id"],
    )
    return True


async def run_gateway_sync(conn: Any, bus: Any) -> None:
    debounce = GatewayDebouncer()
    while True:
        try:
            stable_gw = debounce.observe(_read_gateway_ip())
            if stable_gw:
                await sync_once(conn, bus, stable_gw)
        except Exception:  # noqa: BLE001
            log.exception("gateway_sync.error")
        await asyncio.sleep(CHECK_INTERVAL_S)
