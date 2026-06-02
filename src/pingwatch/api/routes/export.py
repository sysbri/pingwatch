"""Export endpoints — CSV / JSON / ZIP / SQLite snapshot / USB.

The actual export builders live in ``pingwatch.export.*``. Errors are logged
and surfaced to the caller — there is no silent degraded fallback.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from pingwatch.api.deps import ConnDep
from pingwatch.api.schemas import OkResponse
from pingwatch.export import csv_exporter, db_snapshot, json_exporter, zip_bundle

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/export", tags=["export"])

USB_ROOT = Path("/media/pingwatch-usb")


def _window_ms(range_str: str | None) -> tuple[int, int]:
    """Map a UI range token to an absolute (since_ms, until_ms) window."""
    window = {
        "1h": 3_600_000,
        "24h": 86_400_000,
        "7d": 7 * 86_400_000,
        "30d": 30 * 86_400_000,
        "all": 365 * 86_400_000,
    }.get(range_str or "24h", 86_400_000)
    now = int(time.time() * 1000)
    return now - window, now


def _iter_file(path: Path) -> Iterator[bytes]:
    """Stream a temp file in chunks, then delete it."""
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(64 * 1024):
                yield chunk
    finally:
        path.unlink(missing_ok=True)


def _attachment(name: str) -> dict[str, str]:
    return {"Content-Disposition": f"attachment; filename={name}"}


@router.post("/csv")
async def export_csv(
    conn: ConnDep, range: str = Query(default="24h"),  # noqa: A002
) -> StreamingResponse:
    since, until = _window_ms(range)
    body = await csv_exporter.export_outages_csv(conn, since, until)
    name = f"pingwatch-outages-{range}-{int(time.time())}.csv"
    return StreamingResponse(
        iter([body]), media_type="text/csv", headers=_attachment(name)
    )


@router.post("/json")
async def export_json(
    conn: ConnDep, range: str = Query(default="24h"),  # noqa: A002
) -> StreamingResponse:
    since, until = _window_ms(range)
    data = await json_exporter.export_all_json(conn, since, until)
    name = f"pingwatch-{range}-{int(time.time())}.json"
    return StreamingResponse(
        iter([data]), media_type="application/json", headers=_attachment(name)
    )


@router.post("/zip")
async def export_zip(
    conn: ConnDep, range: str = Query(default="24h"),  # noqa: A002
) -> StreamingResponse:
    since, until = _window_ms(range)
    path = await zip_bundle.build_export_zip(conn, since, until)
    name = f"pingwatch-{range}-{int(time.time())}.zip"
    return StreamingResponse(
        _iter_file(path), media_type="application/zip", headers=_attachment(name)
    )


@router.post("/db-snapshot")
async def export_db_snapshot(conn: ConnDep) -> StreamingResponse:
    fd, tmp = tempfile.mkstemp(prefix="pingwatch-", suffix=".sqlite")
    os.close(fd)
    path = Path(tmp)
    try:
        await db_snapshot.hot_db_snapshot(conn, path)
    except Exception as exc:
        path.unlink(missing_ok=True)  # noqa: ASYNC240  # error-path cleanup, blocking acceptable
        log.exception("db-snapshot-failed")
        raise HTTPException(
            status_code=503, detail=f"db-snapshot failed: {type(exc).__name__}"
        ) from exc
    name = f"pingwatch-{int(time.time())}.sqlite"
    return StreamingResponse(
        _iter_file(path),
        media_type="application/octet-stream",
        headers=_attachment(name),
    )


@router.post("/usb", response_model=OkResponse)
async def export_usb(
    conn: ConnDep,
    range: str = Query(default="24h"),  # noqa: A002
    format: str = Query(default="zip"),  # noqa: A002
) -> OkResponse:
    since, until = _window_ms(range)
    try:
        path = await zip_bundle.write_export_to_usb(conn, since, until, USB_ROOT)
    except FileNotFoundError:
        return OkResponse(ok=False, detail=f"USB not mounted at {USB_ROOT}")
    except Exception as exc:  # noqa: BLE001
        log.exception("usb-export-failed")
        return OkResponse(ok=False, detail=f"usb export failed: {type(exc).__name__}")
    return OkResponse(ok=True, detail=str(path))
