from __future__ import annotations

import time

import pytest

from pingwatch.db import queries as q
from pingwatch.db.connection import open_db
from pingwatch.models import (
    DestKind,
    HeartbeatEvent,
    HeartbeatEventType,
    OutageType,
    PingSample,
    ProbeType,
    StreamEvent,
    StreamEventType,
    TraceHop,
    WifiEvent,
    WifiEventType,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest.mark.asyncio
async def test_seeded_destinations_and_settings(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        dests = await q.list_destinations(conn)
        assert len(dests) == 4
        names = [d.name for d in dests]
        assert "Gateway" in names
        assert "Cloudflare" in names

        # default settings present and typed
        n = await q.get_setting_typed(conn, "outage.N_consec_loss", 0)
        assert n == 2
        mult = await q.get_setting_typed(conn, "spike.p50_multiplier", 0.0)
        assert mult == 3.0
        endpoint = await q.get_setting_typed(conn, "stream.endpoint", "")
        assert endpoint.startswith("https://")


@pytest.mark.asyncio
async def test_settings_set_and_get(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        await q.set_setting(conn, "spike.absolute_threshold_us", 100_000)
        v = await q.get_setting_typed(conn, "spike.absolute_threshold_us", 0)
        assert v == 100_000

        await q.set_setting(conn, "spike.p50_multiplier", 4.5)
        m = await q.get_setting_typed(conn, "spike.p50_multiplier", 0.0)
        assert m == 4.5


@pytest.mark.asyncio
async def test_destination_crud(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        dest_id = await q.insert_destination(
            conn,
            name="My Test",
            address="example.com",
            type_=ProbeType.HTTP,
            kind=DestKind.EXTERNAL,
            interval_ms=2000,
            timeout_ms=1500,
            port=443,
            ordering=99,
        )
        assert dest_id > 0
        d = await q.get_destination(conn, dest_id)
        assert d is not None
        assert d.name == "My Test"
        assert d.type == ProbeType.HTTP

        await q.update_destination(conn, dest_id, name="My Test 2", enabled=False)
        d2 = await q.get_destination(conn, dest_id)
        assert d2 is not None
        assert d2.name == "My Test 2"
        assert d2.enabled is False

        await q.delete_destination(conn, dest_id)
        assert await q.get_destination(conn, dest_id) is None


@pytest.mark.asyncio
async def test_reorder_destinations(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        dests = await q.list_destinations(conn)
        ids = [d.id for d in dests]
        reversed_ids = list(reversed(ids))
        await q.reorder_destinations(conn, reversed_ids)
        d2 = await q.list_destinations(conn)
        assert [d.id for d in d2] == reversed_ids


@pytest.mark.asyncio
async def test_raw_pings_insert_list_count(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        dests = await q.list_destinations(conn)
        dest_id = dests[0].id
        now = _now_ms()
        samples = [
            PingSample(dest_id=dest_id, ts_ms=now - 3000, success=True, latency_us=12_000),
            PingSample(dest_id=dest_id, ts_ms=now - 2000, success=True, latency_us=18_000),
            PingSample(
                dest_id=dest_id,
                ts_ms=now - 1000,
                success=False,
                error_kind="timeout",
            ),
        ]
        await q.insert_raw_pings(conn, samples)

        rows = await q.list_raw_pings(
            conn, dest_id, since_ms=now - 10_000, until_ms=now + 1, limit=100
        )
        assert len(rows) == 3

        counts = await q.count_raw_pings(conn, dest_id, now - 10_000, now + 1)
        assert counts["total"] == 3
        assert counts["loss"] == 1
        assert counts["ok"] == 2

        # filter by status
        only_loss = await q.list_raw_pings(
            conn,
            dest_id,
            since_ms=now - 10_000,
            until_ms=now + 1,
            limit=100,
            statuses={"loss"},
        )
        assert len(only_loss) == 1

        spark = await q.sparkline_pings(conn, dest_id, since_ms=now - 10_000)
        assert len(spark) == 3
        assert spark[0][2] is True


@pytest.mark.asyncio
async def test_outages_lifecycle(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        dests = await q.list_destinations(conn)
        dest_id = dests[0].id
        start = _now_ms()
        outage_id = await q.open_outage(
            conn,
            dest_id=dest_id,
            start_ts_ms=start,
            type_=OutageType.EINZEL,
            lost_count=2,
        )
        opens = await q.open_outages(conn)
        assert any(o["id"] == outage_id for o in opens)

        await q.add_outage_member(conn, outage_id, dest_id, start, None, 2)
        await q.update_outage_type(conn, outage_id, OutageType.MULTI)

        end = start + 4_000
        await q.close_outage(conn, outage_id, end)
        det = await q.get_outage(conn, outage_id)
        assert det is not None
        assert det["end_ts_ms"] == end
        assert det["duration_ms"] == 4000
        assert det["type"] == "MULTI"

        members = await q.get_outage_members(conn, outage_id)
        assert len(members) == 1

        cnt = await q.count_outages(conn, None, start - 1, start + 10_000)
        assert cnt == 1
        total_ms = await q.total_outage_ms(conn, None, start - 1, start + 10_000)
        assert total_ms == 4000

        listed = await q.list_outages(
            conn, since_ms=start - 1, until_ms=start + 10_000, types={OutageType.MULTI}
        )
        assert len(listed) == 1


@pytest.mark.asyncio
async def test_hourly_and_daily_aggregates(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        dests = await q.list_destinations(conn)
        dest_id = dests[0].id
        hour_bucket = (_now_ms() // 3_600_000) * 3_600_000
        await q.upsert_hourly_aggregate(
            conn,
            {
                "dest_id": dest_id,
                "hour_bucket": hour_bucket,
                "count_total": 60,
                "count_ok": 58,
                "count_spike": 1,
                "p50_us": 12_000,
                "p95_us": 30_000,
                "p99_us": 50_000,
                "min_us": 8_000,
                "max_us": 60_000,
                "mean_us": 14_000,
                "loss_pct": 3.33,
                "jitter_us": 1200,
                "outage_count": 0,
                "outage_ms": 0,
                "finalized": 1,
            },
        )
        rows = await q.list_hourly_aggregates(
            conn, dest_id, hour_bucket - 1, hour_bucket + 3_600_000
        )
        assert len(rows) == 1
        assert rows[0]["count_total"] == 60

        latest = await q.latest_finalized_hour(conn, dest_id)
        assert latest == hour_bucket

        day_bucket = hour_bucket - hour_bucket % 86_400_000
        await q.upsert_daily_aggregate(
            conn,
            {
                "dest_id": dest_id,
                "day_bucket": day_bucket,
                "count_total": 86400,
                "count_ok": 86000,
                "count_spike": 10,
                "p50_us": 12_000,
                "p95_us": 30_000,
                "p99_us": 50_000,
                "min_us": 5_000,
                "max_us": 80_000,
                "mean_us": 14_000,
                "loss_pct": 0.46,
                "jitter_us": 1300,
                "outage_count": 1,
                "outage_ms": 5000,
            },
        )
        drows = await q.list_daily_aggregates(
            conn, dest_id, day_bucket - 1, day_bucket + 86_400_000
        )
        assert len(drows) == 1


@pytest.mark.asyncio
async def test_traceroute_insert_and_hops(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        dests = await q.list_destinations(conn)
        dest_id = dests[0].id
        ts = _now_ms()
        hops = [
            TraceHop(hop_no=1, host="gw", ip="192.168.1.1", rtt_us=1500),
            TraceHop(hop_no=2, host=None, ip="10.0.0.1", rtt_us=8000),
        ]
        tid = await q.insert_traceroute(
            conn,
            dest_id=dest_id,
            ts_ms=ts,
            trigger="scheduled",
            success=True,
            hop_count=2,
            signature_hash="abc123",
            route_changed=False,
            hops=hops,
        )
        assert tid > 0
        await q.upsert_route_signature(conn, dest_id, "abc123", "1:192.168.1.1|2:10.0.0.1", ts)
        await q.upsert_route_signature(conn, dest_id, "abc123", "1:192.168.1.1|2:10.0.0.1", ts + 1)

        trace = await q.get_traceroute(conn, tid)
        assert trace is not None
        hops_db = await q.get_trace_hops(conn, tid)
        assert len(hops_db) == 2

        traces = await q.list_traceroutes(conn, dest_id, ts - 1, ts + 10_000)
        assert len(traces) == 1

        hist = await q.hop_history(conn, dest_id, 2, ts - 1)
        assert hist == [(ts, 8000)]


@pytest.mark.asyncio
async def test_wifi_and_stream(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        ts = _now_ms()
        await q.insert_wifi_event(
            conn,
            WifiEvent(
                ts_ms=ts,
                event_type=WifiEventType.REASSOC,
                ssid="FreeWiFi",
                rssi=-65,
                channel=6,
                duration_ms=2500,
            ),
        )
        events = await q.list_wifi_events(conn, ts - 1, ts + 10_000)
        assert len(events) == 1

        await q.insert_rssi_sample(conn, ts, -65, 6, 144000)
        latest = await q.latest_rssi(conn)
        assert latest is not None
        assert latest["rssi"] == -65
        series = await q.rssi_series(conn, ts - 1)
        assert series == [(ts, -65)]

        await q.insert_stream_event(
            conn,
            StreamEvent(
                ts_ms=ts,
                event_type=StreamEventType.DROP,
                duration_ms=1200,
                kbps_before=20,
                kbps_after=0,
                reason="zero_kbps",
            ),
        )
        sevs = await q.list_stream_events(conn, ts - 1, ts + 10_000)
        assert len(sevs) == 1

        await q.insert_throughput_sample(conn, ts, 20, 12_000, 1_000_000)
        await q.insert_throughput_sample(conn, ts + 1000, 21, 13_000, 1_050_000)
        ts_series = await q.throughput_series(conn, ts - 1)
        assert len(ts_series) == 2


@pytest.mark.asyncio
async def test_heartbeat_and_notifications(tmp_path):
    async with open_db(tmp_path / "x.db") as conn:
        ts = _now_ms()
        await q.insert_heartbeat_event(
            conn,
            HeartbeatEvent(
                ts_ms=ts, event_type=HeartbeatEventType.LOSS, duration_ms=900, missed_count=4
            ),
        )
        nid = await q.insert_notification(
            conn,
            severity="warn",
            target_kind="outage",
            target_id=1,
            message="Outage detected",
            ts_ms=ts,
        )
        unseen = await q.list_unseen_notifications(conn)
        assert any(n["id"] == nid for n in unseen)
        await q.mark_notifications_seen(conn, [nid])
        unseen_after = await q.list_unseen_notifications(conn)
        assert not any(n["id"] == nid for n in unseen_after)
