"""PingWatch process entrypoint.

Owns the asyncio.TaskGroup that runs every subsystem in a single process:
shared DB connection, shared bus, lazy-imported workers, graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from importlib import import_module
from typing import Any

import structlog
import uvicorn

from pingwatch.api.app import build_app
from pingwatch.bus import Bus, get_bus
from pingwatch.config import Settings, get_settings
from pingwatch.db import queries
from pingwatch.db.connection import DB

log = structlog.get_logger(__name__)


def _lazy(module: str, attr: str) -> Any | None:
    """Import a worker symbol on demand. Returns None and logs if missing."""
    try:
        mod = import_module(module)
    except Exception as e:  # noqa: BLE001
        log.warning("worker.import_failed", module=module, error=repr(e))
        return None
    obj = getattr(mod, attr, None)
    if obj is None:
        log.warning("worker.symbol_missing", module=module, attr=attr)
    return obj


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            # Windows / non-POSIX — best-effort.
            loop.add_signal_handler(sig, stop.set)


async def _run_uvicorn(
    settings: Settings, db: Any, bus: Bus, stop: asyncio.Event
) -> None:
    """Run uvicorn and shut it down gracefully via stop event.

    Setting ``server.should_exit = True`` is uvicorn's documented graceful path;
    cancelling the task directly leaves the lifespan's receive() awaiting
    forever and produces a noisy CancelledError on stderr.
    """
    app = build_app()
    app.state.db = db
    app.state.bus = bus
    app.state.settings = settings
    config = uvicorn.Config(
        app,
        host=settings.bind.host,
        port=settings.bind.port,
        log_level="info",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    # Install signal handlers ourselves; uvicorn would also try but it doesn't
    # know about the rest of the TaskGroup.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _trip_should_exit() -> None:
        await stop.wait()
        server.should_exit = True

    trip = asyncio.create_task(_trip_should_exit(), name="uvicorn-trip")
    try:
        await server.serve()
    finally:
        trip.cancel()
        with suppress(asyncio.CancelledError):
            await trip


async def _spawn_worker(
    tg: asyncio.TaskGroup,
    workers: list[asyncio.Task[None]],
    name: str,
    module: str,
    attr: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Lazily import + start a worker coroutine inside the task group.

    Returns by appending the created task to ``workers`` so the orchestrator
    can cancel exactly its own children at shutdown without touching FastAPI /
    uvicorn internals.
    """
    fn = _lazy(module, attr)
    if fn is None:
        return

    async def _wrapped() -> None:
        log.info("worker.start", name=name)
        try:
            result = fn(*args, **kwargs)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            log.info("worker.cancelled", name=name)
            raise
        except Exception as e:  # noqa: BLE001
            log.error("worker.crashed", name=name, error=repr(e))

    task = tg.create_task(_wrapped(), name=name)
    workers.append(task)


async def serve() -> None:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    settings = get_settings()
    bus = get_bus()
    db = DB(settings.paths.db)
    conn = await db.open()
    log.info("boot.db_ready", path=str(settings.paths.db))

    # Reap any speedtests left in 'running' from a previous unclean shutdown.
    aborted = await queries.cleanup_running_speedtests(conn)
    if aborted:
        log.info("boot.speedtest_cleanup", aborted=aborted)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    workers: list[asyncio.Task[None]] = []

    try:
        async with asyncio.TaskGroup() as tg:
            # Uvicorn (HTTP + WS) — drains gracefully via the shared stop event.
            tg.create_task(_run_uvicorn(settings, db, bus, stop), name="uvicorn")

            # Persister: single writer to SQLite from bus topics.
            await _spawn_worker(
                tg, workers, "persister", "pingwatch.probes.persister", "run_persister", conn, bus
            )
            # Probe runner: 1 task per destination.
            await _spawn_worker(
                tg, workers, "probe_runner",
                "pingwatch.probes.runner", "run_probe_runner", conn, bus,
            )
            # Metrics: rolling aggregator + hourly rollup writer.
            await _spawn_worker(
                tg, workers, "metrics_aggregator",
                "pingwatch.metrics.aggregator", "run_aggregator", conn, bus,
            )
            await _spawn_worker(
                tg, workers, "hourly_rollup",
                "pingwatch.metrics.hourly", "run_hourly_rollup", conn, bus,
            )
            # Streams.
            await _spawn_worker(
                tg, workers, "http_trickle",
                "pingwatch.streams.http_trickle", "run_http_trickle", conn, bus,
            )
            await _spawn_worker(
                tg, workers, "tcp_heartbeat",
                "pingwatch.streams.tcp_heartbeat", "run_tcp_heartbeat", conn, bus,
            )
            # WiFi monitoring.
            await _spawn_worker(
                tg, workers, "wifi_monitor",
                "pingwatch.wifi.monitor", "run_wifi_monitor", conn, bus,
            )
            # Traceroute scheduler.
            await _spawn_worker(
                tg, workers, "trace_scheduler",
                "pingwatch.traceroute.scheduler", "run_trace_scheduler", conn, bus,
            )
            # Outage detection + correlation.
            await _spawn_worker(
                tg, workers, "outage_detector",
                "pingwatch.outages.detector", "run_outage_detector", conn, bus,
            )
            await _spawn_worker(
                tg, workers, "outage_correlator",
                "pingwatch.outages.correlator", "run_outage_correlator", conn, bus,
            )
            # WLAN classifier subscriber: retags outages when wifi events arrive.
            await _spawn_worker(
                tg, workers, "wlan_classifier",
                "pingwatch.outages.classifier", "run_classifier_subscriber", conn, bus,
            )
            # Retention.
            await _spawn_worker(
                tg, workers, "retention", "pingwatch.db.retention", "run_retention", conn,
            )
            # Pi metrics sampler (CPU %).
            await _spawn_worker(
                tg, workers, "pi_metrics_sampler",
                "pingwatch.system.pi_metrics", "pi_metrics_sampler",
            )
            await stop.wait()
            log.info("shutdown.draining", workers=len(workers))
            # Uvicorn observes the stop event and exits serve() naturally; that
            # lets FastAPI run its lifespan shutdown cleanly. Cancel only our
            # own workers — anything FastAPI/starlette/uvicorn spawned internally
            # is none of our business.
            for t in workers:
                if not t.done():
                    t.cancel()
    except* asyncio.CancelledError:
        # Expected during graceful shutdown.
        pass
    except* Exception as eg:  # noqa: BLE001
        log.error("taskgroup.error", error=repr(eg))
    finally:
        with suppress(Exception):
            await db.close()
        log.info("shutdown.complete")


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
