"""Export endpoints — CSV / JSON / ZIP / SQLite snapshot / USB.

The actual export builders live in `pingwatch.export.*` (parallel agent). We
import them lazily so the API still loads when those modules don't exist yet.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from pingwatch.api import _queries_compat as q
from pingwatch.api.deps import ConnDep
from pingwatch.api.schemas import OkResponse

router = APIRouter(prefix="/api/export", tags=["export"])


def _range_to_window_ms(range_str: str | None) -> int:
    return {
        "1h": 3_600_000,
        "24h": 86_400_000,
        "7d": 7 * 86_400_000,
        "30d": 30 * 86_400_000,
        "all": 365 * 86_400_000,
    }.get(range_str or "24h", 86_400_000)


@router.post("/csv")
async def export_csv(
    conn: ConnDep, range: str = Query(default="24h"),  # noqa: A002
) -> StreamingResponse:
    try:
        from pingwatch.export import csv_exporter  # type: ignore[attr-defined]

        stream = csv_exporter.build_stream(conn, range)  # async iterator
        filename = f"pingwatch-{range}-{int(time.time())}.csv"
        return StreamingResponse(
            stream,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception:  # noqa: BLE001
        # Fallback minimal CSV: dump raw_pings.
        window_ms = _range_to_window_ms(range)
        since = int(time.time() * 1000) - window_ms
        buf = io.StringIO()
        buf.write("ts_ms,dest_id,success,latency_us,error_kind,flags\n")
        cur = await conn.execute(
            "SELECT ts_ms, dest_id, success, latency_us, error_kind, flags "
            "FROM raw_pings WHERE ts_ms >= ? ORDER BY ts_ms",
            (since,),
        )
        async for r in cur:
            buf.write(
                f"{r['ts_ms']},{r['dest_id']},{r['success']},"
                f"{r['latency_us'] or ''},{r['error_kind'] or ''},{r['flags']}\n"
            )
        body = buf.getvalue().encode()
        return StreamingResponse(
            iter([body]),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=pingwatch-{range}.csv"
                )
            },
        )


@router.post("/json")
async def export_json(
    conn: ConnDep, range: str = Query(default="24h"),  # noqa: A002
) -> StreamingResponse:
    try:
        from pingwatch.export import json_exporter  # type: ignore[attr-defined]

        payload = await json_exporter.build_dict(conn, range)
    except Exception:  # noqa: BLE001
        window_ms = _range_to_window_ms(range)
        since = int(time.time() * 1000) - window_ms
        rows, _ = await q.list_outages(conn, since_ms=since, limit=1000)
        payload = {
            "range": range,
            "exported_at_ms": int(time.time() * 1000),
            "outages": rows,
            "settings": await q.get_all_settings(conn),
        }
    data = json.dumps(payload, indent=2).encode()
    return StreamingResponse(
        iter([data]),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f"attachment; filename=pingwatch-{range}-{int(time.time())}.json"
            )
        },
    )


@router.post("/zip")
async def export_zip(
    conn: ConnDep, range: str = Query(default="24h"),  # noqa: A002
) -> StreamingResponse:
    try:
        from pingwatch.export import zip_bundle  # type: ignore[attr-defined]

        data = await zip_bundle.build_bytes(conn, range)
    except Exception:  # noqa: BLE001
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            settings = await q.get_all_settings(conn)
            zf.writestr("settings.json", json.dumps(settings, indent=2))
            zf.writestr(
                "info.txt",
                f"PingWatch export\ngenerated: {int(time.time())}\nrange: {range}\n",
            )
        data = buf.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=pingwatch-{range}-{int(time.time())}.zip"
            )
        },
    )


@router.post("/db-snapshot")
async def export_db_snapshot(conn: ConnDep) -> StreamingResponse:
    try:
        from pingwatch.export import db_snapshot  # type: ignore[attr-defined]

        path = await db_snapshot.dump_to_tempfile(conn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"db-snapshot exporter unavailable: {type(exc).__name__}",
        ) from exc

    def _stream() -> Any:
        with open(path, "rb") as fh:
            while chunk := fh.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename=pingwatch-{int(time.time())}.sqlite"
            )
        },
    )


@router.post("/usb", response_model=OkResponse)
async def export_usb(
    conn: ConnDep,  # noqa: ARG001
    format: str = Query(default="zip"),  # noqa: A002
    range: str = Query(default="24h"),  # noqa: A002
) -> OkResponse:
    target_dir = Path("/media/pingwatch-usb")
    if not target_dir.exists():  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
        return OkResponse(ok=False, detail="USB not mounted at /media/pingwatch-usb")
    try:
        from pingwatch.export import usb_writer  # type: ignore[attr-defined]

        path = await usb_writer.write(target_dir, format, range)
    except Exception as exc:  # noqa: BLE001
        return OkResponse(
            ok=False, detail=f"usb-writer unavailable: {type(exc).__name__}"
        )
    return OkResponse(ok=True, detail=str(path))
