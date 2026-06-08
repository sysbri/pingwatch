"""run_wifi_monitor builds a WifiConfig from settings (interface wiring)."""
from __future__ import annotations

import pytest

import pingwatch.wifi.monitor as monitor_mod


@pytest.mark.asyncio
async def test_run_wifi_monitor_passes_interface_from_settings(monkeypatch) -> None:
    captured = {}

    class _FakeMonitor:
        def __init__(self, conn, bus, config=None):
            captured["interface"] = config.interface if config else None

        async def run(self):
            return None

    monkeypatch.setattr(monitor_mod, "WifiMonitor", _FakeMonitor)
    monkeypatch.setenv("PINGWATCH_WLAN_IF", "wlan9")
    # get_settings is lru_cache'd; clear so the env takes effect
    from pingwatch.config import get_settings
    get_settings.cache_clear()

    await monitor_mod.run_wifi_monitor(conn=object(), bus=object())
    assert captured["interface"] == "wlan9"
