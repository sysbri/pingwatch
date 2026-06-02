"""Runtime settings (key/value, hot-reloaded via bus)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from pingwatch.api.deps import ConnDep
from pingwatch.api.schemas import OkResponse
from pingwatch.bus import get_bus
from pingwatch.db import queries as q

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_settings(conn: ConnDep) -> dict[str, Any]:
    return await q.get_all_settings(conn)


@router.patch("", response_model=OkResponse)
async def patch_settings(request: Request, conn: ConnDep) -> OkResponse:
    body = await request.json()
    if not isinstance(body, dict):
        return OkResponse(ok=False, detail="body must be an object")
    await q.set_settings(conn, body)
    bus = get_bus()
    for key, value in body.items():
        await bus.publish("config.changed", {"key": key, "value": value})
    return OkResponse(ok=True, detail=f"{len(body)} keys updated")
