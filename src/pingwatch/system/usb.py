"""USB stick detection + safe writes.

The mount strategy is host-driven (udev rules → /media/pingwatch-usb). This
module surfaces the mount to the app and the UI.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


MARKER_NAME = ".pingwatch-mounted"


async def list_usb_mounts(root: Path = Path("/media")) -> list[Path]:
    """Return subdirs of `root` that look like a mounted USB stick.

    A directory counts as a mount if it contains the udev-rule marker OR is a
    non-empty directory (best-effort for dev boxes without the udev rule).
    """
    mounts: list[Path] = []
    if not root.exists():  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
        return mounts
    try:
        for entry in root.iterdir():  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
            if not entry.is_dir():
                continue
            marker = entry / MARKER_NAME
            if marker.exists():
                mounts.append(entry)
                continue
            try:
                # Treat any non-empty dir as a candidate; cheap iter() check.
                next(iter(entry.iterdir()))
                mounts.append(entry)
            except (StopIteration, PermissionError, OSError):
                continue
    except OSError as e:
        log.warning("usb.list.error", error=str(e))
    return mounts


async def selected_usb_mount(root: Path = Path("/media")) -> Path | None:
    mounts = await list_usb_mounts(root)
    return mounts[0] if mounts else None


async def watch_usb_mounts(root: Path) -> AsyncIterator[Path]:
    """Poll every 3 s; yield newly-appeared mounts.

    Pure stdlib — no inotify dependency. The 3 s tick is plenty for UX given
    USB mount-settle time is usually 1–5 s.
    """
    seen: set[Path] = set()
    while True:
        try:
            current = set(await list_usb_mounts(root))
            for path in current - seen:
                yield path
            seen = current
        except Exception as e:  # noqa: BLE001
            log.warning("usb.watch.error", error=repr(e))
        await asyncio.sleep(3.0)


async def safe_write_zip(src: Path, dst_usb: Path) -> Path:
    """Copy `src` to `dst_usb`, fsync, return final path."""
    dst = dst_usb / src.name if dst_usb.is_dir() else dst_usb  # noqa: ASYNC240  # startup/rare path, blocking is acceptable
    with src.open("rb") as s, dst.open("wb") as d:
        while True:
            chunk = s.read(1024 * 1024)
            if not chunk:
                break
            d.write(chunk)
        d.flush()
        os.fsync(d.fileno())
    os.sync()
    return dst
