"""WiFi management API — talks to host helper via /run/pingwatch-host.fifo."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from pingwatch.api import host_fifo
from pingwatch.api.deps import ConnDep

router = APIRouter(prefix="/api/wifi", tags=["wifi"])

_RESULT_DIR = Path("/run/pingwatch-shared")
_SCAN_FILE = _RESULT_DIR / "wifi-scan.json"
_STATUS_FILE = _RESULT_DIR / "wifi-status.json"
_RESULT_FILE = _RESULT_DIR / "wifi-result.json"

_FORBIDDEN_SSID = ("\t", "\n", "\x00", "\\")
_FORBIDDEN_PW = ("\n", "\x00", "\t")


def _validate_ssid(ssid: str) -> None:
    if not ssid or not ssid.strip():
        raise HTTPException(status_code=400, detail="ssid required")
    if len(ssid) > 32:
        raise HTTPException(status_code=400, detail="ssid too long")
    for ch in _FORBIDDEN_SSID:
        if ch in ssid:
            raise HTTPException(status_code=400, detail="ssid contains forbidden chars")


def _validate_password(pw: str) -> None:
    if len(pw) > 128:
        raise HTTPException(status_code=400, detail="password too long")
    for ch in _FORBIDDEN_PW:
        if ch in pw:
            raise HTTPException(status_code=400, detail="password contains forbidden chars")


async def _write_fifo(line: str) -> None:
    try:
        await host_fifo.write_command(line)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="host helper fifo missing") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail="host helper fifo not writable") from exc
    except (TimeoutError, OSError) as exc:
        raise HTTPException(status_code=503, detail="host helper not responding") from exc


async def _wait_for_file(
    path: Path, max_wait_s: float, min_mtime: float = 0.0
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            st = path.stat()  # noqa: ASYNC240  # polling loop, blocking is acceptable
            if st.st_mtime >= min_mtime and st.st_size > 0:
                try:
                    return json.loads(path.read_text(encoding="utf-8"))  # noqa: ASYNC240  # polling loop, blocking is acceptable
                except json.JSONDecodeError:
                    pass  # partial write, keep polling
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.2)
    return None


def _safe_unlink(path: Path) -> None:
    # /run/pingwatch-shared is bind-mounted read-only from the host; we
    # cannot delete files. Instead we rely on mtime-based marker checks
    # (only return file if its mtime is fresher than the request).
    # Suppress OSError too (Read-only filesystem).
    with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
        path.unlink()


@router.post("/scan")
async def scan() -> dict[str, Any]:
    marker = time.time() - 0.5  # accept files written from "now" onwards
    _safe_unlink(_SCAN_FILE)
    await _write_fifo("wifi_scan")
    data = await _wait_for_file(_SCAN_FILE, max_wait_s=8.0, min_mtime=marker)
    if data is None:
        raise HTTPException(status_code=504, detail="scan timeout")
    return data


@router.post("/connect")
async def connect(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008  # FastAPI dependency injection
    ssid = str(payload.get("ssid") or "").strip()
    password = str(payload.get("password") or "")
    _validate_ssid(ssid)
    _validate_password(password)

    marker = time.time() - 0.5
    _safe_unlink(_RESULT_FILE)
    await _write_fifo(f"wifi_connect\t{ssid}\t{password}")
    data = await _wait_for_file(_RESULT_FILE, max_wait_s=18.0, min_mtime=marker)
    if data is None:
        raise HTTPException(status_code=504, detail="connect timeout")

    if data.get("ok"):
        return {
            "ok": True,
            "ssid": data.get("ssid", ssid),
            "message": data.get("message", "connected"),
        }
    # Failure path — surface message, choose 400 for auth-like errors, 502 otherwise.
    msg = str(data.get("message", "connect failed"))
    lower = msg.lower()
    code = 400 if any(k in lower for k in ("password", "secret", "auth", "psk", "key")) else 502
    raise HTTPException(status_code=code, detail={"ok": False, "ssid": ssid, "message": msg})


@router.get("/status")
async def get_status() -> dict[str, Any]:
    # Use cached file if fresh (<8s old).
    try:
        st = _STATUS_FILE.stat()
        if (time.time() - st.st_mtime) < 8.0 and st.st_size > 0:
            try:
                return json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
    except FileNotFoundError:
        pass

    marker = time.time() - 0.5
    await _write_fifo("wifi_status")
    data = await _wait_for_file(_STATUS_FILE, max_wait_s=2.5, min_mtime=marker)
    if data is None:
        raise HTTPException(status_code=503, detail="status unavailable")
    return data


@router.post("/forget")
async def forget(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008  # FastAPI dependency injection
    ssid = str(payload.get("ssid") or "").strip()
    _validate_ssid(ssid)
    await _write_fifo(f"wifi_forget\t{ssid}")
    await asyncio.sleep(1.0)
    return {"ok": True, "ssid": ssid}


def _read_wifi_status_file() -> dict[str, Any] | None:
    """Lese /run/pingwatch-shared/wifi-status.json (live, optional)."""
    try:
        if _STATUS_FILE.exists():
            with open(_STATUS_FILE) as fh:
                return json.load(fh)
    except (OSError, ValueError):
        pass
    return None


@router.get("/overview")
async def overview_endpoint(
    conn: ConnDep,
    range_: str = Query(default="24h", alias="range"),
) -> dict[str, Any]:
    """WLAN-Detail-View payload: current + KPIs + series + events + APs."""
    range_map = {"1h": 3_600_000, "24h": 86_400_000, "7d": 7 * 86_400_000}
    window_ms = range_map.get(range_, 86_400_000)
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_ms

    # --- Current (live status file) ---
    live = _read_wifi_status_file() or {}
    current = {
        "ssid": live.get("ssid"),
        "bssid": live.get("bssid"),
        "rssi_dbm": live.get("rssi_dbm") if "rssi_dbm" in live else live.get("rssi"),
        "signal_pct": live.get("signal_pct"),
        "bitrate_mbps": live.get("bitrate_mbps"),
        "freq_mhz": live.get("freq") or live.get("freq_mhz"),
        "channel": live.get("channel"),
        "connected": bool(live.get("connected")),
        "security": live.get("security"),
    }

    # --- KPI: RSSI stats ---
    kcur = await conn.execute(
        "SELECT COUNT(*) AS n, AVG(rssi) AS rssi_mean, "
        "MIN(rssi) AS rssi_min, MAX(rssi) AS rssi_max "
        "FROM wifi_rssi_samples WHERE ts_ms >= ?",
        (since_ms,),
    )
    krow = await kcur.fetchone()
    samples_count = int(krow["n"] or 0) if krow else 0
    rssi_mean = float(krow["rssi_mean"]) if krow and krow["rssi_mean"] is not None else None
    rssi_min = int(krow["rssi_min"]) if krow and krow["rssi_min"] is not None else None
    rssi_max = int(krow["rssi_max"]) if krow and krow["rssi_max"] is not None else None

    # --- KPI: Event counts ---
    ecur = await conn.execute(
        "SELECT event_type, COUNT(*) AS cnt FROM wifi_events "
        "WHERE ts_ms >= ? GROUP BY event_type",
        (since_ms,),
    )
    ev_counts: dict[str, int] = {
        row["event_type"]: int(row["cnt"]) for row in await ecur.fetchall()
    }
    kpi = {
        "rssi_mean": round(rssi_mean, 1) if rssi_mean is not None else None,
        "rssi_min": rssi_min,
        "rssi_max": rssi_max,
        "samples_count": samples_count,
        "disconnects_count": ev_counts.get("disconnect", 0),
        "reassocs_count": ev_counts.get("reassoc", 0),
        "channel_changes_count": ev_counts.get("channel_change", 0),
        "ssid_changes_count": ev_counts.get("ssid_change", 0),
    }

    # --- Series (downsample auf max 1000 Punkte, analog pings.py) ---
    scur = await conn.execute(
        "SELECT ts_ms, rssi FROM wifi_rssi_samples "
        "WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (since_ms,),
    )
    raw_series = [{"ts_ms": int(r["ts_ms"]), "rssi": int(r["rssi"])}
                  for r in await scur.fetchall() if r["rssi"] is not None]
    max_points = 1000
    if len(raw_series) > max_points:
        bucket_size = len(raw_series) // max_points + 1
        downsampled: list[dict[str, Any]] = []
        for i in range(0, len(raw_series), bucket_size):
            bucket = raw_series[i:i + bucket_size]
            avg_rssi = sum(b["rssi"] for b in bucket) // len(bucket)
            downsampled.append({
                "ts_ms": bucket[len(bucket) // 2]["ts_ms"],
                "rssi": avg_rssi,
            })
        series = downsampled
    else:
        series = raw_series

    # --- Events (letzte 50 im Fenster) ---
    evcur = await conn.execute(
        "SELECT id, ts_ms, event_type, ssid, bssid, rssi, channel, duration_ms "
        "FROM wifi_events WHERE ts_ms >= ? ORDER BY ts_ms DESC LIMIT 50",
        (since_ms,),
    )
    events = [dict(r) for r in await evcur.fetchall()]

    # --- AP-Liste (distinct BSSIDs im Fenster) ---
    acur = await conn.execute(
        "SELECT bssid, ssid, MIN(ts_ms) AS first_seen_ms, MAX(ts_ms) AS last_seen_ms, "
        "COUNT(*) AS seen_count, AVG(rssi) AS avg_rssi "
        "FROM wifi_events WHERE ts_ms >= ? AND bssid IS NOT NULL "
        "GROUP BY bssid ORDER BY last_seen_ms DESC LIMIT 20",
        (since_ms,),
    )
    aps = []
    for r in await acur.fetchall():
        d = dict(r)
        if d.get("avg_rssi") is not None:
            d["avg_rssi"] = round(float(d["avg_rssi"]), 1)
        aps.append(d)

    return {
        "range": range_,
        "since_ms": since_ms,
        "now_ms": now_ms,
        "current": current,
        "kpi": kpi,
        "series": series,
        "events": events,
        "aps": aps,
    }
