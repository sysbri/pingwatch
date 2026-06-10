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
    return gw if isinstance(gw, str) and gw else None


async def sync_once(conn: Any, bus: Any, gateway_ip: str | None) -> bool:
    """Update the gateway destination to *gateway_ip* if it changed.

    Returns True when an update was applied.
    """
    if not gateway_ip:
        return False
    if not await q.get_setting_typed(conn, "gateway.auto_update", True):
        return False
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
    while True:
        try:
            await sync_once(conn, bus, _read_gateway_ip())
        except Exception:  # noqa: BLE001
            log.exception("gateway_sync.error")
        await asyncio.sleep(CHECK_INTERVAL_S)
