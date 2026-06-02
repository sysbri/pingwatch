"""Tests for probes/base.py – shared loop, _next_seq, cleanup, and runner.build_probe."""

from __future__ import annotations

import asyncio

import pytest

from pingwatch.models import (
    Destination,
    DestKind,
    PingSample,
    ProbeType,
)
from pingwatch.probes.base import Probe
from pingwatch.probes.dns_query import DnsQueryProbe, _is_ip
from pingwatch.probes.http_head import HttpHeadProbe, _normalize_url
from pingwatch.probes.icmp import IcmpProbe
from pingwatch.probes.runner import build_probe
from pingwatch.probes.tcp import TcpProbe

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dest(
    probe_type: ProbeType = ProbeType.ICMP,
    address: str = "192.168.1.1",
    interval_ms: int = 10,
) -> Destination:
    return Destination(
        id=1,
        name="test",
        address=address,
        type=probe_type,
        kind=DestKind.EXTERNAL,
        interval_ms=interval_ms,
        timeout_ms=500,
        port=None,
        enabled=True,
        ordering=0,
    )


class FakeProbe(Probe):
    """Minimal Probe subclass with a canned response and cleanup tracking."""

    def __init__(self, dest: Destination) -> None:
        super().__init__(dest)
        self.cleanup_called = False

    async def probe_once(self) -> PingSample:
        return PingSample(
            dest_id=self.dest.id,
            ts_ms=1_000_000,
            success=True,
            latency_us=1234,
        )

    async def cleanup(self) -> None:
        self.cleanup_called = True


# ---------------------------------------------------------------------------
# _next_seq
# ---------------------------------------------------------------------------


def test_next_seq_increments_sequentially() -> None:
    probe = FakeProbe(_dest())
    assert probe._next_seq() == 1
    assert probe._next_seq() == 2
    assert probe._next_seq() == 3


def test_next_seq_starts_at_zero() -> None:
    probe = FakeProbe(_dest())
    assert probe._sequence == 0
    probe._next_seq()
    assert probe._sequence == 1


# ---------------------------------------------------------------------------
# run() yields samples and cleanup() is called on close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_yields_ping_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collect 3 samples from FakeProbe.run() using a tiny interval."""
    # Patch asyncio.sleep to be a no-op so the loop runs instantly.
    monkeypatch.setattr(asyncio, "sleep", lambda _s: asyncio.coroutine(lambda: None)())

    async def fast_sleep(_s: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    dest = _dest(interval_ms=1)
    probe = FakeProbe(dest)
    samples: list[PingSample] = []
    gen = probe.run()
    for _ in range(3):
        sample = await gen.__anext__()
        samples.append(sample)
    await gen.aclose()

    assert len(samples) == 3
    for s in samples:
        assert isinstance(s, PingSample)
        assert s.success is True
        assert s.latency_us == 1234
        assert s.dest_id == 1


@pytest.mark.asyncio
async def test_run_calls_cleanup_on_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """cleanup() must be called when the generator is closed."""

    async def fast_sleep(_s: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    dest = _dest(interval_ms=1)
    probe = FakeProbe(dest)
    gen = probe.run()
    await gen.__anext__()  # consume one sample
    await gen.aclose()     # close the generator

    assert probe.cleanup_called is True


# ---------------------------------------------------------------------------
# build_probe returns the correct subclass
# ---------------------------------------------------------------------------


def test_build_probe_icmp() -> None:
    assert isinstance(build_probe(_dest(ProbeType.ICMP)), IcmpProbe)


def test_build_probe_tcp() -> None:
    dest = Destination(
        id=1,
        name="test",
        address="1.1.1.1",
        type=ProbeType.TCP,
        kind=DestKind.EXTERNAL,
        interval_ms=10,
        timeout_ms=500,
        port=443,
        enabled=True,
        ordering=0,
    )
    assert isinstance(build_probe(dest), TcpProbe)


def test_build_probe_http() -> None:
    assert isinstance(
        build_probe(_dest(ProbeType.HTTP, address="example.com")), HttpHeadProbe
    )


def test_build_probe_dns() -> None:
    assert isinstance(
        build_probe(_dest(ProbeType.DNS, address="8.8.8.8")), DnsQueryProbe
    )


# ---------------------------------------------------------------------------
# dns_query._is_ip
# ---------------------------------------------------------------------------


def test_is_ip_ipv4_true() -> None:
    assert _is_ip("192.168.1.1") is True


def test_is_ip_ipv6_true() -> None:
    assert _is_ip("::1") is True


def test_is_ip_hostname_false() -> None:
    assert _is_ip("cloudflare.com") is False


def test_is_ip_partial_false() -> None:
    assert _is_ip("192.168") is False


# ---------------------------------------------------------------------------
# http_head._normalize_url
# ---------------------------------------------------------------------------


def test_normalize_url_adds_https_when_bare() -> None:
    assert _normalize_url("example.com") == "https://example.com"


def test_normalize_url_preserves_https() -> None:
    assert _normalize_url("https://example.com") == "https://example.com"


def test_normalize_url_preserves_http() -> None:
    assert _normalize_url("http://example.com") == "http://example.com"


def test_normalize_url_with_path() -> None:
    assert _normalize_url("example.com/path") == "https://example.com/path"


# ---------------------------------------------------------------------------
# HttpHeadProbe.cleanup() closes the client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_head_cleanup_calls_aclose() -> None:
    """cleanup() must call aclose() on the internal httpx client."""
    dest = _dest(ProbeType.HTTP, address="example.com")
    probe = HttpHeadProbe(dest)

    aclose_called = False

    class FakeClient:
        async def aclose(self) -> None:
            nonlocal aclose_called
            aclose_called = True

    probe._client = FakeClient()  # type: ignore[assignment]
    await probe.cleanup()

    assert aclose_called is True
    assert probe._client is None


@pytest.mark.asyncio
async def test_http_head_cleanup_no_client_is_noop() -> None:
    """cleanup() should not raise when no client exists yet."""
    dest = _dest(ProbeType.HTTP, address="example.com")
    probe = HttpHeadProbe(dest)
    assert probe._client is None
    await probe.cleanup()  # must not raise
