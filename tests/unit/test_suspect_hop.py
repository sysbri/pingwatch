"""Golden-trace tests for suspect-hop identification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pingwatch.traceroute.diff import find_suspect_hop
from pingwatch.traceroute.mtr_runner import parse_mtr_json

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mtr_outputs"


def _load(name: str) -> list:
    raw = (FIXTURES / name).read_text()
    json.loads(raw)  # validate
    return parse_mtr_json(raw)


def test_clean_trace_no_suspect() -> None:
    hops = _load("clean.json")
    assert find_suspect_hop(hops, trace_succeeded=True) is None


def test_rule_a_rtt_jump() -> None:
    hops = _load("rule_a_jump.json")
    # 10 normal samples (~8 ms ± 1 ms jitter) so the new 120 ms is a huge outlier.
    history = {3: [7_000, 8_000, 9_000, 8_500, 7_500, 8_000, 8_200, 7_800, 8_300, 7_900]}
    suspect = find_suspect_hop(hops, hop_history_lookup=history, trace_succeeded=True)
    assert suspect == 3


def test_rule_b_loss_after_clean_upstream() -> None:
    hops = _load("rule_b_loss.json")
    suspect = find_suspect_hop(hops, trace_succeeded=True)
    # Rule B fires on first hop with loss > 1% while all upstream are 0%.
    assert suspect == 3


def test_rule_c_timeout() -> None:
    hops = _load("rule_c_timeout.json")
    suspect = find_suspect_hop(hops, trace_succeeded=False)
    # Rule C: smallest hop_no that timed out (hop 3, since hop 1 is excluded)
    assert suspect == 3


def test_first_hop_never_suspect() -> None:
    hops = _load("clean.json")
    # Force loss on hop 1 (gateway). Rule should NOT mark it.
    from pingwatch.models import TraceHop

    altered = [
        TraceHop(hop_no=1, host="192.168.1.1", ip="192.168.1.1", rtt_us=1200, loss_pct=10.0),
        *hops[1:],
    ]
    assert find_suspect_hop(altered, trace_succeeded=True) is None


def test_tie_break_c_over_b() -> None:
    # When trace failed AND there is also Rule-B-style loss earlier, C wins.
    hops = _load("rule_c_timeout.json")
    # Inject upstream loss into hop 2 (but rule C still wins because trace failed)
    suspect = find_suspect_hop(hops, trace_succeeded=False)
    assert suspect == 3
