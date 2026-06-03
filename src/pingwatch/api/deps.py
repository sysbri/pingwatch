"""Common FastAPI dependencies.

Resolves the shared `aiosqlite.Connection` from `app.state.db` so route
handlers can stay declarative.
"""

from __future__ import annotations

import time
from typing import Annotated

import aiosqlite
from fastapi import Depends, HTTPException, Request, status

# Superset of all range tokens used by pings.py, wifi.py and export.py.
# export.py also accepts "30d" and "all"; keep those so no route loses a
# supported range.
RANGE_TO_MS: dict[str, int] = {
    "1h": 3_600_000,
    "12h": 12 * 3_600_000,
    "24h": 86_400_000,
    "7d": 7 * 86_400_000,
    "30d": 30 * 86_400_000,
    "all": 365 * 86_400_000,
}
_DEFAULT_RANGE_MS = 86_400_000  # 24h


def range_window(range_str: str | None, default_ms: int = _DEFAULT_RANGE_MS) -> tuple[int, int]:
    """Return (since_ms, until_ms) for a range token string.

    Unknown tokens fall back to *default_ms*.  ``until_ms`` is always
    ``int(time.time() * 1000)``.
    """
    window = RANGE_TO_MS.get(range_str or "", default_ms) if range_str else default_ms
    now = int(time.time() * 1000)
    return now - window, now


async def get_conn(request: Request) -> aiosqlite.Connection:
    db = getattr(request.app.state, "db", None)
    if db is None or db._conn is None:  # noqa: SLF001 - app wiring
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database not initialised",
        )
    return db.conn


ConnDep = Annotated[aiosqlite.Connection, Depends(get_conn)]
