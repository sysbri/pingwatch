"""Settings queries."""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar, cast

import aiosqlite

T = TypeVar("T")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ===== Settings =====


async def get_setting(conn: aiosqlite.Connection, key: str) -> str | None:
    cur = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    return row["value"] if row else None


def _cast_setting(value: str, value_type: str) -> Any:
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        return value.lower() in ("1", "true", "yes", "on")
    if value_type == "json":
        return json.loads(value)
    return value


async def get_setting_typed(conn: aiosqlite.Connection, key: str, default: T) -> T:  # noqa: UP047
    cur = await conn.execute("SELECT value, value_type FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return default
    try:
        return cast(T, _cast_setting(row["value"], row["value_type"]))
    except (ValueError, json.JSONDecodeError):
        return default


def _infer_value_type(value: Any) -> tuple[str, str]:
    if isinstance(value, bool):
        return ("true" if value else "false"), "bool"
    if isinstance(value, int):
        return str(value), "int"
    if isinstance(value, float):
        return repr(value), "float"
    if isinstance(value, (dict, list)):
        return json.dumps(value), "json"
    return str(value), "string"


async def set_setting(conn: aiosqlite.Connection, key: str, value: Any) -> None:
    str_val, vtype = _infer_value_type(value)
    # Preserve declared value_type if a row already exists.
    cur = await conn.execute("SELECT value_type FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    await cur.close()
    if row:
        vtype = row["value_type"]
        if vtype == "bool" and isinstance(value, bool):
            str_val = "true" if value else "false"
        elif vtype == "json":
            str_val = json.dumps(value) if not isinstance(value, str) else value
        else:
            str_val = str(value)
    await conn.execute(
        """
        INSERT INTO settings(key, value, value_type, updated_at_ts_ms)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at_ts_ms = excluded.updated_at_ts_ms
        """,
        (key, str_val, vtype, _now_ms()),
    )
    await conn.commit()


async def list_settings(conn: aiosqlite.Connection) -> dict[str, str]:
    cur = await conn.execute("SELECT key, value FROM settings ORDER BY key")
    rows = await cur.fetchall()
    await cur.close()
    return {r["key"]: r["value"] for r in rows}
