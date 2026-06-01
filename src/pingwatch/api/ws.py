"""WebSocket endpoints + dashboard broadcaster.

Architecture:
  * One background task computes the dashboard payload every 2 s and pushes
    it on the bus topic ``dashboard.tick``. Every connected /ws/dashboard
    client subscribes to that topic — so the payload is computed once.
  * ``/ws/raw-pings?dest_id=X`` subscribes to ``samples.<id>`` from the bus
    and forwards each sample.
  * ``/ws/events`` aggregates outage/wifi/stream topics into a single feed.

All endpoints enforce a max-clients cap and a 30 s ping/pong keepalive.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from pingwatch.api.routes.dashboard import build_dashboard_payload
from pingwatch.bus import get_bus

log = structlog.get_logger(__name__)

router = APIRouter()

MAX_CLIENTS_PER_TOPIC = 5
PING_INTERVAL_S = 30.0
DASHBOARD_TICK_S = 2.0

_topic_clients: dict[str, set[WebSocket]] = {}


def _slot_acquire(topic: str, ws: WebSocket) -> bool:
    slots = _topic_clients.setdefault(topic, set())
    if len(slots) >= MAX_CLIENTS_PER_TOPIC:
        return False
    slots.add(ws)
    return True


def _slot_release(topic: str, ws: WebSocket) -> None:
    slots = _topic_clients.get(topic)
    if slots is not None:
        slots.discard(ws)


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    return str(obj)


async def _keepalive(ws: WebSocket) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        try:
            await ws.send_json({"type": "ping", "ts_ms": int(time.time() * 1000)})
        except (WebSocketDisconnect, RuntimeError):
            return


async def _ws_lifecycle(
    ws: WebSocket,
    topic_slot: str,
    handler: Callable[[], Awaitable[None]],
) -> None:
    await ws.accept()
    if not _slot_acquire(topic_slot, ws):
        await ws.send_json({"error": "too many clients"})
        await ws.close(code=1013)
        return
    keepalive_task = asyncio.create_task(_keepalive(ws))
    try:
        await handler()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("ws-handler-crashed", topic=topic_slot)
    finally:
        keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive_task
        _slot_release(topic_slot, ws)
        with contextlib.suppress(Exception):
            await ws.close()


# ---------- /ws/dashboard ----------

async def dashboard_broadcaster(get_conn: Callable[[], Any]) -> None:
    """Background task: compute payload, publish to bus every DASHBOARD_TICK_S.

    `get_conn` is a callable so we can re-resolve the connection if the DB
    handle is recreated mid-flight.
    """
    bus = get_bus()
    while True:
        try:
            conn = get_conn()
            payload = await build_dashboard_payload(conn)
            await bus.publish("dashboard.tick", payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("dashboard-broadcaster-error")
        await asyncio.sleep(DASHBOARD_TICK_S)


@router.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket) -> None:
    bus = get_bus()

    async def handler() -> None:
        # Send an initial snapshot immediately so clients render at once.
        try:
            conn = ws.app.state.db.conn
            initial = await build_dashboard_payload(conn)
            await ws.send_json(_to_jsonable(initial))
        except Exception:  # noqa: BLE001
            log.exception("dashboard-initial-failed")

        async with bus.subscribe("dashboard.tick", maxsize=8) as queue:
            while True:
                payload = await queue.get()
                await ws.send_json(_to_jsonable(payload))

    await _ws_lifecycle(ws, "dashboard", handler)


# ---------- /ws/raw-pings ----------

@router.websocket("/ws/raw-pings")
async def ws_raw_pings(ws: WebSocket, dest_id: int = Query(...)) -> None:
    bus = get_bus()
    topic = f"samples.{dest_id}"

    async def handler() -> None:
        async with bus.subscribe(topic, maxsize=512) as queue:
            while True:
                sample = await queue.get()
                await ws.send_json(_to_jsonable(sample))

    await _ws_lifecycle(ws, f"raw-pings:{dest_id}", handler)


# ---------- /ws/events ----------

@router.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    bus = get_bus()

    async def handler() -> None:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=256)

        async def relay(topic: str) -> None:
            async with bus.subscribe(topic, maxsize=128) as q_:
                while True:
                    msg = await q_.get()
                    try:
                        queue.put_nowait((topic, msg))
                    except asyncio.QueueFull:
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                        queue.put_nowait((topic, msg))

        topics = (
            "outages.new",
            "outages.close",
            "wifi.events",
            "drops.stream",
            "drops.heartbeat",
        )
        relays = [asyncio.create_task(relay(t)) for t in topics]
        try:
            while True:
                topic, msg = await queue.get()
                await ws.send_json(
                    {"topic": topic, "payload": _to_jsonable(msg)}
                )
        finally:
            for t in relays:
                t.cancel()
            for t in relays:
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    await _ws_lifecycle(ws, "events", handler)
