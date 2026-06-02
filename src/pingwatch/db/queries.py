"""Re-export facade for the db query sub-modules.

All public names from the domain modules are re-exported here so that both
``from pingwatch.db import queries as q; q.foo(...)`` and
``from pingwatch.db.queries import foo`` continue to work unchanged.
"""

from __future__ import annotations

# Expose the private _now_ms helper that existing code in other modules may
# rely on via ``from pingwatch.db.queries import _now_ms`` (unlikely but
# kept for safety).
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
    delete_destination,
    get_destination,
    insert_destination,
    list_destinations,
    reorder_destinations,
    update_destination,
)
from .q_outages import (
    add_outage_member,
    close_outage,
    count_outages,
    get_outage,
    get_outage_members,
    list_outages,
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
    list_raw_pings,
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
    insert_wifi_event,
    latest_rssi,
    list_wifi_events,
    rssi_series,
)

__all__ = [
    # destinations
    "_DEST_UPDATABLE",
    "_row_to_destination",
    "delete_destination",
    "get_destination",
    "insert_destination",
    "list_destinations",
    "reorder_destinations",
    "update_destination",
    # settings
    "_cast_setting",
    "_infer_value_type",
    "_now_ms",
    "get_setting",
    "get_setting_typed",
    "list_settings",
    "set_setting",
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
    "total_outage_ms",
    "update_outage_type",
    # traces
    "get_trace_hops",
    "get_traceroute",
    "hop_history",
    "insert_traceroute",
    "list_traceroutes",
    "upsert_route_signature",
    # wifi
    "insert_rssi_sample",
    "insert_wifi_event",
    "latest_rssi",
    "list_wifi_events",
    "rssi_series",
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
]
