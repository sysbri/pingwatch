"""Shared math helpers for metrics modules."""

from __future__ import annotations


def percentile(sorted_vals: list[int], pct: float) -> int | None:
    """Return the *pct* percentile (0.0–1.0) of a pre-sorted list of ints.

    Returns ``None`` for an empty list; returns the single element for a
    one-element list.  Uses linear interpolation between adjacent ranks.
    """
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return int(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def hour_bucket_ms(ts_ms: int) -> int:
    """Return the start-of-hour timestamp (ms) for *ts_ms*."""
    return (ts_ms // 3_600_000) * 3_600_000
