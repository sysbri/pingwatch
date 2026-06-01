"""Shared dataclasses & enums used across modules.

Wire format on the bus and persistence schema mirror these where possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ProbeType(StrEnum):
    ICMP = "ICMP"
    TCP = "TCP"
    HTTP = "HTTP"
    DNS = "DNS"


class DestKind(StrEnum):
    GATEWAY = "gateway"
    EXTERNAL = "external"


class OutageType(StrEnum):
    EINZEL = "EINZEL"
    MULTI = "MULTI"
    UPLINK = "UPLINK"
    WLAN = "WLAN"
    STREAM = "STREAM"


class TraceTrigger(StrEnum):
    SCHEDULED = "scheduled"
    OUTAGE = "outage"
    MANUAL = "manual"


class WifiEventType(StrEnum):
    DISCONNECT = "disconnect"
    CONNECT = "connect"
    REASSOC = "reassoc"
    CHANNEL_CHANGE = "channel_change"
    SSID_CHANGE = "ssid_change"
    ROAM = "roam"


class StreamEventType(StrEnum):
    CONNECT = "connect"
    RECONNECT = "reconnect"
    DROP = "drop"
    THROUGHPUT_LOW = "throughput_low"
    THROUGHPUT_RECOVER = "throughput_recover"


class HeartbeatEventType(StrEnum):
    LOSS = "loss"
    RECOVER = "recover"
    DRIFT = "drift"


# Bit flags for raw_pings.flags
FLAG_SPIKE = 1
FLAG_OUTAGE_FIRST = 2
FLAG_OUTAGE_LAST = 4
FLAG_DURING_OUTAGE = 8


@dataclass(slots=True, frozen=True)
class Destination:
    id: int
    name: str
    address: str
    type: ProbeType
    kind: DestKind
    interval_ms: int
    timeout_ms: int
    port: int | None
    enabled: bool
    ordering: int
    resolved_ip: str | None = None


@dataclass(slots=True, frozen=True)
class PingSample:
    """Emitted by a probe per attempt."""
    dest_id: int
    ts_ms: int
    success: bool
    latency_us: int | None = None
    ttl: int | None = None
    sequence: int | None = None
    error_kind: str | None = None


@dataclass(slots=True, frozen=True)
class OutageOpened:
    outage_id: int
    dest_id: int
    start_ts_ms: int


@dataclass(slots=True, frozen=True)
class OutageClosed:
    outage_id: int
    dest_id: int
    start_ts_ms: int
    end_ts_ms: int
    duration_ms: int
    outage_type: OutageType


@dataclass(slots=True, frozen=True)
class WifiSnapshot:
    """1 Hz sample."""
    ts_ms: int
    ssid: str | None
    bssid: str | None
    rssi: int | None
    channel: int | None
    link_rate_kbps: int | None
    associated: bool


@dataclass(slots=True, frozen=True)
class WifiEvent:
    ts_ms: int
    event_type: WifiEventType
    ssid: str | None = None
    bssid: str | None = None
    rssi: int | None = None
    channel: int | None = None
    duration_ms: int | None = None
    raw: str | None = None


@dataclass(slots=True, frozen=True)
class StreamSample:
    ts_ms: int
    kbps: int
    tcp_rtt_us: int | None
    bytes_total: int


@dataclass(slots=True, frozen=True)
class StreamEvent:
    ts_ms: int
    event_type: StreamEventType
    duration_ms: int | None = None
    kbps_before: int | None = None
    kbps_after: int | None = None
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class HeartbeatEvent:
    ts_ms: int
    event_type: HeartbeatEventType
    duration_ms: int | None = None
    missed_count: int | None = None


@dataclass(slots=True, frozen=True)
class TraceHop:
    hop_no: int
    host: str | None
    ip: str | None
    rtt_us: int | None
    rtt_min_us: int | None = None
    rtt_max_us: int | None = None
    loss_pct: float = 0.0


@dataclass(slots=True, frozen=True)
class TraceSnapshot:
    dest_id: int
    ts_ms: int
    trigger: TraceTrigger
    success: bool
    hops: list[TraceHop] = field(default_factory=list)
