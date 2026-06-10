"""Tests for the open-portal endpoint (kiosk captive-portal browser)."""
from __future__ import annotations

import pytest

from pingwatch import connectivity
from pingwatch.api.routes import wifi as wifi_routes
from pingwatch.connectivity import ConnectivityState


@pytest.fixture
def fifo_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def fake_write(line: str) -> None:
        calls.append(line)

    monkeypatch.setattr(wifi_routes.host_fifo, "write_command", fake_write)
    return calls


@pytest.mark.asyncio
async def test_open_portal_uses_detected_url(monkeypatch, fifo_calls) -> None:
    monkeypatch.setattr(
        connectivity, "get_state",
        lambda: ConnectivityState("portal", "http://portal.example/login", 1),
    )
    res = await wifi_routes.open_portal()
    assert res.ok is True
    assert fifo_calls == ["open_portal\thttp://portal.example/login"]


@pytest.mark.asyncio
async def test_open_portal_falls_back_without_detected_url(monkeypatch, fifo_calls) -> None:
    monkeypatch.setattr(
        connectivity, "get_state", lambda: ConnectivityState("full", None, 1),
    )
    res = await wifi_routes.open_portal()
    assert res.ok is True
    # Plain-HTTP probe page so the portal can hijack it.
    assert fifo_calls == ["open_portal\thttp://neverssl.com"]


@pytest.mark.asyncio
async def test_close_portal_writes_close_command(fifo_calls) -> None:
    res = await wifi_routes.close_portal()
    assert res.ok is True
    assert fifo_calls == ["close_portal"]


@pytest.mark.asyncio
async def test_open_portal_reports_fifo_unavailable(monkeypatch) -> None:
    async def fake_write(line: str) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(wifi_routes.host_fifo, "write_command", fake_write)
    monkeypatch.setattr(
        connectivity, "get_state", lambda: ConnectivityState("full", None, 1),
    )
    res = await wifi_routes.open_portal()
    assert res.ok is False
