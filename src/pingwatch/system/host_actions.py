"""Host-side actions via a named pipe at /run/pingwatch-host.fifo.

The container talks to a tiny host-side helper (pingwatch-host-helper.service)
that listens on the FIFO and maps allowlisted commands to systemctl invocations.
On a dev box the pipe doesn't exist — we degrade gracefully and log a warning.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

FIFO_PATH = Path("/run/pingwatch-host.fifo")

# Allowlist mirrored on the host helper.
_VALID_COMMANDS = {"reboot", "factory_reset", "update_check", "restart_app"}


async def _write_command(cmd: str, payload: str = "") -> None:
    if cmd not in _VALID_COMMANDS:
        raise ValueError(f"unknown host command: {cmd}")
    if not FIFO_PATH.exists():
        log.warning("host_actions.fifo_missing", path=str(FIFO_PATH), cmd=cmd)
        return
    line = f"{cmd}\t{payload}\n".encode()

    def _write() -> None:
        # Open in non-blocking mode to avoid hanging if no reader is attached.
        fd = None
        try:
            import os

            fd = os.open(str(FIFO_PATH), os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, line)
        except OSError as e:
            log.warning("host_actions.write_failed", cmd=cmd, error=str(e))
        finally:
            if fd is not None:
                try:
                    import os as _os

                    _os.close(fd)
                except OSError:
                    pass

    await asyncio.to_thread(_write)
    log.info("host_actions.sent", cmd=cmd, payload=payload)


async def request_reboot(reason: str) -> None:
    await _write_command("reboot", reason)


async def request_factory_reset() -> None:
    await _write_command("factory_reset")


async def request_update_check() -> None:
    await _write_command("update_check")


async def request_restart_app() -> None:
    await _write_command("restart_app")
