"""Pydantic request/response schemas for the HTTP API.

Internal "model.py" dataclasses describe wire formats on the bus and DB rows;
these Pydantic models describe the JSON surface of the HTTP API. Keeping them
separate prevents Pydantic decorators from leaking into low-level dataclasses.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class APIError(BaseModel):
    detail: str


# ---------- Targets ----------

ProbeTypeLiteral = Literal["ICMP", "TCP", "HTTP", "DNS"]
DestKindLiteral = Literal["gateway", "external"]


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=1, max_length=255)
    type: ProbeTypeLiteral = "ICMP"
    kind: DestKindLiteral = "external"
    interval_ms: int = Field(default=1000, ge=200, le=60000)
    timeout_ms: int = Field(default=2000, ge=200, le=60000)
    port: int | None = Field(default=None, ge=1, le=65535)
    enabled: bool = True
    ordering: int = Field(default=0, ge=0, le=999)


class TargetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    address: str | None = None
    type: ProbeTypeLiteral | None = None
    kind: DestKindLiteral | None = None
    interval_ms: int | None = Field(default=None, ge=200, le=60000)
    timeout_ms: int | None = Field(default=None, ge=200, le=60000)
    port: int | None = Field(default=None, ge=1, le=65535)
    enabled: bool | None = None
    ordering: int | None = Field(default=None, ge=0, le=999)


class TargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    address: str
    type: ProbeTypeLiteral
    kind: DestKindLiteral
    interval_ms: int
    timeout_ms: int
    port: int | None
    enabled: bool
    ordering: int
    resolved_ip: str | None = None


class ReorderIn(BaseModel):
    order: list[int]


class TestResult(BaseModel):
    success: bool
    latency_us: int | None
    error_kind: str | None = None
    ts_ms: int


# ---------- Settings ----------

class SettingsPatch(BaseModel):
    """Free-form key/value patch."""

    model_config = ConfigDict(extra="allow")


# ---------- System ----------

class SystemMetrics(BaseModel):
    cpu_pct: float
    ram_used_mb: int
    ram_total_mb: int
    temp_c: float | None
    sd_used_gb: float
    sd_total_gb: float
    db_size_mb: float
    uptime_seconds: int
    version: str
    wifi: dict[str, Any]


# ---------- Generic ----------

class OkResponse(BaseModel):
    ok: bool = True
    detail: str | None = None
