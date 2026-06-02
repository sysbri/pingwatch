"""FastAPI application factory.

Wires the foundation files (DB, bus) with every route module + WebSocket
endpoints, and runs the dashboard broadcaster as a background task while the
app is up.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pingwatch.api import ws as ws_mod
from pingwatch.api.routes import (
    dashboard,
    export,
    outages,
    pings,
    settings as settings_routes,
    speedtest,
    system,
    targets,
    traces,
    wifi,
)
from pingwatch.db.connection import DB

log = structlog.get_logger(__name__)

PKG_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"

DEFAULT_DB_PATH = os.environ.get("PINGWATCH_DB", "/data/pingwatch.db")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open DB and start the dashboard broadcaster on startup; tear down on stop.

    When the app is launched by ``pingwatch.main`` the orchestrator has already
    opened a shared DB and attached it to ``app.state.db`` before invoking
    uvicorn — in that case we reuse it instead of opening a second connection
    to the same file. When the app is launched standalone (e.g. via uvicorn
    directly for dev) we open our own.
    """
    db_path = getattr(app.state, "_db_path_override", None) or DEFAULT_DB_PATH
    owned_db: DB | None = None
    existing = getattr(app.state, "db", None)
    if existing is None:
        owned_db = DB(db_path)
        await owned_db.open()
        app.state.db = owned_db
        log.info("db-opened", path=db_path)

    def _conn() -> Any:
        d = app.state.db
        # Both DB (own wrapper) and raw aiosqlite.Connection are acceptable
        return getattr(d, "conn", d)

    broadcaster = asyncio.create_task(
        ws_mod.dashboard_broadcaster(_conn),
        name="dashboard-broadcaster",
    )
    app.state.broadcaster = broadcaster

    try:
        yield
    finally:
        broadcaster.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await broadcaster
        if owned_db is not None:
            await owned_db.close()
            log.info("db-closed")


def build_app(*, db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="PingWatch",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )
    if db_path is not None:
        app.state._db_path_override = db_path  # noqa: SLF001

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Boot-Timestamp fuer Liveness-Check (Probes brauchen Anlauf nach Cold-Start)
    app.state.boot_ts_ms = int(time.time() * 1000)

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz() -> JSONResponse:
        """Healthz prueft nicht nur HTTP sondern auch ob die Probe-Pipeline
        tatsaechlich Daten schreibt. Wenn der Container > 3 min hochlaeuft
        und in den letzten 3 min KEINE raw_pings angekommen sind, gilt das
        als unhealthy -> docker compose restart greift.
        """
        from time import time as _t
        now_ms = int(_t() * 1000)
        uptime_ms = now_ms - app.state.boot_ts_ms
        # Anlauf-Periode: erste 180s sind wir immer ok
        if uptime_ms < 180_000:
            return JSONResponse({"status": "ok", "phase": "warmup", "uptime_s": uptime_ms // 1000})
        # Liveness: pruefe ob in den letzten 3 min raw_pings angekommen sind
        try:
            db_obj = app.state.db
            conn = getattr(db_obj, "conn", db_obj)
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM raw_pings WHERE ts_ms >= ?",
                (now_ms - 180_000,),
            )
            row = await cur.fetchone()
            recent = int(row["n"] or 0) if row else 0
            if recent == 0:
                log.error("healthz.no_recent_pings", uptime_s=uptime_ms // 1000)
                return JSONResponse(
                    {"status": "unhealthy", "reason": "no raw_pings in last 180s",
                     "uptime_s": uptime_ms // 1000},
                    status_code=503,
                )
            return JSONResponse({"status": "ok", "recent_pings": recent,
                                 "uptime_s": uptime_ms // 1000})
        except Exception as exc:  # noqa: BLE001
            log.warning("healthz.db_check_failed", error=repr(exc))
            return JSONResponse({"status": "ok", "phase": "db-degraded"})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        ctx: dict[str, Any] = {
            "boot": {"now_ms": 0, "version": "0.1.0"},
        }
        return templates.TemplateResponse(request, "index.html", ctx)

    # Routers
    app.include_router(dashboard.router)
    app.include_router(targets.router)
    app.include_router(outages.router)
    app.include_router(traces.router)
    app.include_router(pings.router)
    app.include_router(settings_routes.router)
    app.include_router(speedtest.router)
    app.include_router(export.router)
    app.include_router(system.router)
    app.include_router(wifi.router)

    # WebSocket endpoints
    app.include_router(ws_mod.router)

    return app


app = build_app()
