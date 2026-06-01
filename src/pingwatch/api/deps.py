"""Common FastAPI dependencies.

Resolves the shared `aiosqlite.Connection` from `app.state.db` so route
handlers can stay declarative.
"""

from __future__ import annotations

from typing import Annotated

import aiosqlite
from fastapi import Depends, HTTPException, Request, status


async def get_conn(request: Request) -> aiosqlite.Connection:
    db = getattr(request.app.state, "db", None)
    if db is None or db._conn is None:  # noqa: SLF001 - app wiring
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database not initialised",
        )
    return db.conn


ConnDep = Annotated[aiosqlite.Connection, Depends(get_conn)]
