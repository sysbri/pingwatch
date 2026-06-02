"""ZIP bundling: combined export, USB write, support diagnose bundle."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path

import aiosqlite

from pingwatch.config import get_settings
from pingwatch.export.csv_exporter import (
    export_destinations_csv,
    export_outages_csv,
    export_traces_csv,
)
from pingwatch.export.db_snapshot import hot_db_snapshot
from pingwatch.export.json_exporter import export_all_json


def _ts_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def _sanitized_config_dict() -> dict:
    s = get_settings()
    return {
        "paths": {k: str(v) for k, v in s.paths.model_dump().items()},
        "bind": s.bind.model_dump(),
        "wifi": s.wifi.model_dump(),
        "timezone": s.timezone,
    }


def _tail_logs(logs_dir: Path, since_ms: int) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not logs_dir.exists():
        return out
    cutoff_s = since_ms / 1000.0
    for p in sorted(logs_dir.glob("*.log*")):
        try:
            if p.stat().st_mtime < cutoff_s:
                continue
            out[p.name] = p.read_bytes()
        except OSError:
            continue
    return out


async def build_export_zip(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int
) -> Path:
    """Build a ZIP containing CSV + JSON + sanitized config + last-24h logs.

    Returns the temp file path. Caller is responsible for streaming + cleanup.
    """
    settings = get_settings()
    fd, tmp_path = tempfile.mkstemp(prefix="pingwatch-export-", suffix=".zip")
    os.close(fd)
    out_path = Path(tmp_path)

    dests_csv = await export_destinations_csv(conn, since_ms, until_ms)
    outages_csv = await export_outages_csv(conn, since_ms, until_ms)
    traces_csv = await export_traces_csv(conn, since_ms, until_ms)
    all_json = await export_all_json(conn, since_ms, until_ms)

    log_cutoff_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
    logs = _tail_logs(settings.paths.logs_dir, log_cutoff_ms)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("destinations.csv", dests_csv)
        zf.writestr("outages.csv", outages_csv)
        zf.writestr("traces.csv", traces_csv)
        zf.writestr("all.json", all_json)
        zf.writestr("config.json", json.dumps(_sanitized_config_dict(), indent=2).encode("utf-8"))
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "kind": "pingwatch-export",
                    "created_ts_ms": int(time.time() * 1000),
                    "range": {"since_ms": since_ms, "until_ms": until_ms},
                    "schema_version": 1,
                },
                indent=2,
            ).encode("utf-8"),
        )
        for name, blob in logs.items():
            zf.writestr(f"logs/{name}", blob)

    return out_path


async def write_export_to_usb(
    conn: aiosqlite.Connection, since_ms: int, until_ms: int, usb_root: Path
) -> Path:
    """Build a ZIP and write it onto the USB stick at usb_root.

    Raises FileNotFoundError if usb_root is not a directory (no stick).
    """
    if not usb_root.exists() or not usb_root.is_dir():  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
        raise FileNotFoundError(f"USB mount not present: {usb_root}")

    tmp_zip = await build_export_zip(conn, since_ms, until_ms)
    try:
        target = usb_root / f"pingwatch-export-{_ts_tag()}.zip"
        with tmp_zip.open("rb") as src, target.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.sync()
        return target
    finally:
        with contextlib.suppress(OSError):
            tmp_zip.unlink()


async def build_diagnose_bundle(conn: aiosqlite.Connection) -> Path:
    """Support-bundle: full DB snapshot + 7-days logs + system info + settings + outages."""
    settings = get_settings()
    fd, tmp_path = tempfile.mkstemp(prefix="pingwatch-diagnose-", suffix=".zip")
    os.close(fd)
    out_path = Path(tmp_path)

    # Hot DB snapshot first.
    snap_fd, snap_path = tempfile.mkstemp(prefix="pw-snap-", suffix=".db")
    os.close(snap_fd)
    snap = Path(snap_path)
    try:
        await hot_db_snapshot(conn, snap)
        log_cutoff_ms = int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000
        logs = _tail_logs(settings.paths.logs_dir, log_cutoff_ms)

        # Recent outages — last 30 days.
        now_ms = int(time.time() * 1000)
        outages_csv = await export_outages_csv(conn, now_ms - 30 * 86400000, now_ms)

        # Sanitized settings table dump.
        cur = await conn.execute("SELECT key, value, value_type FROM settings ORDER BY key")
        settings_rows = await cur.fetchall()

        sys_info: dict = {}
        try:
            from pingwatch.system.pi_metrics import read_pi_metrics

            sys_info = await read_pi_metrics()
        except Exception as e:  # noqa: BLE001
            sys_info = {"error": repr(e)}

        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, arcname="pingwatch.db")
            zf.writestr("outages_30d.csv", outages_csv)
            zf.writestr(
                "settings.json",
                json.dumps(
                    [{"key": r[0], "value": r[1], "type": r[2]} for r in settings_rows],
                    indent=2,
                ).encode("utf-8"),
            )
            zf.writestr("system_info.json", json.dumps(sys_info, indent=2).encode("utf-8"))
            zf.writestr(
                "config.json", json.dumps(_sanitized_config_dict(), indent=2).encode("utf-8"),
            )
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "kind": "pingwatch-diagnose",
                        "created_ts_ms": int(time.time() * 1000),
                        "schema_version": 1,
                    },
                    indent=2,
                ).encode("utf-8"),
            )
            for name, blob in logs.items():
                zf.writestr(f"logs/{name}", blob)
        return out_path
    finally:
        with contextlib.suppress(OSError):
            snap.unlink()  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
