"""Smoke tests — every API endpoint mounts and returns sensible responses
against a freshly-seeded SQLite DB.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pingwatch.api.app import build_app


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="pw-test-")
    os.close(fd)
    app = build_app(db_path=path)
    with TestClient(app) as c:
        yield c
    with contextlib.suppress(OSError):
        os.unlink(path)


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "PingWatch" in r.text


def test_static_css(client: TestClient) -> None:
    r = client.get("/static/css/pingwatch.css")
    assert r.status_code == 200
    assert ".pw-card" in r.text


def test_dashboard_endpoint(client: TestClient) -> None:
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = r.json()
    for k in ("hero", "stream", "wifi", "cards", "timeline", "events"):
        assert k in data


def test_targets_crud(client: TestClient) -> None:
    r = client.get("/api/targets")
    assert r.status_code == 200
    initial = r.json()
    assert isinstance(initial, list)
    assert len(initial) >= 4  # seeded defaults

    created = client.post(
        "/api/targets",
        json={
            "name": "Test Target",
            "address": "example.com",
            "type": "ICMP",
            "kind": "external",
            "interval_ms": 1000,
            "timeout_ms": 2000,
            "enabled": True,
            "ordering": 99,
        },
    )
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    fetched = client.get(f"/api/targets/{tid}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Test Target"

    patched = client.patch(f"/api/targets/{tid}", json={"name": "Renamed"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"

    deleted = client.delete(f"/api/targets/{tid}")
    assert deleted.status_code == 200

    test = client.post(f"/api/targets/{initial[0]['id']}/test")
    assert test.status_code == 200


def test_target_reorder(client: TestClient) -> None:
    r = client.get("/api/targets")
    ids = [t["id"] for t in r.json()]
    rev = list(reversed(ids))
    res = client.post("/api/targets/reorder", json={"order": rev})
    assert res.status_code == 200


def test_outages_list(client: TestClient) -> None:
    r = client.get("/api/outages?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body


def test_outages_detail_404(client: TestClient) -> None:
    r = client.get("/api/outages/99999")
    assert r.status_code == 404


def test_traces_list(client: TestClient) -> None:
    r = client.get("/api/destinations/1/traces")
    assert r.status_code == 200


def test_hop_trends(client: TestClient) -> None:
    r = client.get("/api/destinations/1/hop-trends")
    assert r.status_code == 200


def test_pings_list(client: TestClient) -> None:
    r = client.get("/api/destinations/1/pings?limit=10")
    assert r.status_code == 200


def test_overview(client: TestClient) -> None:
    r = client.get("/api/destinations/1/overview?range=24h")
    assert r.status_code == 200
    data = r.json()
    for k in ("kpi", "percentiles", "histogram"):
        assert k in data


def test_settings_roundtrip(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 200
    initial = r.json()
    assert "device.name" in initial

    p = client.patch("/api/settings", json={"device.name": "Test-Pi"})
    assert p.status_code == 200

    r2 = client.get("/api/settings")
    assert r2.json().get("device.name") == "Test-Pi"


def test_export_endpoints(client: TestClient) -> None:
    r = client.post("/api/export/csv?range=24h")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    r = client.post("/api/export/json?range=24h")
    assert r.status_code == 200
    r = client.post("/api/export/zip?range=24h")
    assert r.status_code == 200


def test_export_usb_missing(client: TestClient) -> None:
    r = client.post("/api/export/usb?range=24h&format=zip")
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body


def test_system_endpoint(client: TestClient) -> None:
    r = client.get("/api/system")
    assert r.status_code == 200
    for k in ("cpu_pct", "ram_used_mb", "ram_total_mb", "db_size_mb"):
        assert k in r.json()


def test_system_clear_data(client: TestClient) -> None:
    r = client.post("/api/system/clear-data")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_factory_reset_requires_confirm(client: TestClient) -> None:
    r = client.post("/api/system/factory-reset")
    assert r.status_code == 400
    r2 = client.post("/api/system/factory-reset?confirm=yes")
    assert r2.status_code == 200


def test_restart(client: TestClient) -> None:
    r = client.post("/api/system/restart")
    assert r.status_code == 200
    assert "ok" in r.json()
