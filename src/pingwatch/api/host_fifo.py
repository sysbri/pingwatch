"""Async writer for the host-helper command FIFO (/run/pingwatch-host.fifo).

Opening a FIFO for writing blocks until a reader is present, so it must never
run on the event loop directly — always off-loop via ``asyncio.to_thread`` with
a timeout. Both the system and wifi routes use this single implementation.
"""

from __future__ import annotations

import asyncio
import os

FIFO_PATH = "/run/pingwatch-host.fifo"
_DEFAULT_TIMEOUT_S = 3.0


def _write_sync(line: str) -> None:
    # O_WRONLY blocks until the helper opens the read end; the to_thread
    # timeout in write_command bounds that wait.
    fd = os.open(FIFO_PATH, os.O_WRONLY)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


async def write_command(line: str, timeout: float = _DEFAULT_TIMEOUT_S) -> None:  # noqa: ASYNC109  # explicit timeout is intentional
    """Write one command line to the host FIFO without blocking the event loop.

    Raises ``FileNotFoundError`` (no FIFO), ``PermissionError`` (not writable),
    ``TimeoutError`` (no reader within ``timeout``), or ``OSError`` otherwise.
    """
    if not line.endswith("\n"):
        line += "\n"
    await asyncio.wait_for(asyncio.to_thread(_write_sync, line), timeout=timeout)
