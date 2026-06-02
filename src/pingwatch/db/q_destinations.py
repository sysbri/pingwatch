"""Destination CRUD queries."""

from __future__ import annotations

from typing import Any, cast

import aiosqlite

from ..models import Destination, DestKind, ProbeType


def _row_to_destination(row: aiosqlite.Row) -> Destination:
    return Destination(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        type=ProbeType(row["type"]),
        kind=DestKind(row["kind"]),
        interval_ms=row["interval_ms"],
        timeout_ms=row["timeout_ms"],
        port=row["port"],
        enabled=bool(row["enabled"]),
        ordering=row["ordering"],
        resolved_ip=row["resolved_ip"],
    )


# ===== Destinations =====


async def list_destinations(
    conn: aiosqlite.Connection, enabled_only: bool = True
) -> list[Destination]:
    sql = (
        "SELECT id, name, address, type, kind, interval_ms, timeout_ms, port, "
        "enabled, ordering, resolved_ip FROM destinations"
    )
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY ordering ASC, id ASC"
    cur = await conn.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_destination(r) for r in rows]


async def get_destination(conn: aiosqlite.Connection, dest_id: int) -> Destination | None:
    cur = await conn.execute(
        "SELECT id, name, address, type, kind, interval_ms, timeout_ms, port, "
        "enabled, ordering, resolved_ip FROM destinations WHERE id = ?",
        (dest_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return _row_to_destination(row) if row else None


async def insert_destination(
    conn: aiosqlite.Connection,
    *,
    name: str,
    address: str,
    type_: ProbeType | str,
    kind: DestKind | str,
    interval_ms: int = 1000,
    timeout_ms: int = 2000,
    port: int | None = None,
    ordering: int = 0,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO destinations(name, address, type, kind, interval_ms,
                                 timeout_ms, port, ordering)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, address, str(type_), str(kind), interval_ms, timeout_ms, port, ordering),
    )
    await conn.commit()
    return cast(int, cur.lastrowid)


_DEST_UPDATABLE = frozenset(
    {
        "name",
        "address",
        "type",
        "kind",
        "interval_ms",
        "timeout_ms",
        "port",
        "enabled",
        "ordering",
        "resolved_ip",
        "resolved_at_ts_ms",
    }
)


async def update_destination(conn: aiosqlite.Connection, dest_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in _DEST_UPDATABLE:
            raise ValueError(f"unknown destination field: {k}")
        cols.append(f"{k} = ?")
        if isinstance(v, bool):
            vals.append(1 if v else 0)
        elif hasattr(v, "value"):
            vals.append(v.value)
        else:
            vals.append(v)
    vals.append(dest_id)
    await conn.execute(
        f"UPDATE destinations SET {', '.join(cols)} WHERE id = ?",  # noqa: S608  # internal constant identifier, not user input
        tuple(vals),
    )
    await conn.commit()


async def delete_destination(conn: aiosqlite.Connection, dest_id: int) -> None:
    await conn.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
    await conn.commit()


async def reorder_destinations(conn: aiosqlite.Connection, id_order: list[int]) -> None:
    # Two-phase write to dodge UNIQUE(ordering) collisions.
    offset = 1_000_000
    await conn.execute("BEGIN")
    try:
        for idx, dest_id in enumerate(id_order):
            await conn.execute(
                "UPDATE destinations SET ordering = ? WHERE id = ?",
                (offset + idx, dest_id),
            )
        for idx, dest_id in enumerate(id_order):
            await conn.execute(
                "UPDATE destinations SET ordering = ? WHERE id = ?",
                (idx, dest_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
