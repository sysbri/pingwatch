"""Route signature, change detection and suspect-hop heuristics."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence

from pingwatch.models import TraceHop

ZSCORE_THRESHOLD = 2.0
RTT_JUMP_US = 50_000
LOSS_THRESHOLD_PCT = 1.0


def signature_text(hops: Sequence[TraceHop]) -> str:
    return "|".join(f"{h.hop_no}:{h.ip or '*'}" for h in hops)


def signature_hash(hops: Sequence[TraceHop]) -> tuple[str, str]:
    text = signature_text(hops)
    digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return digest, text


async def detect_route_change(conn: object, dest_id: int, current_hash: str) -> bool:
    cur = await conn.execute(  # type: ignore[attr-defined]
        "SELECT signature_hash FROM traceroutes WHERE dest_id = ? "
        "ORDER BY ts_ms DESC LIMIT 1",
        (dest_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return False
    return str(row[0]) != current_hash


def find_suspect_hop(
    hops: Sequence[TraceHop],
    hop_history_lookup: Mapping[int, Sequence[int]] | None = None,
    *,
    trace_succeeded: bool = True,
) -> int | None:
    """Implement the suspect-hop rules described in the plan.

    Tie-break: C > B > A. For multiple A candidates: highest z-score, then
    smallest hop_no. The first hop (gateway) is never suspect.
    """
    hop_history_lookup = hop_history_lookup or {}
    if not hops:
        return None

    candidates_c: list[int] = []
    if not trace_succeeded:
        for h in hops:
            if h.ip is None and h.hop_no > 1:
                candidates_c.append(h.hop_no)
        if candidates_c:
            return min(candidates_c)

    candidates_b: list[int] = []
    for idx, h in enumerate(hops):
        if h.hop_no <= 1:
            continue
        if h.loss_pct > LOSS_THRESHOLD_PCT and all(
            prev.loss_pct == 0.0 for prev in hops[:idx]
        ):
            candidates_b.append(h.hop_no)
    if candidates_b:
        return min(candidates_b)

    candidates_a: list[tuple[float, int]] = []
    prev_rtt: int | None = None
    for h in hops:
        rtt = h.rtt_us
        if h.hop_no <= 1:
            if rtt is not None:
                prev_rtt = rtt
            continue
        if rtt is None or prev_rtt is None:
            if rtt is not None:
                prev_rtt = rtt
            continue
        delta = rtt - prev_rtt
        if delta > RTT_JUMP_US:
            history = hop_history_lookup.get(h.hop_no) or ()
            z = _zscore(rtt, history)
            if z is not None and z > ZSCORE_THRESHOLD:
                candidates_a.append((z, h.hop_no))
        prev_rtt = rtt

    if candidates_a:
        candidates_a.sort(key=lambda x: (-x[0], x[1]))
        return candidates_a[0][1]
    return None


def _zscore(value: int, history: Sequence[int]) -> float | None:
    if len(history) < 5:
        return None
    mean = sum(history) / len(history)
    var = sum((h - mean) ** 2 for h in history) / len(history)
    if var <= 0:
        return None
    return (value - mean) / math.sqrt(var)
