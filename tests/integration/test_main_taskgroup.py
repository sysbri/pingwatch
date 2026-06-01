"""main.serve() boots, then shuts down cleanly on signal within 5 s."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile

import pytest


@pytest.mark.asyncio
async def test_main_graceful_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Point env at a fresh temp DB so the boot doesn't collide with /data.
    tmpdir = tempfile.mkdtemp(prefix="pw-main-")
    monkeypatch.setenv("PINGWATCH_DB", f"{tmpdir}/pingwatch.db")
    monkeypatch.setenv("PINGWATCH_CONFIG", f"{tmpdir}/config.yaml")
    monkeypatch.setenv("PINGWATCH_BIND_PORT", "0")  # OS-assigned

    # Reload settings to pick up env.
    from pingwatch import config as config_mod

    config_mod.get_settings.cache_clear()

    from pingwatch.main import serve

    serve_task = asyncio.create_task(serve())
    # Allow boot.
    await asyncio.sleep(0.5)

    # Signal SIGTERM to the running event loop.
    os.kill(os.getpid(), signal.SIGTERM)

    try:
        await asyncio.wait_for(serve_task, timeout=5.0)
    except TimeoutError:
        serve_task.cancel()
        pytest.fail("serve() did not shut down within 5s")
