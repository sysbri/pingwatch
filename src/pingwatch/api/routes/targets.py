"""CRUD for monitor destinations (targets)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, status

from pingwatch.api import _queries_compat as q
from pingwatch.api.deps import ConnDep
from pingwatch.bus import get_bus
from pingwatch.api.schemas import (
    OkResponse,
    ReorderIn,
    TargetIn,
    TargetOut,
    TargetPatch,
    TestResult,
)

router = APIRouter(prefix="/api/targets", tags=["targets"])


def _to_out(row: dict[str, Any]) -> TargetOut:
    return TargetOut(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        type=row["type"],
        kind=row["kind"],
        interval_ms=row["interval_ms"],
        timeout_ms=row["timeout_ms"],
        port=row.get("port"),
        enabled=bool(row["enabled"]),
        ordering=row["ordering"],
        resolved_ip=row.get("resolved_ip"),
    )


@router.get("", response_model=list[TargetOut])
async def list_targets(conn: ConnDep) -> list[TargetOut]:
    rows = await q.list_destinations(conn)
    return [_to_out(r) for r in rows]


@router.get("/{target_id}", response_model=TargetOut)
async def get_target(target_id: int, conn: ConnDep) -> TargetOut:
    row = await q.get_destination(conn, target_id)
    if not row:
        raise HTTPException(status_code=404, detail="target not found")
    return _to_out(row)


@router.post("", response_model=TargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(body: TargetIn, conn: ConnDep) -> TargetOut:
    new_id = await q.create_destination(conn, body.model_dump())
    row = await q.get_destination(conn, new_id)
    if not row:
        raise HTTPException(status_code=500, detail="target creation failed")
    bus = get_bus()
    await bus.publish(
        "config.changed", {"key": f"destinations.{new_id}", "action": "create"}
    )
    # Triggert sofortigen Trace fuer externe Ziele (sonst wartet der Scheduler
    # bis zum naechsten 5-min-Intervall).
    if row.get("kind") == "external":
        await bus.publish("targets.address_changed", {"dest_id": new_id})
    return _to_out(row)


@router.patch("/{target_id}", response_model=TargetOut)
async def patch_target(target_id: int, body: TargetPatch, conn: ConnDep) -> TargetOut:
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    existing = await q.get_destination(conn, target_id)
    if not existing:
        raise HTTPException(status_code=404, detail="target not found")
    address_changed = (
        "address" in patch and patch["address"] != existing.get("address")
    )
    if address_changed:
        patch["resolved_ip"] = None
    await q.update_destination(conn, target_id, patch)
    if address_changed:
        # Neue Adresse = neuer Slot. Historische Pings/Traces/Outages der
        # alten Adresse loeschen, damit User einen sauberen Start hat.
        await q.reset_destination_data(conn, target_id)
    bus = get_bus()
    await bus.publish(
        "config.changed",
        {"key": f"destinations.{target_id}", "action": "update"},
    )
    if address_changed:
        await bus.publish("targets.address_changed", {"dest_id": target_id})
    row = await q.get_destination(conn, target_id)
    assert row is not None  # noqa: S101
    return _to_out(row)


@router.delete("/{target_id}", response_model=OkResponse)
async def delete_target(target_id: int, conn: ConnDep) -> OkResponse:
    existing = await q.get_destination(conn, target_id)
    if not existing:
        raise HTTPException(status_code=404, detail="target not found")
    await q.delete_destination(conn, target_id)
    await get_bus().publish(
        "config.changed", {"key": f"destinations.{target_id}", "action": "delete"}
    )
    return OkResponse(ok=True)


@router.post("/reorder", response_model=OkResponse)
async def reorder_targets(body: ReorderIn, conn: ConnDep) -> OkResponse:
    await q.reorder_destinations(conn, body.order)
    await get_bus().publish(
        "config.changed", {"key": "destinations.order", "action": "reorder"}
    )
    return OkResponse(ok=True)


@router.post("/{target_id}/reset", response_model=OkResponse)
async def reset_target_data(target_id: int, conn: ConnDep) -> OkResponse:
    """Loescht alle historischen Daten (Pings/Aggregates/Traces/Outages)
    fuer dieses Ziel. Ziel-Definition selbst bleibt erhalten."""
    existing = await q.get_destination(conn, target_id)
    if not existing:
        raise HTTPException(status_code=404, detail="target not found")
    await q.reset_destination_data(conn, target_id)
    if existing.get("kind") == "external":
        await get_bus().publish("targets.address_changed", {"dest_id": target_id})
    return OkResponse(ok=True)


@router.post("/{target_id}/test", response_model=TestResult)
async def test_target(target_id: int, conn: ConnDep) -> TestResult:
    """Run a one-shot probe synchronously and return the result.

    The probe module is owned by another agent; we try to import it lazily and
    gracefully fall back to a stub when not yet present.
    """
    row = await q.get_destination(conn, target_id)
    if not row:
        raise HTTPException(status_code=404, detail="target not found")

    try:  # pragma: no cover - depends on parallel agent
        from pingwatch.probes import runner as probe_runner  # type: ignore

        result = await probe_runner.one_shot(row)
        return TestResult(
            success=bool(result.success),
            latency_us=result.latency_us,
            error_kind=result.error_kind,
            ts_ms=result.ts_ms,
        )
    except Exception as exc:  # noqa: BLE001
        return TestResult(
            success=False,
            latency_us=None,
            error_kind=f"probe_unavailable: {type(exc).__name__}",
            ts_ms=int(time.time() * 1000),
        )
