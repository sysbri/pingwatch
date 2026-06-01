"""Tests for the route-signature hash and change detection."""

from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.models import TraceHop
from pingwatch.traceroute.diff import detect_route_change, signature_hash, signature_text


def _hop(hop_no: int, ip: str | None) -> TraceHop:
    return TraceHop(hop_no=hop_no, host=ip, ip=ip, rtt_us=1000, loss_pct=0.0)


def test_signature_text_includes_star_for_missing_ip() -> None:
    hops = [_hop(1, "192.168.1.1"), _hop(2, None), _hop(3, "1.1.1.1")]
    text = signature_text(hops)
    assert text == "1:192.168.1.1|2:*|3:1.1.1.1"


def test_signature_hash_is_stable() -> None:
    hops_a = [_hop(1, "192.168.1.1"), _hop(2, "10.0.0.1"), _hop(3, "1.1.1.1")]
    hops_b = [_hop(1, "192.168.1.1"), _hop(2, "10.0.0.1"), _hop(3, "1.1.1.1")]
    h_a, _ = signature_hash(hops_a)
    h_b, _ = signature_hash(hops_b)
    assert h_a == h_b


def test_signature_hash_differs_on_ip_change() -> None:
    hops_a = [_hop(1, "192.168.1.1"), _hop(2, "10.0.0.1"), _hop(3, "1.1.1.1")]
    hops_b = [_hop(1, "192.168.1.1"), _hop(2, "10.0.0.2"), _hop(3, "1.1.1.1")]
    assert signature_hash(hops_a)[0] != signature_hash(hops_b)[0]


def test_signature_hash_differs_on_hop_count() -> None:
    hops_a = [_hop(1, "192.168.1.1"), _hop(2, "1.1.1.1")]
    hops_b = [_hop(1, "192.168.1.1"), _hop(2, "10.0.0.1"), _hop(3, "1.1.1.1")]
    assert signature_hash(hops_a)[0] != signature_hash(hops_b)[0]


@pytest.mark.asyncio
async def test_detect_route_change_first_trace_returns_false(db: aiosqlite.Connection) -> None:
    changed = await detect_route_change(db, dest_id=2, current_hash="abc123")
    assert changed is False


@pytest.mark.asyncio
async def test_detect_route_change_same_hash_returns_false(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO traceroutes(dest_id, ts_ms, trigger, success, hop_count, "
        "signature_hash, route_changed) VALUES (2, 100, 'scheduled', 1, 3, 'abc', 0)"
    )
    await db.commit()
    assert await detect_route_change(db, dest_id=2, current_hash="abc") is False


@pytest.mark.asyncio
async def test_detect_route_change_new_hash_returns_true(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO traceroutes(dest_id, ts_ms, trigger, success, hop_count, "
        "signature_hash, route_changed) VALUES (2, 100, 'scheduled', 1, 3, 'abc', 0)"
    )
    await db.commit()
    assert await detect_route_change(db, dest_id=2, current_hash="def") is True
