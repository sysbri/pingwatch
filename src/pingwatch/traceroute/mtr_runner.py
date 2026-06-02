"""Wrap ``mtr --json`` as an async subprocess.

The JSON layout is ``{"report": {"mtr": {...}, "hubs": [...]}}``. Hubs
provide ``count`` (hop number), ``host`` (``???`` for timeouts), and the
loss/avg/last/best/wrst fields in ms (floats).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from typing import Any

import structlog

from pingwatch.models import TraceHop, TraceSnapshot, TraceTrigger

log = structlog.get_logger(__name__)


async def run_mtr(
    host: str,
    *,
    dest_id: int = 0,
    trigger: TraceTrigger = TraceTrigger.SCHEDULED,
    max_hops: int = 30,
    timeout: float = 30.0,  # noqa: ASYNC109  # explicit timeout param is intentional
    cycles: int = 1,
) -> TraceSnapshot:
    """Run ``mtr --json -c <cycles> -n --max-ttl <hops> <host>`` and parse.

    Returns a partial snapshot on timeout (``success=False``).
    """
    cmd = [
        "mtr",
        "--json",
        "-c",
        str(cycles),
        "-n",
        "--max-ttl",
        str(max_hops),
        host,
    ]
    ts_ms = int(time.time() * 1000)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("mtr.binary_missing")
        return TraceSnapshot(
            dest_id=dest_id,
            ts_ms=ts_ms,
            trigger=trigger,
            success=False,
            hops=[],
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        log.warning("mtr.timeout", host=host)
        return TraceSnapshot(
            dest_id=dest_id,
            ts_ms=ts_ms,
            trigger=trigger,
            success=False,
            hops=[],
        )

    if proc.returncode != 0:
        log.warning(
            "mtr.nonzero_exit",
            host=host,
            code=proc.returncode,
            stderr=stderr.decode("utf-8", errors="replace")[:200],
        )

    hops = parse_mtr_json(stdout)
    last_hop_ip = hops[-1].ip if hops else None
    success = bool(hops) and last_hop_ip not in (None, "???")
    return TraceSnapshot(
        dest_id=dest_id,
        ts_ms=ts_ms,
        trigger=trigger,
        success=success,
        hops=hops,
    )


def parse_mtr_json(raw: bytes | str) -> list[TraceHop]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("mtr.json_parse_failed", text=text[:200])
        return []
    if not isinstance(data, Mapping):
        return []
    report = data.get("report") or {}
    hubs = report.get("hubs") or []
    hops: list[TraceHop] = []
    for hub in hubs:
        if not isinstance(hub, Mapping):
            continue
        hop_no_raw = hub.get("count")
        try:
            hop_no = int(hop_no_raw) if hop_no_raw is not None else len(hops) + 1
        except (TypeError, ValueError):
            hop_no = len(hops) + 1
        host_raw = hub.get("host")
        host_str: str | None
        ip: str | None
        if host_raw in (None, "???"):
            host_str = None
            ip = None
        else:
            host_str = str(host_raw)
            ip = host_str
        hops.append(
            TraceHop(
                hop_no=hop_no,
                host=host_str,
                ip=ip,
                rtt_us=_ms_to_us(
                    hub.get("Avg") or hub.get("avg") or hub.get("Last") or hub.get("last")
                ),
                rtt_min_us=_ms_to_us(hub.get("Best") or hub.get("best")),
                rtt_max_us=_ms_to_us(
                    hub.get("Wrst") or hub.get("wrst") or hub.get("Worst") or hub.get("worst")
                ),
                loss_pct=_to_float(hub.get("Loss%") or hub.get("loss") or hub.get("Loss")) or 0.0,
            )
        )
    return hops


def _ms_to_us(val: Any) -> int | None:
    if val is None:
        return None
    try:
        ms = float(val)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return int(ms * 1000)


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
