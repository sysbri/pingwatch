"""On-demand Speedtest via Cloudflare. Pausiert HttpTrickleWorker fuer Dauer."""
from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any

import httpx
import structlog

from pingwatch.db import queries

log = structlog.get_logger(__name__)

DOWN_URL = "https://speed.cloudflare.com/__down?bytes={n}"
UP_URL = "https://speed.cloudflare.com/__up"
LAT_URL = "https://speed.cloudflare.com/__down?bytes=0"
DOWN_BYTES = 25 * 1024 * 1024
UP_BYTES = 5 * 1024 * 1024
LATENCY_SAMPLES = 8
HARD_TIMEOUT_S = 45.0

_pause_trickle = asyncio.Event()


def trickle_should_pause() -> bool:
    return _pause_trickle.is_set()


def _build_client() -> httpx.AsyncClient:
    """Build an httpx client. Use http2 if the optional dep is installed."""
    headers = {"User-Agent": "PingWatch/0.1 (+speedtest)"}
    timeout = httpx.Timeout(HARD_TIMEOUT_S, connect=10.0)
    try:
        return httpx.AsyncClient(timeout=timeout, headers=headers, http2=True)
    except Exception:  # noqa: BLE001 — http2 dep optional
        return httpx.AsyncClient(timeout=timeout, headers=headers)


async def _latency(cli: httpx.AsyncClient) -> tuple[float, float]:
    """8 Samples, trim min+max, return (mean_ms, stdev_jitter_ms)."""
    samples_ms: list[float] = []
    for i in range(LATENCY_SAMPLES):
        t0 = time.perf_counter()
        try:
            r = await cli.get(LAT_URL)
            r.raise_for_status()
            samples_ms.append((time.perf_counter() - t0) * 1000.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("speedtest.latency_sample_failed", i=i, error=str(exc))
    if len(samples_ms) < 3:
        raise RuntimeError(f"latency: only {len(samples_ms)} valid samples")
    trimmed = sorted(samples_ms)[1:-1]
    mean_ms = statistics.mean(trimmed)
    jitter_ms = statistics.stdev(trimmed) if len(trimmed) > 1 else 0.0
    log.info(
        "speedtest.latency_done",
        mean_ms=round(mean_ms, 2),
        jitter_ms=round(jitter_ms, 2),
        n=len(trimmed),
    )
    return mean_ms, jitter_ms


async def _download(cli: httpx.AsyncClient) -> tuple[float, int]:
    """Stream DOWN_BYTES, return (mbps_round2, bytes_received)."""
    url = DOWN_URL.format(n=DOWN_BYTES)
    bytes_received = 0
    t0 = time.perf_counter()
    async with cli.stream("GET", url) as resp:
        resp.raise_for_status()
        async for piece in resp.aiter_bytes(chunk_size=64 * 1024):
            bytes_received += len(piece)
    elapsed = max(time.perf_counter() - t0, 1e-6)
    mbps = (bytes_received * 8.0) / 1_000_000.0 / elapsed
    log.info(
        "speedtest.download_done",
        bytes=bytes_received,
        elapsed_s=round(elapsed, 3),
        mbps=round(mbps, 2),
    )
    return round(mbps, 2), bytes_received


async def _upload(cli: httpx.AsyncClient) -> tuple[float, int]:
    """POST UP_BYTES zero-payload, return (mbps_round2, bytes_sent)."""
    chunk = b"\x00" * (64 * 1024)
    total = UP_BYTES
    chunks_full, rem = divmod(total, len(chunk))

    async def gen() -> Any:
        for _ in range(chunks_full):
            yield chunk
        if rem:
            yield b"\x00" * rem

    t0 = time.perf_counter()
    headers = {"Content-Length": str(total), "Content-Type": "application/octet-stream"}
    r = await cli.post(UP_URL, content=gen(), headers=headers)
    r.raise_for_status()
    elapsed = max(time.perf_counter() - t0, 1e-6)
    mbps = (total * 8.0) / 1_000_000.0 / elapsed
    log.info(
        "speedtest.upload_done",
        bytes=total,
        elapsed_s=round(elapsed, 3),
        mbps=round(mbps, 2),
    )
    return round(mbps, 2), total


async def run_speedtest(conn, task_id: str) -> dict[str, Any]:
    """Drive the 3-phase speedtest. Inserts a row with status='running' upfront,
    updates to 'done'/'failed' in the finally block. Pauses trickle worker."""
    ts_ms = int(time.time() * 1000)
    t_start = time.perf_counter()
    _pause_trickle.set()
    log.info("speedtest.start", task_id=task_id)

    # Insert running row
    await conn.execute(
        """
        INSERT INTO speedtests(ts_ms, server, status, task_id)
        VALUES (?, 'speed.cloudflare.com', 'running', ?)
        """,
        (ts_ms, task_id),
    )
    await conn.commit()

    result: dict[str, Any] = {
        "task_id": task_id,
        "status": "running",
        "ts_ms": ts_ms,
    }
    down_mbps: float | None = None
    up_mbps: float | None = None
    latency_ms: float | None = None
    jitter_ms: float | None = None
    bytes_down = 0
    bytes_up = 0
    err: str | None = None

    try:
        async with _build_client() as cli:
            # Phase 1: latency
            latency_ms, jitter_ms = await _latency(cli)
            # Phase 2: download
            down_mbps, bytes_down = await _download(cli)
            # Phase 3: upload
            up_mbps, bytes_up = await _upload(cli)
        result["status"] = "done"
    except asyncio.CancelledError:
        err = "cancelled"
        result["status"] = "aborted"
        log.warning("speedtest.cancelled", task_id=task_id)
        raise
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:200]
        result["status"] = "failed"
        log.warning("speedtest.failed", task_id=task_id, error=err)
    finally:
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        try:
            await conn.execute(
                """
                UPDATE speedtests SET
                    down_mbps = ?, up_mbps = ?, latency_ms = ?, jitter_ms = ?,
                    bytes_down = ?, bytes_up = ?, duration_ms = ?,
                    status = ?, error = ?
                WHERE task_id = ?
                """,
                (
                    down_mbps,
                    up_mbps,
                    round(latency_ms, 2) if latency_ms is not None else None,
                    round(jitter_ms, 2) if jitter_ms is not None else None,
                    bytes_down,
                    bytes_up,
                    duration_ms,
                    result["status"],
                    err,
                    task_id,
                ),
            )
            await conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("speedtest.persist_failed", task_id=task_id)
        _pause_trickle.clear()
        log.info(
            "speedtest.end",
            task_id=task_id,
            status=result["status"],
            duration_ms=duration_ms,
        )

    result.update(
        {
            "down_mbps": down_mbps,
            "up_mbps": up_mbps,
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
            "jitter_ms": round(jitter_ms, 2) if jitter_ms is not None else None,
            "bytes_down": bytes_down,
            "bytes_up": bytes_up,
            "duration_ms": duration_ms,
            "error": err,
        }
    )
    return result
