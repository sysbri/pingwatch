"""Shared async utilities used across multiple modules."""

from __future__ import annotations

import asyncio
import contextlib


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for *seconds* unless *stop* is set first.

    Identical semantics to every ``_sleep_or_stop`` method in the codebase:
    ``asyncio.wait_for(stop.wait(), timeout=seconds)`` with TimeoutError
    swallowed (normal path — stop was not requested within the window).
    """
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
