"""WiFi management API — talks to host helper via /run/pingwatch-host.fifo."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, status

router = APIRouter(prefix="/api/wifi", tags=["wifi"])

_FIFO = "/run/pingwatch-host.fifo"
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


def _write_fifo_sync(line: str) -> None:
    # FIFO write is blocking until a reader is present; helper is always listening.
    # Use os.open with O_WRONLY (no O_NONBLOCK so we block-wait if helper is busy).
    fd = os.open(_FIFO, os.O_WRONLY)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


async def _write_fifo(line: str) -> None:
    if not line.endswith("\n"):
        line += "\n"
    try:
        await asyncio.wait_for(asyncio.to_thread(_write_fifo_sync, line), timeout=3.0)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="host helper fifo missing") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail="host helper fifo not writable") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="host helper not responding") from exc


async def _wait_for_file(path: Path, max_wait_s: float, min_mtime: float = 0.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            st = path.stat()
            if st.st_mtime >= min_mtime and st.st_size > 0:
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
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
async def connect(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
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
        return {"ok": True, "ssid": data.get("ssid", ssid), "message": data.get("message", "connected")}
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
async def forget(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ssid = str(payload.get("ssid") or "").strip()
    _validate_ssid(ssid)
    await _write_fifo(f"wifi_forget\t{ssid}")
    await asyncio.sleep(1.0)
    return {"ok": True, "ssid": ssid}
