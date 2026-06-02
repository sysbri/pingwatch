"""On-demand Speedtest mit Provider-Dispatch.

Provider:
  - cloudflare    : httpx -> speed.cloudflare.com (~30 MB)
  - speedtest_net : Ookla via speedtest-cli (PyPI) (~30 MB)
  - iperf3        : LAN-Test via iperf3 CLI (kein Internet-Volumen)

Alle drei pausieren den HttpTrickleWorker via `_pause_trickle`.
"""
from __future__ import annotations

import asyncio
import json
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
IPERF3_TIMEOUT_S = 20.0
SPEEDTEST_NET_TIMEOUT_S = 45.0

_pause_trickle = asyncio.Event()


def trickle_should_pause() -> bool:
    return _pause_trickle.is_set()


def _build_client() -> httpx.AsyncClient:
    headers = {"User-Agent": "PingWatch/0.1 (+speedtest)"}
    timeout = httpx.Timeout(HARD_TIMEOUT_S, connect=10.0)
    try:
        return httpx.AsyncClient(timeout=timeout, headers=headers, http2=True)
    except Exception:  # noqa: BLE001
        return httpx.AsyncClient(timeout=timeout, headers=headers)


async def _latency(cli: httpx.AsyncClient) -> tuple[float, float]:
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
    return mean_ms, jitter_ms


async def _download(cli: httpx.AsyncClient) -> tuple[float, int]:
    url = DOWN_URL.format(n=DOWN_BYTES)
    bytes_received = 0
    t0 = time.perf_counter()
    async with cli.stream("GET", url) as resp:
        resp.raise_for_status()
        async for piece in resp.aiter_bytes(chunk_size=64 * 1024):
            bytes_received += len(piece)
    elapsed = max(time.perf_counter() - t0, 1e-6)
    mbps = (bytes_received * 8.0) / 1_000_000.0 / elapsed
    return round(mbps, 2), bytes_received


async def _upload(cli: httpx.AsyncClient) -> tuple[float, int]:
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
    return round(mbps, 2), total


def _persist_result_sync_payload(
    *,
    down_mbps: float | None,
    up_mbps: float | None,
    latency_ms: float | None,
    jitter_ms: float | None,
    bytes_down: int,
    bytes_up: int,
    duration_ms: int,
    server: str | None,
) -> dict[str, Any]:
    return {
        "down_mbps": down_mbps,
        "up_mbps": up_mbps,
        "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
        "jitter_ms": round(jitter_ms, 2) if jitter_ms is not None else None,
        "bytes_down": bytes_down,
        "bytes_up": bytes_up,
        "duration_ms": duration_ms,
        "server": server,
    }


# ---------------- Provider: Cloudflare ----------------
async def _run_cloudflare(conn, task_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    server = "speed.cloudflare.com"
    ts_ms = int(time.time() * 1000)
    t_start = time.perf_counter()
    _pause_trickle.set()
    log.info("speedtest.start", task_id=task_id, provider="cloudflare")

    await queries.insert_speedtest_start(conn, task_id, ts_ms)

    result: dict[str, Any] = {"task_id": task_id, "status": "running", "ts_ms": ts_ms}
    down_mbps = up_mbps = latency_ms = jitter_ms = None
    bytes_down = bytes_up = 0
    err: str | None = None
    duration_ms = 0
    try:
        async with _build_client() as cli:
            latency_ms, jitter_ms = await _latency(cli)
            down_mbps, bytes_down = await _download(cli)
            up_mbps, bytes_up = await _upload(cli)
        result["status"] = "done"
    except asyncio.CancelledError:
        err = "cancelled"
        result["status"] = "aborted"
        raise
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:200]
        result["status"] = "failed"
        log.warning("speedtest.failed", task_id=task_id, error=err)
    finally:
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        payload = _persist_result_sync_payload(
            down_mbps=down_mbps, up_mbps=up_mbps,
            latency_ms=latency_ms, jitter_ms=jitter_ms,
            bytes_down=bytes_down, bytes_up=bytes_up,
            duration_ms=duration_ms, server=server,
        )
        try:
            if result["status"] == "done":
                await queries.update_speedtest_done(conn, task_id, payload)
            else:
                await queries.update_speedtest_failed(conn, task_id, err or "unknown")
        except Exception:  # noqa: BLE001
            log.exception("speedtest.persist_failed", task_id=task_id)
        _pause_trickle.clear()

    result.update(payload)
    result["error"] = err
    return result


# ---------------- Provider: Ookla speedtest-cli ----------------
def _run_speedtest_net_sync() -> dict[str, Any]:
    import speedtest  # type: ignore
    s = speedtest.Speedtest(secure=True)
    s.get_servers()
    best = s.get_best_server()
    down_bps = s.download()
    up_bps = s.upload()
    res = s.results
    server_name = f"{best.get('name','?')} ({best.get('sponsor','?')})"
    return {
        "down_mbps": round(down_bps / 1_000_000.0, 2),
        "up_mbps": round(up_bps / 1_000_000.0, 2),
        "latency_ms": float(res.ping),
        "server": server_name,
        "bytes_down": int(getattr(res, "bytes_received", 0) or 0),
        "bytes_up": int(getattr(res, "bytes_sent", 0) or 0),
    }


async def _run_speedtest_net(conn, task_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    ts_ms = int(time.time() * 1000)
    t_start = time.perf_counter()
    _pause_trickle.set()
    log.info("speedtest.start", task_id=task_id, provider="speedtest_net")

    await queries.insert_speedtest_start(conn, task_id, ts_ms)

    result: dict[str, Any] = {"task_id": task_id, "status": "running", "ts_ms": ts_ms}
    down_mbps = up_mbps = latency_ms = None
    jitter_ms = None
    bytes_down = bytes_up = 0
    server = "speedtest.net"
    err: str | None = None
    duration_ms = 0
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_run_speedtest_net_sync),
            timeout=SPEEDTEST_NET_TIMEOUT_S,
        )
        down_mbps = data["down_mbps"]
        up_mbps = data["up_mbps"]
        latency_ms = data["latency_ms"]
        server = data["server"]
        bytes_down = data["bytes_down"]
        bytes_up = data["bytes_up"]
        result["status"] = "done"
    except asyncio.CancelledError:
        err = "cancelled"
        result["status"] = "aborted"
        raise
    except TimeoutError:
        err = "timeout"
        result["status"] = "failed"
        log.warning("speedtest.failed", task_id=task_id, error=err, provider="speedtest_net")
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:200]
        result["status"] = "failed"
        log.warning("speedtest.failed", task_id=task_id, error=err, provider="speedtest_net")
    finally:
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        payload = _persist_result_sync_payload(
            down_mbps=down_mbps, up_mbps=up_mbps,
            latency_ms=latency_ms, jitter_ms=jitter_ms,
            bytes_down=bytes_down, bytes_up=bytes_up,
            duration_ms=duration_ms, server=server,
        )
        try:
            if result["status"] == "done":
                await queries.update_speedtest_done(conn, task_id, payload)
            else:
                await queries.update_speedtest_failed(conn, task_id, err or "unknown")
        except Exception:  # noqa: BLE001
            log.exception("speedtest.persist_failed", task_id=task_id)
        _pause_trickle.clear()

    result.update(payload)
    result["error"] = err
    return result


# ---------------- Provider: iperf3 LAN ----------------
async def _iperf3_run(host: str, port: int, reverse: bool) -> dict[str, Any]:
    args = ["iperf3", "-c", host, "-p", str(port), "-J", "-t", "5"]
    if reverse:
        args.append("-R")
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=IPERF3_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"iperf3 rc={proc.returncode}: {stderr.decode(errors='ignore')[:160]}")
    try:
        return json.loads(stdout.decode())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"iperf3 json parse: {exc}") from exc


async def _run_iperf3(conn, task_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    host = (settings.get("speedtest.iperf3_server") or "").strip()
    try:
        port = int(settings.get("speedtest.iperf3_port") or 5201)
    except (TypeError, ValueError):
        port = 5201

    ts_ms = int(time.time() * 1000)
    t_start = time.perf_counter()
    _pause_trickle.set()
    log.info("speedtest.start", task_id=task_id, provider="iperf3", host=host, port=port)

    await queries.insert_speedtest_start(conn, task_id, ts_ms)

    result: dict[str, Any] = {"task_id": task_id, "status": "running", "ts_ms": ts_ms}
    down_mbps = up_mbps = latency_ms = None
    jitter_ms = None
    bytes_down = bytes_up = 0
    server = f"iperf3 @ {host}:{port}" if host else "iperf3 (unconfigured)"
    err: str | None = None
    duration_ms = 0
    try:
        if not host:
            raise RuntimeError("iperf3 server not configured")
        # Upload (client -> server, default direction)
        up_json = await _iperf3_run(host, port, reverse=False)
        up_bps = (
            up_json.get("end", {}).get("sum_sent", {}).get("bits_per_second")
            or up_json.get("end", {}).get("sum_received", {}).get("bits_per_second")
            or 0.0
        )
        up_mbps = round(float(up_bps) / 1_000_000.0, 2)
        bytes_up = int(up_json.get("end", {}).get("sum_sent", {}).get("bytes", 0) or 0)
        # Download (reverse: server -> client)
        down_json = await _iperf3_run(host, port, reverse=True)
        down_bps = (
            down_json.get("end", {}).get("sum_received", {}).get("bits_per_second")
            or down_json.get("end", {}).get("sum_sent", {}).get("bits_per_second")
            or 0.0
        )
        down_mbps = round(float(down_bps) / 1_000_000.0, 2)
        bytes_down = int(down_json.get("end", {}).get("sum_received", {}).get("bytes", 0) or 0)
        # RTT aus iperf3 streams (mean_rtt is microseconds)
        try:
            streams = down_json.get("end", {}).get("streams", [])
            if streams:
                sender = streams[0].get("sender", {})
                mean_rtt_us = sender.get("mean_rtt")
                if mean_rtt_us:
                    latency_ms = float(mean_rtt_us) / 1000.0
        except Exception:  # noqa: BLE001,S110
            pass
        host_connect = down_json.get("start", {}).get("connecting_to", {}).get("host") or host
        server = f"iperf3 @ {host_connect}:{port}"
        result["status"] = "done"
    except asyncio.CancelledError:
        err = "cancelled"
        result["status"] = "aborted"
        raise
    except TimeoutError:
        err = "iperf3 timeout"
        result["status"] = "failed"
        log.warning("speedtest.failed", task_id=task_id, error=err, provider="iperf3")
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:200]
        result["status"] = "failed"
        log.warning("speedtest.failed", task_id=task_id, error=err, provider="iperf3")
    finally:
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        payload = _persist_result_sync_payload(
            down_mbps=down_mbps, up_mbps=up_mbps,
            latency_ms=latency_ms, jitter_ms=jitter_ms,
            bytes_down=bytes_down, bytes_up=bytes_up,
            duration_ms=duration_ms, server=server,
        )
        try:
            if result["status"] == "done":
                await queries.update_speedtest_done(conn, task_id, payload)
            else:
                await queries.update_speedtest_failed(conn, task_id, err or "unknown")
        except Exception:  # noqa: BLE001
            log.exception("speedtest.persist_failed", task_id=task_id)
        _pause_trickle.clear()

    result.update(payload)
    result["error"] = err
    return result


# ---------------- Public Dispatcher ----------------
async def run_speedtest(
    conn, task_id: str, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Dispatch to provider based on settings['speedtest.provider']. Defaults to cloudflare."""
    settings = settings or {}
    provider = (settings.get("speedtest.provider") or "cloudflare").strip().lower()
    if provider == "speedtest_net":
        return await _run_speedtest_net(conn, task_id, settings)
    if provider == "iperf3":
        return await _run_iperf3(conn, task_id, settings)
    return await _run_cloudflare(conn, task_id, settings)
