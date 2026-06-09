"""Tests for the software-update endpoints (check/status/install)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pingwatch.api.routes import system as sysroutes


@pytest.mark.asyncio
async def test_update_status_reads_file(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "update-status.json"
    f.write_text(json.dumps(
        {"behind": 3, "current_sha": "abc1234", "remote_sha": "def5678", "ts_ms": 111}
    ))
    monkeypatch.setattr(sysroutes, "_UPDATE_STATUS_FILE", f)
    data = await sysroutes.get_update_status()
    assert data["behind"] == 3
    assert data["current_sha"] == "abc1234"
    assert data["remote_sha"] == "def5678"


@pytest.mark.asyncio
async def test_update_status_missing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sysroutes, "_UPDATE_STATUS_FILE", tmp_path / "nope.json")
    data = await sysroutes.get_update_status()
    assert data["behind"] is None
    assert data["current_sha"] is None


@pytest.mark.asyncio
async def test_install_update_writes_update_check(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_write(line: str) -> None:
        calls.append(line)

    monkeypatch.setattr(sysroutes.host_fifo, "write_command", fake_write)
    res = await sysroutes.install_update()
    assert res.ok is True
    assert calls == ["update_check"]


@pytest.mark.asyncio
async def test_check_update_writes_check_update(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_write(line: str) -> None:
        calls.append(line)

    monkeypatch.setattr(sysroutes.host_fifo, "write_command", fake_write)
    res = await sysroutes.check_update()
    assert res.ok is True
    assert calls == ["check_update"]


@pytest.mark.asyncio
async def test_install_update_reports_fifo_unavailable(monkeypatch) -> None:
    async def fake_write(line: str) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(sysroutes.host_fifo, "write_command", fake_write)
    res = await sysroutes.install_update()
    assert res.ok is False
    assert "fifo" in res.detail.lower()
