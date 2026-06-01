"""Raspberry Pi system metrics.

Reads from /proc, /sys, and disk_usage. Maintains a 1 Hz background sampler
for CPU usage (requires deltas between two /proc/stat reads).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


_state: dict[str, Any] = {
    "cpu_pct": 0.0,
    "_prev_idle": 0,
    "_prev_total": 0,
}


def _read_cpu_jiffies() -> tuple[int, int]:
    """Return (idle+iowait, total) jiffies from /proc/stat 'cpu' aggregate line."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
    except OSError:
        return 0, 0
    parts = line.split()
    if len(parts) < 8 or parts[0] != "cpu":
        return 0, 0
    nums = [int(x) for x in parts[1:8]]
    user, nice, system, idle, iowait, irq, softirq = nums
    idle_total = idle + iowait
    total = user + nice + system + idle + iowait + irq + softirq
    return idle_total, total


def _read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                value = rest.strip().split()
                if value:
                    try:
                        out[key.strip()] = int(value[0])  # kB
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _read_temp_c() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return int(raw) / 1000.0
    except (OSError, ValueError):
        return None


def _read_uptime_seconds() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_pi_model() -> str | None:
    try:
        # devicetree base/model is NUL-terminated.
        raw = Path("/sys/firmware/devicetree/base/model").read_bytes()
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip() or None
    except OSError:
        return None


def _read_kernel() -> str | None:
    try:
        return Path("/proc/sys/kernel/osrelease").read_text().strip()
    except OSError:
        return None


async def read_pi_metrics() -> dict[str, Any]:
    """One-shot snapshot of the host metrics.

    CPU % uses the value updated by `pi_metrics_sampler`. If the sampler hasn't
    run yet, we take a synchronous 100 ms sample inline (best-effort).
    """
    if _state.get("_prev_total", 0) == 0:
        await _sample_cpu_once(interval=0.1)

    mem = _read_meminfo()
    mem_total_mb = mem.get("MemTotal", 0) // 1024
    mem_avail_mb = mem.get("MemAvailable", 0) // 1024
    mem_used_mb = max(mem_total_mb - mem_avail_mb, 0)

    try:
        usage = shutil.disk_usage("/")
        sd_total_gb = usage.total / (1024**3)
        sd_used_gb = (usage.total - usage.free) / (1024**3)
    except OSError:
        sd_total_gb = 0.0
        sd_used_gb = 0.0

    return {
        "cpu_pct": round(float(_state.get("cpu_pct", 0.0)), 1),
        "ram_used_mb": mem_used_mb,
        "ram_total_mb": mem_total_mb,
        "temp_c": _read_temp_c(),
        "sd_used_gb": round(sd_used_gb, 2),
        "sd_total_gb": round(sd_total_gb, 2),
        "uptime_seconds": _read_uptime_seconds(),
        "kernel": _read_kernel(),
        "hostname": socket.gethostname(),
        "model": _read_pi_model(),
    }


async def _sample_cpu_once(interval: float = 1.0) -> None:
    idle1, total1 = _read_cpu_jiffies()
    await asyncio.sleep(interval)
    idle2, total2 = _read_cpu_jiffies()
    d_total = total2 - total1
    d_idle = idle2 - idle1
    if d_total > 0:
        _state["cpu_pct"] = max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))
    _state["_prev_idle"] = idle2
    _state["_prev_total"] = total2


async def pi_metrics_sampler() -> None:
    """1 Hz background CPU sampler. Runs forever; cancel to stop."""
    log.info("pi_metrics_sampler.start")
    try:
        while True:
            await _sample_cpu_once(interval=1.0)
    except asyncio.CancelledError:
        log.info("pi_metrics_sampler.stop")
        raise
