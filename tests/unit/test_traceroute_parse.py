"""Tests for traceroute/diff.find_suspect_hop and mtr_runner.parse_mtr_json."""

from __future__ import annotations

import json

from pingwatch.models import TraceHop
from pingwatch.traceroute.diff import find_suspect_hop
from pingwatch.traceroute.mtr_runner import parse_mtr_json

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hop(
    hop_no: int,
    ip: str | None = "10.0.0.1",
    rtt_us: int | None = 5_000,
    loss_pct: float = 0.0,
) -> TraceHop:
    return TraceHop(hop_no=hop_no, host=ip, ip=ip, rtt_us=rtt_us, loss_pct=loss_pct)


def _timeout_hop(hop_no: int) -> TraceHop:
    return TraceHop(hop_no=hop_no, host=None, ip=None, rtt_us=None, loss_pct=100.0)


# ---------------------------------------------------------------------------
# find_suspect_hop
# ---------------------------------------------------------------------------


def test_empty_hops_returns_none() -> None:
    assert find_suspect_hop([]) is None


def test_all_healthy_hops_no_suspect() -> None:
    hops = [_hop(1, rtt_us=1_000), _hop(2, rtt_us=5_000), _hop(3, rtt_us=8_000)]
    assert find_suspect_hop(hops, trace_succeeded=True) is None


def test_rule_c_first_timeout_hop_is_suspect() -> None:
    """Rule C: failed trace, first timedout hop beyond hop 1 is suspect."""
    hops = [
        _hop(1, rtt_us=1_000),
        _hop(2, rtt_us=5_000),
        _timeout_hop(3),
        _timeout_hop(4),
    ]
    suspect = find_suspect_hop(hops, trace_succeeded=False)
    assert suspect == 3


def test_rule_c_ignores_hop_1_timeout() -> None:
    """Hop 1 (gateway) must never be reported as suspect under Rule C."""
    hops = [
        _timeout_hop(1),
        _timeout_hop(2),
    ]
    suspect = find_suspect_hop(hops, trace_succeeded=False)
    assert suspect == 2


def test_rule_b_first_lossy_hop_after_clean_upstream() -> None:
    """Rule B: first hop with loss > 1% where all prior hops have 0% loss."""
    hops = [
        _hop(1, loss_pct=0.0),
        _hop(2, loss_pct=0.0),
        _hop(3, loss_pct=50.0),
        _hop(4, loss_pct=50.0),
    ]
    suspect = find_suspect_hop(hops, trace_succeeded=True)
    assert suspect == 3


def test_rule_b_skipped_when_hop1_has_loss() -> None:
    """If hop 1 (gateway) already has loss, Rule B doesn't fire from hop 2."""
    hops = [
        _hop(1, loss_pct=10.0),
        _hop(2, loss_pct=10.0),
        _hop(3, loss_pct=10.0),
    ]
    # hop 2 has loss but hop 1 is not 0% loss, so Rule B condition fails on hop 2
    suspect = find_suspect_hop(hops, trace_succeeded=True)
    assert suspect is None


def test_rule_a_rtt_jump_with_history() -> None:
    """Rule A: RTT jump > 50 ms with high z-score picks the jumped hop."""
    hops = [
        _hop(1, rtt_us=1_000),
        _hop(2, rtt_us=5_000),
        _hop(3, rtt_us=120_000),  # 120 ms – big jump from 5 ms
        _hop(4, rtt_us=125_000),
    ]
    # Provide enough history for hop 3 to get a z-score
    history = {3: [7_000, 8_000, 9_000, 8_500, 7_500, 8_000, 8_200, 7_800, 8_300, 7_900]}
    suspect = find_suspect_hop(hops, hop_history_lookup=history, trace_succeeded=True)
    assert suspect == 3


def test_rule_c_beats_rule_b_when_trace_failed() -> None:
    """Tie-break: Rule C wins over Rule B when trace_succeeded=False."""
    hops = [
        _hop(1, loss_pct=0.0),
        _hop(2, loss_pct=50.0),   # Would be Rule B suspect
        _timeout_hop(3),           # Also a Rule C candidate
    ]
    suspect = find_suspect_hop(hops, trace_succeeded=False)
    # Rule C fires first; first timeout hop beyond hop 1 is hop 3
    assert suspect == 3


def test_hop_1_never_suspect_even_with_high_loss() -> None:
    """Hop 1 is excluded from suspect logic regardless of loss."""
    hops = [
        _hop(1, loss_pct=100.0),
        _hop(2, loss_pct=0.0),
    ]
    assert find_suspect_hop(hops, trace_succeeded=True) is None


# ---------------------------------------------------------------------------
# parse_mtr_json – field alias cascade
# ---------------------------------------------------------------------------


def _report(hubs: list[dict]) -> str:
    return json.dumps({"report": {"mtr": {}, "hubs": hubs}})


def test_parse_avg_field() -> None:
    """'Avg' (capital) is used for rtt_us when present."""
    raw = _report([{"count": 1, "host": "192.168.1.1", "Avg": 5.0, "Loss%": 0.0}])
    hops = parse_mtr_json(raw)
    assert len(hops) == 1
    assert hops[0].rtt_us == 5_000  # 5 ms → 5000 µs


def test_parse_lowercase_avg_field() -> None:
    """'avg' (lowercase) is accepted as fallback."""
    raw = _report([{"count": 1, "host": "192.168.1.1", "avg": 3.0, "Loss%": 0.0}])
    hops = parse_mtr_json(raw)
    assert hops[0].rtt_us == 3_000


def test_parse_last_field_fallback() -> None:
    """'Last' is used when 'Avg' and 'avg' are absent."""
    raw = _report([{"count": 1, "host": "10.0.0.1", "Last": 2.5}])
    hops = parse_mtr_json(raw)
    assert hops[0].rtt_us == 2_500


def test_parse_lowercase_last_field_fallback() -> None:
    """'last' is used as final fallback in the alias cascade."""
    raw = _report([{"count": 1, "host": "10.0.0.1", "last": 1.0}])
    hops = parse_mtr_json(raw)
    assert hops[0].rtt_us == 1_000


def test_parse_timeout_hop_ip_is_none() -> None:
    """Hubs with host='???' have ip=None."""
    raw = _report([{"count": 2, "host": "???", "Loss%": 100.0}])
    hops = parse_mtr_json(raw)
    assert len(hops) == 1
    assert hops[0].ip is None
    assert hops[0].host is None


def test_parse_loss_pct_alias() -> None:
    """Loss% and loss aliases are both accepted."""
    raw_upper = _report([{"count": 1, "host": "h", "Loss%": 50.0}])
    raw_lower = _report([{"count": 1, "host": "h", "loss": 50.0}])
    hops_u = parse_mtr_json(raw_upper)
    hops_l = parse_mtr_json(raw_lower)
    assert hops_u[0].loss_pct == 50.0
    assert hops_l[0].loss_pct == 50.0


def test_parse_invalid_json_returns_empty() -> None:
    hops = parse_mtr_json(b"not json")
    assert hops == []


def test_parse_empty_hubs_returns_empty() -> None:
    raw = json.dumps({"report": {"mtr": {}, "hubs": []}})
    hops = parse_mtr_json(raw)
    assert hops == []


def test_parse_bytes_input() -> None:
    """parse_mtr_json accepts raw bytes as well as strings."""
    raw = _report([{"count": 1, "host": "10.0.0.1", "Avg": 7.0}])
    hops = parse_mtr_json(raw.encode())
    assert len(hops) == 1
    assert hops[0].hop_no == 1


def test_parse_multiple_hops_ordered() -> None:
    raw = _report([
        {"count": 1, "host": "192.168.1.1", "Avg": 1.0, "Loss%": 0.0},
        {"count": 2, "host": "10.0.0.1", "Avg": 5.0, "Loss%": 0.0},
        {"count": 3, "host": "1.1.1.1", "Avg": 12.0, "Loss%": 0.0},
    ])
    hops = parse_mtr_json(raw)
    assert len(hops) == 3
    assert [h.hop_no for h in hops] == [1, 2, 3]
    assert hops[2].ip == "1.1.1.1"


def test_parse_best_worst_fields() -> None:
    """Best and Wrst fields populate rtt_min_us / rtt_max_us."""
    raw = _report([{
        "count": 1,
        "host": "gw",
        "Avg": 5.0,
        "Best": 3.0,
        "Wrst": 9.0,
        "Loss%": 0.0,
    }])
    hops = parse_mtr_json(raw)
    assert hops[0].rtt_min_us == 3_000
    assert hops[0].rtt_max_us == 9_000
