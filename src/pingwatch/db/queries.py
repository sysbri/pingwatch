"""Re-export facade for the db query sub-modules.

All public names from the domain modules are re-exported here so that both
``from pingwatch.db import queries as q; q.foo(...)`` and
``from pingwatch.db.queries import foo`` continue to work unchanged.

The HTTP-layer helpers in ``q_compat`` are imported last so that names shared
with the domain modules (``list_destinations``, ``get_destination``,
``list_outages``, ``get_outage``, ``list_raw_pings``, ``update_destination``,
``delete_destination``) resolve to the richer dict/tuple-returning compat
versions that the API routes expect.  Internal callers that need the
domain-typed ``Destination`` objects should import directly from
``pingwatch.db.q_destinations``.
"""

from __future__ import annotations  # noqa: I001

from .q_aggregates import (
    _DAILY_COLS,
    _HOURLY_COLS,
    latest_finalized_hour,
    list_daily_aggregates,
    list_hourly_aggregates,
    upsert_daily_aggregate,
    upsert_hourly_aggregate,
)
from .q_destinations import (
    _DEST_UPDATABLE,
    _row_to_destination,
    insert_destination,
)
from .q_outages import (
    add_outage_member,
    close_outage,
    count_outages,
    get_outage_members,
    open_outage,
    open_outages,
    total_outage_ms,
    update_outage_type,
)
from .q_pings import (
    _PingStatus,
    count_raw_pings,
    insert_raw_pings,
    insert_raw_pings_with_flags,
    latest_pings,
    sparkline_pings,
)
from .q_settings import (
    _cast_setting,
    _infer_value_type,
    _now_ms,
    get_setting,
    get_setting_typed,
    list_settings,
    set_setting,
)
from .q_speedtest import (
    _SPEEDTEST_COLS,
    cleanup_running_speedtests,
    insert_speedtest_start,
    speedtest_by_task,
    speedtest_history,
    speedtest_last,
    update_speedtest_done,
    update_speedtest_failed,
)
from .q_stream import (
    bytes_today,
    insert_heartbeat_event,
    insert_notification,
    insert_stream_event,
    insert_throughput_sample,
    list_stream_events,
    list_unseen_notifications,
    mark_notifications_seen,
    throughput_series,
)
from .q_traces import (
    get_trace_hops,
    get_traceroute,
    hop_history,
    insert_traceroute,
    list_traceroutes,
    upsert_route_signature,
)
from .q_wifi import (
    insert_rssi_sample,
    insert_source_switch,
    insert_wifi_event,
    latest_rssi,
    list_wifi_events,
    rssi_series,
    source_switches,
)

# HTTP-layer compat: imported last so their names shadow the domain versions
# where signatures differ (e.g. list_destinations returns list[dict] here).
from .q_compat import (
    _coerce,
    _detect_type,
    _stringify,
    create_destination,
    db_size_bytes,
    delete_destination,
    dest_kpis,
    factory_reset,
    get_all_settings,
    get_destination,
    get_outage,
    get_trace,
    hero_stats,
    hourly_aggregates_for,
    latency_sparkline,
    latest_traces_per_dest,
    list_destinations,
    list_outages,
    list_raw_pings,
    list_traces,
    outages_today_for_dest,
    purge_all_metrics,
    recent_events,
    reorder_destinations,
    reset_destination_data,
    set_settings,
    stream_summary,
    update_destination,
    wifi_current,
)

__all__ = [
    # destinations (compat / dict-returning versions)
    "_DEST_UPDATABLE",
    "_row_to_destination",
    "create_destination",
    "delete_destination",
    "get_destination",
    "insert_destination",
    "list_destinations",
    "reorder_destinations",
    "reset_destination_data",
    "update_destination",
    # settings
    "_cast_setting",
    "_coerce",
    "_detect_type",
    "_infer_value_type",
    "_now_ms",
    "_stringify",
    "get_all_settings",
    "get_setting",
    "get_setting_typed",
    "list_settings",
    "set_setting",
    "set_settings",
    # pings
    "_PingStatus",
    "count_raw_pings",
    "insert_raw_pings",
    "insert_raw_pings_with_flags",
    "latest_pings",
    "list_raw_pings",
    "sparkline_pings",
    # aggregates
    "_DAILY_COLS",
    "_HOURLY_COLS",
    "hourly_aggregates_for",
    "list_daily_aggregates",
    "list_hourly_aggregates",
    "latest_finalized_hour",
    "upsert_daily_aggregate",
    "upsert_hourly_aggregate",
    # outages
    "add_outage_member",
    "close_outage",
    "count_outages",
    "get_outage",
    "get_outage_members",
    "list_outages",
    "open_outage",
    "open_outages",
    "outages_today_for_dest",
    "total_outage_ms",
    "update_outage_type",
    # traces
    "get_trace",
    "get_trace_hops",
    "get_traceroute",
    "hop_history",
    "insert_traceroute",
    "latest_traces_per_dest",
    "list_traces",
    "list_traceroutes",
    "upsert_route_signature",
    # wifi
    "insert_rssi_sample",
    "insert_source_switch",
    "insert_wifi_event",
    "latest_rssi",
    "list_wifi_events",
    "rssi_series",
    "source_switches",
    "wifi_current",
    # stream + heartbeat + notifications
    "bytes_today",
    "insert_heartbeat_event",
    "insert_notification",
    "insert_stream_event",
    "insert_throughput_sample",
    "list_stream_events",
    "list_unseen_notifications",
    "mark_notifications_seen",
    "throughput_series",
    # speedtest
    "_SPEEDTEST_COLS",
    "cleanup_running_speedtests",
    "insert_speedtest_start",
    "speedtest_by_task",
    "speedtest_history",
    "speedtest_last",
    "update_speedtest_done",
    "update_speedtest_failed",
    # dashboard
    "db_size_bytes",
    "dest_kpis",
    "hero_stats",
    "latency_sparkline",
    "purge_all_metrics",
    "factory_reset",
    "recent_events",
    "stream_summary",
]
