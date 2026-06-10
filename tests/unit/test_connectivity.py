"""Tests for captive-portal detection (HTTP-204 connectivity probe)."""
from __future__ import annotations

import pytest

from pingwatch.connectivity import (
    ConnectivityState,
    Monitor,
    classify_response,
)


def test_classify_204_is_full() -> None:
    assert classify_response(204, None, 0) == ("full", None)


def test_classify_redirect_is_portal_with_url() -> None:
    assert classify_response(302, "http://portal.example/login?x=1", 0) == (
        "portal", "http://portal.example/login?x=1",
    )


def test_classify_hijacked_200_is_portal() -> None:
    # Portal serves its own page instead of the 204.
    assert classify_response(200, None, 5123) == ("portal", None)


def test_classify_no_response_is_offline() -> None:
    assert classify_response(None, None, 0) == ("offline", None)


def test_classify_other_codes_are_portal() -> None:
    # Anything that is not the expected 204 means something intercepted us.
    assert classify_response(503, None, 0)[0] == "portal"


class _Bus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))


@pytest.mark.asyncio
async def test_monitor_publishes_only_on_state_change() -> None:
    bus = _Bus()
    mon = Monitor(bus)
    await mon.apply("portal", "http://portal.example/login", ts_ms=1000)
    await mon.apply("portal", "http://portal.example/login", ts_ms=2000)
    await mon.apply("full", None, ts_ms=3000)
    topics = [t for t, _ in bus.published]
    assert topics == ["connectivity.changed", "connectivity.changed"]
    assert bus.published[0][1]["status"] == "portal"
    assert bus.published[1][1]["status"] == "full"


@pytest.mark.asyncio
async def test_monitor_state_is_globally_readable() -> None:
    bus = _Bus()
    mon = Monitor(bus)
    await mon.apply("portal", "http://portal.example/login", ts_ms=1000)
    state = mon.state
    assert isinstance(state, ConnectivityState)
    assert state.status == "portal"
    assert state.portal_url == "http://portal.example/login"
    assert state.checked_ts_ms == 1000


@pytest.mark.asyncio
async def test_monitor_keeps_last_portal_url_while_portal(monkeypatch) -> None:
    # A later probe may get a 200-hijack without Location; keep the known URL.
    bus = _Bus()
    mon = Monitor(bus)
    await mon.apply("portal", "http://portal.example/login", ts_ms=1000)
    await mon.apply("portal", None, ts_ms=2000)
    assert mon.state.portal_url == "http://portal.example/login"
