# WLAN-Stick als bevorzugte Verbindung mit Failover — Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein eingesteckter USB-WLAN-Stick wird automatisch zur aktiven (route-tragenden) Verbindung; beim Abziehen fällt der Pi auf das interne `wlan0` zurück. PingWatch misst jeweils die aktive Antenne, markiert Antennenwechsel und zeigt die ausgehandelte Link-Rate (umschaltbar mit RSSI) live.

**Architecture:** Single active interface. Ein neuer stdlib-Resolver (`netif.py`) wählt USB-bevorzugt. Der Host-Helper-`wifi_status_loop` löst pro Tick das aktive Interface auf, liest `iw dev <if> link` und stempelt das Interface in `wifi-status.json`; der In-Container-Monitor liest diese JSON, persistiert RSSI/Link-Rate und schreibt bei Interface-Wechsel einen Marker in die neue additive Tabelle `wifi_source_switches`. NetworkManager macht das Route-Failover über Route-Metric (Stick 50 < onboard 100), getriggert per udev-`add` → Host-Helper-Befehl `wifi_prefer_stick`.

**Tech Stack:** Python 3.12+, FastAPI, aiosqlite, pytest/pytest-asyncio, ruff; Bash-Host-Helper + udev + NetworkManager (`nmcli`); No-build-Frontend (Alpine + Chart.js).

**Spec:** `docs/superpowers/specs/2026-06-08-wlan-stick-failover-design.md`

**Branch:** `feat/wlan-stick-failover` (bereits angelegt, auf aktuellem `main`).

**Commands:** Tests `./.venv/bin/python -m pytest -q`; Lint `ruff check .`. Auf dem Pi: SSH-Alias `pingwatch-pi` (User `alex`, passwortloses `sudo`); Repo unter `/opt/pingwatch`; Deploy = `sudo git -C /opt/pingwatch pull --ff-only` (CSS/Templates: Source-Watcher lädt Kiosk neu; Python/Service/udev: `sudo systemctl restart pingwatch.service` bzw. `udevadm control --reload-rules`).

---

## File Structure

**Neu:**
- `src/pingwatch/netif.py` — WLAN-Interface-Resolver (reine stdlib; USB-bevorzugt; CLI-`__main__` für den Host-Helper).
- `tests/unit/test_netif.py` — Resolver-Tests gegen ein Sysfs-Fixture.
- `deploy/99-pingwatch-wlan.rules` — udev: USB-WLAN-`add` → Prefer-Hook.
- `deploy/pingwatch-wlan-prefer` — winziger Hook, schreibt `wifi_prefer_stick\t<if>` in die FIFO.

**Geändert:**
- `src/pingwatch/db/schema.sql` — additive Tabelle `wifi_source_switches`.
- `src/pingwatch/db/q_wifi.py` — `insert_source_switch`, `source_switches`, `link_rate_series`.
- `src/pingwatch/db/queries.py` — Re-Export der drei neuen Funktionen.
- `src/pingwatch/models.py` — `WifiSnapshot.interface` (optionales Feld).
- `src/pingwatch/wifi/monitor.py` — Interface aus JSON lesen; Switch-Marker in `_handle`; Fallback-Pfade setzen Interface + Link-Rate; `run_wifi_monitor` verdrahtet Settings.
- `src/pingwatch/api/routes/wifi.py` — `/overview`: Link-Rate-Serie + `current.interface` + `source_switches`.
- `deploy/pingwatch-host-helper.sh` — Interface pro Tick auflösen + in JSON stempeln; Befehl `wifi_prefer_stick`.
- `deploy/install-pingwatch.sh` — neue udev-Regel + Hook installieren.
- `src/pingwatch/templates/detail/wifi.html` + `src/pingwatch/static/js/app.js` — RSSI-⇄-Link-Speed-Umschalter in derselben Kachel/Chart + Quellen-Label.

---

## Task 1: Interface-Resolver `netif.py`

**Files:**
- Create: `src/pingwatch/netif.py`
- Test: `tests/unit/test_netif.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_netif.py
"""Tests for the WLAN interface resolver."""
from __future__ import annotations

from pathlib import Path

from pingwatch.netif import (
    label_for,
    list_wlan_interfaces,
    resolve_active_interface,
)


def _mk_iface(root: Path, name: str, *, wireless: bool = True, bus: str = "usb") -> None:
    """Build a fake /sys/class/net/<name> entry with a device symlink on *bus*."""
    d = root / name
    d.mkdir(parents=True)
    if wireless:
        (d / "wireless").mkdir()
    busdir = root / "_devices" / bus / name
    busdir.mkdir(parents=True)
    (d / "device").symlink_to(busdir)


def test_lists_only_wireless(tmp_path: Path) -> None:
    _mk_iface(tmp_path, "wlan0", bus="mmc")
    _mk_iface(tmp_path, "eth0", wireless=False, bus="platform")
    assert list_wlan_interfaces(str(tmp_path)) == ["wlan0"]


def test_prefers_usb_over_onboard(tmp_path: Path) -> None:
    _mk_iface(tmp_path, "wlan0", bus="mmc")        # onboard
    _mk_iface(tmp_path, "wlan1", bus="usb")        # stick
    assert resolve_active_interface(str(tmp_path)) == "wlan1"
    assert label_for("wlan1", str(tmp_path)) == "usb"
    assert label_for("wlan0", str(tmp_path)) == "intern"


def test_falls_back_to_onboard_without_usb(tmp_path: Path) -> None:
    _mk_iface(tmp_path, "wlan0", bus="mmc")
    assert resolve_active_interface(str(tmp_path)) == "wlan0"


def test_fallback_name_when_sysfs_empty(tmp_path: Path) -> None:
    assert resolve_active_interface(str(tmp_path), fallback="wlan0") == "wlan0"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/unit/test_netif.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pingwatch.netif'`

- [ ] **Step 3: Write the resolver**

```python
# src/pingwatch/netif.py
"""Resolve the active WLAN interface — USB stick preferred over onboard.

Pure stdlib so the bash host-helper can call it as a CLI
(`PYTHONPATH=/opt/pingwatch/src python3 -m pingwatch.netif`). Reads a
sysfs-style root (the container mounts the host's at /host/sys/class/net).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_SYSFS = "/sys/class/net"


def _is_wireless(iface_dir: Path) -> bool:
    return (iface_dir / "wireless").exists() or (iface_dir / "phy80211").exists()


def _is_usb(iface_dir: Path) -> bool:
    try:
        target = os.path.realpath(iface_dir / "device")
    except OSError:
        return False
    return "/usb" in target


def list_wlan_interfaces(sysfs_root: str = DEFAULT_SYSFS) -> list[str]:
    root = Path(sysfs_root)
    if not root.is_dir():
        return []
    return sorted(e.name for e in root.iterdir() if _is_wireless(e))


def label_for(iface: str, sysfs_root: str = DEFAULT_SYSFS) -> str:
    return "usb" if _is_usb(Path(sysfs_root) / iface) else "intern"


def resolve_active_interface(sysfs_root: str = DEFAULT_SYSFS, fallback: str = "wlan0") -> str:
    ifaces = list_wlan_interfaces(sysfs_root)
    if not ifaces:
        return fallback
    usb = [i for i in ifaces if _is_usb(Path(sysfs_root) / i)]
    if usb:
        return usb[0]
    if fallback in ifaces:
        return fallback
    return ifaces[0]


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the active WLAN interface.")
    ap.add_argument("--sysfs", default=DEFAULT_SYSFS)
    ap.add_argument("--fallback", default="wlan0")
    args = ap.parse_args()
    print(resolve_active_interface(args.sysfs, args.fallback))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/unit/test_netif.py -q && ruff check src/pingwatch/netif.py tests/unit/test_netif.py`
Expected: PASS, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/pingwatch/netif.py tests/unit/test_netif.py
git commit -m "feat(netif): USB-preferred WLAN interface resolver"
```

---

## Task 2: `wifi_source_switches`-Tabelle + Queries

**Files:**
- Modify: `src/pingwatch/db/schema.sql` (nach dem `wifi_rssi_samples`-Block, ~Zeile 171)
- Modify: `src/pingwatch/db/q_wifi.py`
- Modify: `src/pingwatch/db/queries.py` (Re-Export-Facade)
- Test: `tests/unit/test_q_wifi_source_switches.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_q_wifi_source_switches.py
"""Tests for wifi_source_switches persistence + queries."""
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.db import queries


@pytest.mark.asyncio
async def test_table_exists_on_fresh_db(db: aiosqlite.Connection) -> None:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='wifi_source_switches'"
    )
    assert await cur.fetchone() is not None


@pytest.mark.asyncio
async def test_insert_and_query_source_switches(db: aiosqlite.Connection) -> None:
    await queries.insert_source_switch(db, 1000, "wlan0", "wlan1")
    await queries.insert_source_switch(db, 2000, "wlan1", "wlan0")
    rows = await queries.source_switches(db, since_ms=0)
    assert rows == [
        {"ts_ms": 1000, "from_if": "wlan0", "to_if": "wlan1"},
        {"ts_ms": 2000, "from_if": "wlan1", "to_if": "wlan0"},
    ]
    # since_ms filters
    assert await queries.source_switches(db, since_ms=1500) == [
        {"ts_ms": 2000, "from_if": "wlan1", "to_if": "wlan0"},
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/test_q_wifi_source_switches.py -q`
Expected: FAIL — table missing / `AttributeError: module 'pingwatch.db.queries' has no attribute 'insert_source_switch'`

- [ ] **Step 3a: Add the table to `schema.sql`**

Insert directly after the `wifi_rssi_samples` table (after its closing `) WITHOUT ROWID;`, ~line 171):

```sql

-- Antennen-/Quellen-Wechsel (interne vs. USB-WLAN). Additiv: wird auf
-- Bestands-DBs per CREATE TABLE IF NOT EXISTS automatisch angelegt.
CREATE TABLE IF NOT EXISTS wifi_source_switches (
  ts_ms   INTEGER NOT NULL,
  from_if TEXT,
  to_if   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wifi_src_sw_time ON wifi_source_switches(ts_ms DESC);
```

- [ ] **Step 3b: Add query functions to `q_wifi.py`**

Append to `src/pingwatch/db/q_wifi.py`:

```python
async def insert_source_switch(
    conn: aiosqlite.Connection, ts_ms: int, from_if: str | None, to_if: str
) -> None:
    await conn.execute(
        "INSERT INTO wifi_source_switches(ts_ms, from_if, to_if) VALUES (?, ?, ?)",
        (ts_ms, from_if, to_if),
    )
    await conn.commit()


async def source_switches(
    conn: aiosqlite.Connection, since_ms: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT ts_ms, from_if, to_if FROM wifi_source_switches "
        "WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (since_ms,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]
```

- [ ] **Step 3c: Re-export from the facade `queries.py`**

In `src/pingwatch/db/queries.py`, add `insert_source_switch` and `source_switches` to the existing `from .q_wifi import (...)` import block (alongside `insert_rssi_sample`, `insert_wifi_event`, etc.). If the module has an `__all__`, add both names there too.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/unit/test_q_wifi_source_switches.py -q && ruff check src/pingwatch/db/`
Expected: PASS, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/pingwatch/db/schema.sql src/pingwatch/db/q_wifi.py src/pingwatch/db/queries.py tests/unit/test_q_wifi_source_switches.py
git commit -m "feat(db): wifi_source_switches table + queries (additive, no migration)"
```

---

## Task 3: `WifiSnapshot.interface` + Monitor liest Interface aus der Status-JSON

**Files:**
- Modify: `src/pingwatch/models.py:110-119` (WifiSnapshot)
- Modify: `src/pingwatch/wifi/monitor.py` (`_sample_from_file`, `_sample_nl80211`, `_sample_iw`)
- Test: `tests/unit/test_monitor_interface.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_monitor_interface.py
"""Monitor carries the active interface from the host-helper status file."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pingwatch.wifi.monitor import WifiConfig, WifiMonitor


def _write_status(path: Path, **fields: object) -> None:
    base = {"ts_ms": int(time.time() * 1000), "connected": True,
            "ssid": "Cafe-Free", "bssid": "aa:bb:cc:dd:ee:ff",
            "rssi_dbm": -55, "bitrate_mbps": 144.0, "channel": 36}
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")


@pytest.mark.asyncio
async def test_snapshot_carries_interface_from_file(tmp_path: Path) -> None:
    status = tmp_path / "wifi-status.json"
    _write_status(status, interface="wlan1")
    mon = WifiMonitor(None, config=WifiConfig(status_file=str(status)))
    snap = mon._sample_from_file(int(time.time() * 1000))
    assert snap is not None
    assert snap.interface == "wlan1"
    assert snap.link_rate_kbps == 144_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/test_monitor_interface.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'interface'` (WifiSnapshot has no `interface`).

- [ ] **Step 3a: Add `interface` to `WifiSnapshot`**

In `src/pingwatch/models.py`, change the `WifiSnapshot` dataclass to add a trailing optional field:

```python
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
    interface: str | None = None
```

- [ ] **Step 3b: Populate `interface` in the sampling paths of `monitor.py`**

In `_sample_from_file`, read the interface and pass it into **both** returned snapshots:

```python
        iface = data.get("interface")
        if not data.get("connected"):
            return WifiSnapshot(
                ts_ms=ts, ssid=None, bssid=None, rssi=None, channel=None,
                link_rate_kbps=None, associated=False, interface=iface,
            )
        rssi = data.get("rssi_dbm")
        if rssi is None:
            rssi = data.get("rssi")
        bitrate = data.get("bitrate_mbps")
        return WifiSnapshot(
            ts_ms=ts,
            ssid=data.get("ssid"),
            bssid=data.get("bssid"),
            rssi=int(rssi) if rssi is not None else None,
            channel=data.get("channel"),
            link_rate_kbps=int(float(bitrate) * 1000) if bitrate is not None else None,
            associated=True,
            interface=iface,
        )
```

In `_sample_nl80211` and `_sample_iw`, add `interface=self._cfg.interface` to **every** `WifiSnapshot(...)` return in those two methods (the dev/CI fallback knows its interface from config).

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/unit/test_monitor_interface.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite (no regressions from the model change)**

Run: `./.venv/bin/python -m pytest -q && ruff check src/pingwatch/`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pingwatch/models.py src/pingwatch/wifi/monitor.py tests/unit/test_monitor_interface.py
git commit -m "feat(wifi): carry active interface on WifiSnapshot"
```

---

## Task 4: Monitor schreibt Antennenwechsel-Marker

**Files:**
- Modify: `src/pingwatch/wifi/monitor.py` (`WifiMonitor.__init__`, `_handle`)
- Test: `tests/unit/test_monitor_source_switch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_monitor_source_switch.py
"""Monitor records a wifi_source_switches row when the interface changes."""
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.models import WifiSnapshot
from pingwatch.wifi.monitor import WifiMonitor


def _snap(ts: int, iface: str) -> WifiSnapshot:
    return WifiSnapshot(
        ts_ms=ts, ssid="Cafe-Free", bssid="aa:bb:cc:dd:ee:ff",
        rssi=-55, channel=36, link_rate_kbps=144_000, associated=True, interface=iface,
    )


@pytest.mark.asyncio
async def test_records_switch_on_interface_change(db: aiosqlite.Connection) -> None:
    mon = WifiMonitor(db)
    await mon._handle(_snap(1000, "wlan0"))   # first: sets baseline, no marker
    await mon._handle(_snap(2000, "wlan1"))   # change: one marker
    cur = await db.execute(
        "SELECT ts_ms, from_if, to_if FROM wifi_source_switches ORDER BY ts_ms"
    )
    assert [dict(r) for r in await cur.fetchall()] == [
        {"ts_ms": 2000, "from_if": "wlan0", "to_if": "wlan1"},
    ]


@pytest.mark.asyncio
async def test_no_marker_when_interface_stable(db: aiosqlite.Connection) -> None:
    mon = WifiMonitor(db)
    await mon._handle(_snap(1000, "wlan1"))
    await mon._handle(_snap(2000, "wlan1"))
    cur = await db.execute("SELECT COUNT(*) AS n FROM wifi_source_switches")
    assert (await cur.fetchone())["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/test_monitor_source_switch.py -q`
Expected: FAIL — `0 == [{...}]` (no marker written; `_prev_interface` not tracked).

- [ ] **Step 3a: Track previous interface in `__init__`**

In `WifiMonitor.__init__`, after `self._pending_disconnect_ts = None`, add:

```python
        self._prev_interface: str | None = None
```

- [ ] **Step 3b: Emit the marker in `_handle`**

In `_handle`, after `self._prev_snapshot = snap` add:

```python
        if snap.interface and snap.interface != self._prev_interface:
            if self._prev_interface is not None:
                await queries.insert_source_switch(
                    self._conn, snap.ts_ms, self._prev_interface, snap.interface
                )
                await self._bus.publish("wifi.source_switch", snap)
            self._prev_interface = snap.interface
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/unit/test_monitor_source_switch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pingwatch/wifi/monitor.py tests/unit/test_monitor_source_switch.py
git commit -m "feat(wifi): record antenna-switch marker on interface change"
```

---

## Task 5: Fallback-Pfade + `run_wifi_monitor` verdrahtet Settings + Link-Rate auf nl80211

**Files:**
- Modify: `src/pingwatch/wifi/monitor.py` (`run_wifi_monitor`, `_sample_nl80211`)
- Test: `tests/unit/test_run_wifi_monitor_config.py`

> Hintergrund: Auf der Appliance kommt das Interface aus der JSON. Für Dev/CI-Fallback soll `run_wifi_monitor` das konfigurierte Interface in die `WifiConfig` setzen, und der nl80211-Fallback soll die Link-Rate nicht mehr verschlucken (Station-Info), damit Tests/Dev denselben Datengehalt haben.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_run_wifi_monitor_config.py
"""run_wifi_monitor builds a WifiConfig from settings (interface wiring)."""
from __future__ import annotations

import pingwatch.wifi.monitor as monitor_mod


@pytest.mark.asyncio  # noqa: F821 (pytest import added below)
async def test_run_wifi_monitor_passes_interface_from_settings(monkeypatch) -> None:
    captured = {}

    class _FakeMonitor:
        def __init__(self, conn, bus, config=None):
            captured["interface"] = config.interface if config else None

        async def run(self):
            return None

    monkeypatch.setattr(monitor_mod, "WifiMonitor", _FakeMonitor)
    monkeypatch.setenv("PINGWATCH_WLAN_IF", "wlan9")
    # get_settings is lru_cache'd; clear so the env takes effect
    from pingwatch.config import get_settings
    get_settings.cache_clear()

    await monitor_mod.run_wifi_monitor(conn=object(), bus=object())
    assert captured["interface"] == "wlan9"
```

Add `import pytest` at the top of the test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/test_run_wifi_monitor_config.py -q`
Expected: FAIL — `captured["interface"]` is `"wlan0"` (config not built from settings).

- [ ] **Step 3a: Build `WifiConfig` from settings in `run_wifi_monitor`**

Replace the body of `run_wifi_monitor` at the bottom of `monitor.py`:

```python
async def run_wifi_monitor(conn, bus) -> None:
    from pingwatch.config import get_settings

    settings = get_settings()
    cfg = WifiConfig(interface=settings.wifi.interface)
    await WifiMonitor(conn, bus, cfg).run()
```

- [ ] **Step 3b: Populate link rate on the nl80211 fallback path**

In `_sample_nl80211`, after building the BSS dict and before the final `return WifiSnapshot(...)`, derive the bitrate from the station info if available; otherwise leave `None`. Replace the final return's `link_rate_kbps=None` with a best-effort value:

```python
            bitrate_mbps = _nl80211_bitrate_mbps(nl, ifidx)
            return WifiSnapshot(
                ts_ms=ts_ms,
                ssid=ssid,
                bssid=bss.get("NL80211_BSS_BSSID"),
                rssi=_dbm_from_mbm(bss.get("NL80211_BSS_SIGNAL_MBM")),
                channel=_freq_to_channel(bss.get("NL80211_BSS_FREQUENCY")),
                link_rate_kbps=int(bitrate_mbps * 1000) if bitrate_mbps else None,
                associated=True,
                interface=self._cfg.interface,
            )
```

And add a small helper near the other module-level helpers:

```python
def _nl80211_bitrate_mbps(nl, ifidx) -> float | None:  # pragma: no cover - hw-dependent
    """Best-effort negotiated TX bitrate (MBit/s) from station info; None if N/A."""
    try:
        stations = nl.get_stations(ifidx) or []
    except Exception:  # noqa: BLE001
        return None
    for st in stations:
        attrs = dict(st.get("attrs", []))
        sinfo = dict(attrs.get("NL80211_ATTR_STA_INFO", {}).get("attrs", []))
        rate = dict(sinfo.get("NL80211_STA_INFO_TX_BITRATE", {}).get("attrs", []))
        bitrate = rate.get("NL80211_RATE_INFO_BITRATE32") or rate.get("NL80211_RATE_INFO_BITRATE")
        if bitrate:
            return float(bitrate) / 10.0  # nl80211 reports in 100 kbit/s units
    return None
```

> Note: `_sample_nl80211` is marked `# pragma: no cover` (hardware-dependent); the helper is too. Tests cover the file path (Task 3) and the config wiring (this task), not live nl80211.

- [ ] **Step 4: Run test + full suite to verify**

Run: `./.venv/bin/python -m pytest -q && ruff check src/pingwatch/wifi/monitor.py`
Expected: PASS, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/pingwatch/wifi/monitor.py tests/unit/test_run_wifi_monitor_config.py
git commit -m "feat(wifi): wire interface from settings + link-rate on nl80211 fallback"
```

---

## Task 6: `link_rate_series` Query

**Files:**
- Modify: `src/pingwatch/db/q_wifi.py`
- Modify: `src/pingwatch/db/queries.py` (Re-Export)
- Test: `tests/unit/test_q_wifi_link_rate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_q_wifi_link_rate.py
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.db import queries


@pytest.mark.asyncio
async def test_link_rate_series_skips_nulls(db: aiosqlite.Connection) -> None:
    await queries.insert_rssi_sample(db, 1000, -50, 36, 144_000)
    await queries.insert_rssi_sample(db, 2000, -52, 36, None)   # no link rate
    await queries.insert_rssi_sample(db, 3000, -48, 36, 86_000)
    series = await queries.link_rate_series(db, since_ms=0)
    assert series == [(1000, 144_000), (3000, 86_000)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/test_q_wifi_link_rate.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'link_rate_series'`

- [ ] **Step 3a: Add `link_rate_series` to `q_wifi.py`**

```python
async def link_rate_series(
    conn: aiosqlite.Connection, since_ms: int
) -> list[tuple[int, int]]:
    cur = await conn.execute(
        "SELECT ts_ms, link_rate_kbps FROM wifi_rssi_samples "
        "WHERE ts_ms >= ? AND link_rate_kbps IS NOT NULL ORDER BY ts_ms ASC",
        (since_ms,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r["ts_ms"], r["link_rate_kbps"]) for r in rows]
```

- [ ] **Step 3b: Re-export from `queries.py`**

Add `link_rate_series` to the `from .q_wifi import (...)` block (and `__all__` if present).

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/unit/test_q_wifi_link_rate.py -q && ruff check src/pingwatch/db/`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pingwatch/db/q_wifi.py src/pingwatch/db/queries.py tests/unit/test_q_wifi_link_rate.py
git commit -m "feat(db): link_rate_series query"
```

---

## Task 7: `/api/wifi/overview` — Link-Rate-Serie + Interface + Switch-Marker

**Files:**
- Modify: `src/pingwatch/api/routes/wifi.py` (`overview_endpoint`, ~Zeile 163-275)
- Test: `tests/integration/test_wifi_overview.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_wifi_overview.py
"""overview endpoint exposes link-rate series, interface, and switch markers."""
from __future__ import annotations

import aiosqlite
import pytest

from pingwatch.api.routes import wifi as wifi_routes
from pingwatch.db import queries


@pytest.mark.asyncio
async def test_overview_includes_link_series_and_switches(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    # Live status file → current.interface
    monkeypatch.setattr(
        wifi_routes, "_read_wifi_status_file",
        lambda: {"connected": True, "ssid": "Cafe-Free", "rssi_dbm": -55,
                 "bitrate_mbps": 144.0, "interface": "wlan1", "interface_label": "usb"},
    )
    now_ms = 10_000_000
    monkeypatch.setattr(wifi_routes.time, "time", lambda: now_ms / 1000)

    await queries.insert_rssi_sample(db, now_ms - 5000, -50, 36, 144_000)
    await queries.insert_rssi_sample(db, now_ms - 4000, -52, 36, 86_000)
    await queries.insert_source_switch(db, now_ms - 4500, "wlan0", "wlan1")

    payload = await wifi_routes.overview_endpoint(conn=db, range_="24h")

    assert payload["current"]["interface"] == "wlan1"
    assert payload["current"]["interface_label"] == "usb"
    assert payload["link_series"] == [
        {"ts_ms": now_ms - 5000, "link_rate_kbps": 144_000},
        {"ts_ms": now_ms - 4000, "link_rate_kbps": 86_000},
    ]
    assert payload["source_switches"] == [
        {"ts_ms": now_ms - 4500, "from_if": "wlan0", "to_if": "wlan1"},
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/integration/test_wifi_overview.py -q`
Expected: FAIL — `KeyError: 'interface'` / `'link_series'`.

- [ ] **Step 3a: Add interface to `current`**

In `overview_endpoint`, extend the `current = {...}` dict (after `"security": live.get("security"),`) with:

```python
        "interface": live.get("interface"),
        "interface_label": live.get("interface_label"),
```

- [ ] **Step 3b: Build the link-rate series**

After the RSSI `series` block (right after the `else: series = raw_series`), add:

```python
    # --- Link-Rate-Serie (kbps; null-Werte übersprungen) ---
    link_series = [
        {"ts_ms": ts, "link_rate_kbps": kbps}
        for ts, kbps in await queries.link_rate_series(conn, since_ms)
    ]
```

- [ ] **Step 3c: Fetch source switches**

After the `aps` block, add:

```python
    source_switches = await queries.source_switches(conn, since_ms)
```

- [ ] **Step 3d: Extend the return dict**

Add to the final `return {...}`:

```python
        "link_series": link_series,
        "source_switches": source_switches,
```

(Ensure `from pingwatch.db import queries` is imported at the top of `wifi.py`; if the module currently uses raw SQL only, add the import.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/integration/test_wifi_overview.py -q && ruff check src/pingwatch/api/routes/wifi.py`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/pingwatch/api/routes/wifi.py tests/integration/test_wifi_overview.py
git commit -m "feat(api): overview exposes link-rate series, interface, source switches"
```

---

## Task 8: Host-Helper — aktives Interface pro Tick auflösen + in JSON stempeln

**Files:**
- Modify: `deploy/pingwatch-host-helper.sh` (`write_wifi_status`, Resolver-Helper)

> Bash + Live-Hardware → keine Unit-Tests. Konkrete Änderung + manuelle Pi-Verifikation.

- [ ] **Step 1: Add a resolver helper near the top (after `WLAN_IF=...`, ~line 14)**

```bash
PW_SRC="${PINGWATCH_SRC:-/opt/pingwatch/src}"

resolve_wlan_if() {
  # USB-bevorzugt; Fallback = konfiguriertes wlan0. Reiner stdlib-Aufruf.
  PYTHONPATH="$PW_SRC" python3 -m pingwatch.netif \
    --sysfs /sys/class/net --fallback "${PINGWATCH_WLAN_IF:-wlan0}" 2>/dev/null \
    || echo "${PINGWATCH_WLAN_IF:-wlan0}"
}

iface_label() {
  case "$1" in
    wlan0) echo "intern" ;;
    *) if [ -d "/sys/class/net/$1/device" ] && \
          readlink -f "/sys/class/net/$1/device" | grep -q "/usb"; then
         echo "usb"; else echo "intern"; fi ;;
  esac
}
```

- [ ] **Step 2: Use the resolved interface in `write_wifi_status` + stamp it into the JSON**

Change `write_wifi_status` so it resolves the interface each call and passes name+label to the JSON builder:

```bash
write_wifi_status() {
  local security="${1:-}"
  local wlan_if label link tmp
  wlan_if=$(resolve_wlan_if)
  label=$(iface_label "$wlan_if")
  link=$(iw dev "$wlan_if" link 2>/dev/null || true)
  tmp=$(mktemp "${SHARED_DIR}/.wifi-status.XXXXXX" 2>/dev/null) || return 0
  python3 - "$link" "$security" "$wlan_if" "$label" > "$tmp" <<'PY'
import json, time, sys, re
link = sys.argv[1] if len(sys.argv) > 1 else ''
security = sys.argv[2] if len(sys.argv) > 2 else ''
iface = sys.argv[3] if len(sys.argv) > 3 else None
label = sys.argv[4] if len(sys.argv) > 4 else None
connected = 'Connected to' in link
# ... (unveränderte find()/freq_to_chan()/Feld-Extraktion) ...
print(json.dumps({
  'ts_ms': int(time.time() * 1000),
  'connected': connected,
  'ssid': ssid.strip() if ssid else None,
  'bssid': find(r'Connected to ([0-9a-fA-F:]+)'),
  'rssi_dbm': rssi,
  'signal_pct': pct,
  'bitrate_mbps': find(r'tx bitrate:\s*([\d.]+)', float),
  'freq': freq,
  'channel': freq_to_chan(freq),
  'security': security or None,
  'interface': iface,
  'interface_label': label,
}))
PY
  chmod 644 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "${SHARED_DIR}/wifi-status.json" 2>/dev/null || rm -f "$tmp"
}
```

(Keep the existing `find()` / `freq_to_chan()` / field-extraction lines unchanged; only the `sys.argv` reads and the two new JSON keys are added.)

- [ ] **Step 3: Deploy + verify on the Pi**

```bash
git add deploy/pingwatch-host-helper.sh && git commit -m "feat(host-helper): resolve active WLAN interface per tick + stamp into status json"
git push origin feat/wlan-stick-failover
# On the Pi (host-helper is installed to /usr/local/bin and runs as a service):
ssh pingwatch-pi 'sudo git -C /opt/pingwatch fetch origin && sudo git -C /opt/pingwatch checkout feat/wlan-stick-failover && sudo git -C /opt/pingwatch pull --ff-only'
ssh pingwatch-pi 'sudo install -m0755 /opt/pingwatch/deploy/pingwatch-host-helper.sh /usr/local/bin/pingwatch-host-helper.sh && sudo systemctl restart pingwatch-host-helper.service'
```

- [ ] **Step 4: Manual verification (no stick yet)**

```bash
ssh pingwatch-pi 'cat /run/pingwatch-shared/wifi-status.json'
```
Expected: JSON now contains `"interface": "wlan0"` and `"interface_label": "intern"`.

---

## Task 9: Host-Helper — Befehl `wifi_prefer_stick`

**Files:**
- Modify: `deploy/pingwatch-host-helper.sh` (neuer `case`-Zweig im FIFO-Dispatch)

- [ ] **Step 1: Add the command branch**

In the `case "$cmd" in` block (alongside `wifi_connect)` etc.), add:

```bash
      wifi_prefer_stick)
        # payload = USB-WLAN-Interface-Name. Verbindet die aktuell genutzte SSID
        # auf dem Stick, bindet das Profil ans Interface und gibt ihm die
        # niedrigere Route-Metric, damit der Stick die Default-Route gewinnt.
        iface="$payload"
        ssid="$(nmcli -t -f IN-USE,SSID dev wifi 2>/dev/null | awk -F: '$1=="*"{print $2; exit}')"
        [ -z "$ssid" ] && ssid="${PINGWATCH_EXPECTED_SSID:-}"
        log "wifi_prefer_stick iface=$iface ssid=${ssid:-<none>}"
        if [ -n "$iface" ] && [ -n "$ssid" ]; then
          nmcli dev wifi connect "$ssid" ifname "$iface" 2>&1 | logger -t pingwatch-host-helper || true
          con="$(nmcli -t -f NAME,DEVICE c show --active 2>/dev/null | awk -F: -v d="$iface" '$2==d{print $1; exit}')"
          if [ -n "$con" ]; then
            nmcli c modify "$con" \
              ipv4.route-metric 50 ipv6.route-metric 50 \
              connection.autoconnect yes connection.autoconnect-retries 0 \
              2>&1 | logger -t pingwatch-host-helper || true
            nmcli c up "$con" ifname "$iface" 2>&1 | logger -t pingwatch-host-helper || true
          fi
        fi
        ;;
```

> The onboard profile keeps its default metric (NM default 100 for wifi), so when the stick (metric 50) is up it wins the default route; when the stick disappears, NM removes its routes and onboard resumes automatically.

- [ ] **Step 2: Deploy + manual verification on the Pi (with the stick plugged in)**

```bash
ssh pingwatch-pi 'sudo install -m0755 /opt/pingwatch/deploy/pingwatch-host-helper.sh /usr/local/bin/pingwatch-host-helper.sh && sudo systemctl restart pingwatch-host-helper.service'
# Determine the stick's interface name first:
ssh pingwatch-pi 'iw dev'
# Trigger the command manually (replace wlan1 with the real stick iface):
ssh pingwatch-pi 'printf "wifi_prefer_stick\twlan1\n" | sudo tee /run/pingwatch-host.fifo >/dev/null'
sleep 4
ssh pingwatch-pi 'nmcli -t -f NAME,DEVICE,STATE c show --active; ip route | grep default'
```
Expected: a connection active on the stick iface; the default route via the stick (lower metric). `cat /run/pingwatch-shared/wifi-status.json` now shows `"interface":"wlan1","interface_label":"usb"`.

- [ ] **Step 3: Commit**

```bash
git add deploy/pingwatch-host-helper.sh
git commit -m "feat(host-helper): wifi_prefer_stick command (connect on stick + route-metric)"
```

---

## Task 10: udev-Regel + Installer-Verdrahtung (Plug-and-Play-Trigger)

**Files:**
- Create: `deploy/99-pingwatch-wlan.rules`
- Create: `deploy/pingwatch-wlan-prefer`
- Modify: `deploy/install-pingwatch.sh` (neben der bestehenden `99-pingwatch-usb.rules`-Installation)

- [ ] **Step 1: Create the udev rule**

```
# deploy/99-pingwatch-wlan.rules
# Wird ein USB-WLAN-Interface hinzugefügt, bittet PingWatch den Host-Helper,
# es als aktive Verbindung zu bevorzugen.
ACTION=="add", SUBSYSTEM=="net", SUBSYSTEMS=="usb", ENV{DEVTYPE}=="wlan", \
  RUN+="/usr/local/bin/pingwatch-wlan-prefer %k"
```

- [ ] **Step 2: Create the prefer hook**

```bash
#!/bin/bash
# deploy/pingwatch-wlan-prefer  — winziger udev-Hook. Schreibt einen Befehl in
# die Host-Helper-FIFO; läuft als root (udev). Kurz halten (udev killt langes RUN);
# systemd-run entkoppelt die FIFO-Schreibung von der udev-Worker-Lebensdauer.
IF="$1"
FIFO="/run/pingwatch-host.fifo"
[ -n "$IF" ] || exit 0
[ -p "$FIFO" ] || exit 0
systemd-run --quiet --no-block /bin/sh -c "printf 'wifi_prefer_stick\t%s\n' '$IF' > '$FIFO'" 2>/dev/null \
  || printf 'wifi_prefer_stick\t%s\n' "$IF" > "$FIFO"
```

- [ ] **Step 3: Wire the installer**

In `deploy/install-pingwatch.sh`, next to where `99-pingwatch-usb.rules` is installed, add:

```bash
install -m 0644 "${DEPLOY_DIR}/99-pingwatch-wlan.rules" /etc/udev/rules.d/99-pingwatch-wlan.rules
install -m 0755 "${DEPLOY_DIR}/pingwatch-wlan-prefer"   /usr/local/bin/pingwatch-wlan-prefer
udevadm control --reload-rules && udevadm trigger --subsystem-match=net || true
```

- [ ] **Step 4: Deploy + manual verification (plug/unplug)**

```bash
ssh pingwatch-pi 'sudo install -m0644 /opt/pingwatch/deploy/99-pingwatch-wlan.rules /etc/udev/rules.d/99-pingwatch-wlan.rules && sudo install -m0755 /opt/pingwatch/deploy/pingwatch-wlan-prefer /usr/local/bin/pingwatch-wlan-prefer && sudo udevadm control --reload-rules'
# Physically unplug + replug the stick, then:
ssh pingwatch-pi 'journalctl -t pingwatch-host-helper --no-pager -n 20'
ssh pingwatch-pi 'ip route | grep default; cat /run/pingwatch-shared/wifi-status.json'
```
Expected: replug logs a `wifi_prefer_stick` invocation; default route + status JSON switch to the stick. Unplug → route + JSON fall back to `wlan0` within ~1-2 s.

- [ ] **Step 5: Commit**

```bash
git add deploy/99-pingwatch-wlan.rules deploy/pingwatch-wlan-prefer deploy/install-pingwatch.sh
git commit -m "feat(deploy): udev auto-prefer USB WLAN stick on plug-in"
```

---

## Task 11: Frontend — RSSI ⇄ Link-Speed-Umschalter + Quellen-Label

**Files:**
- Modify: `src/pingwatch/templates/detail/wifi.html` (Live-Status-Box + Chart-Kopf)
- Modify: `src/pingwatch/static/js/app.js` (Overview-State, Chart-Datenquelle)

> No-build-Frontend, keine Unit-Tests → konkrete Snippets + Browser-/Kiosk-Verifikation.

- [ ] **Step 1: Quellen-Label in der Live-Status-Box anzeigen**

In `src/pingwatch/templates/detail/wifi.html`, in der `current`-Box (nach der `Status`-Zeile, ~Zeile 57) eine Zeile ergänzen:

```html
          <div><span class="t">Quelle</span><span x-text="(detail.wifi_overview.current.interface_label === 'usb' ? 'Stick' : 'Intern') + ' · ' + (detail.wifi_overview.current.interface || '—')"></span></div>
```

- [ ] **Step 2: Umschalter im Chart-Kopf + Achsen-Logik**

Im Kopf der RSSI-Chart-Karte (dort wo der Chart-Titel/`<canvas>` steht) einen Umschalter ergänzen. Alpine-State steuert die gezeigte Metrik:

```html
<div class="ph">
  <div class="pt" x-text="wifiMetric === 'link' ? 'Link-Speed' : 'RSSI'"></div>
  <div class="seg">
    <button :class="wifiMetric==='rssi' && 'on'" @click="setWifiMetric('rssi')">RSSI</button>
    <button :class="wifiMetric==='link' && 'on'" @click="setWifiMetric('link')">Link-Speed</button>
  </div>
</div>
```

- [ ] **Step 3: Chart-Datenquelle umschalten (app.js)**

Im Alpine-Component-State (wo `detail`/Chart-Init lebt) ergänzen:

```javascript
  wifiMetric: 'rssi',
  setWifiMetric(m) {
    this.wifiMetric = m;
    this.renderWifiChart();   // bestehende Render-Funktion; siehe unten
  },
```

`renderWifiChart()` (bzw. die Stelle, die heute `detail.wifi_overview.series` mit der RSSI-Linie zeichnet) so anpassen, dass sie je nach `wifiMetric` die richtige Serie + Achse nimmt:

```javascript
renderWifiChart() {
  const ov = this.detail.wifi_overview;
  if (!ov) return;
  const isLink = this.wifiMetric === 'link';
  const points = isLink
    ? (ov.link_series || []).map(p => ({ x: p.ts_ms, y: p.link_rate_kbps / 1000 }))   // MBit/s
    : (ov.series || []).map(p => ({ x: p.ts_ms, y: p.rssi }));                          // dBm
  const unit = isLink ? 'MBit/s' : 'dBm';
  // ... vorhandene Chart.js-Update-Logik: dataset.data = points;
  //     yAxis.title.text = unit; chart.update(); ...
}
```

> Die exakte Chart.js-Verdrahtung an den bestehenden WLAN-Chart-Code anlehnen (gleiche `chart.data.datasets[0].data = points` / `chart.options.scales.y.title.text = unit` / `chart.update()`-Pattern wie bei den anderen Detail-Charts). Die Live-Kachel (große RSSI-Zahl) analog je nach `wifiMetric` auf `bitrate_mbps` + Einheit umschalten.

- [ ] **Step 4: Minimal-CSS für den Segment-Umschalter** (falls `.seg` noch nicht existiert), in `static/css/pingwatch.css`:

```css
.seg { display:inline-flex; gap:2px; }
.seg button { font-size:11px; padding:2px 8px; border:1px solid #374151; background:transparent; color:#9ca3af; cursor:pointer; }
.seg button.on { background:#374151; color:#e5e7eb; }
```

- [ ] **Step 5: Deploy + Browser/Kiosk-Verifikation**

```bash
git add src/pingwatch/templates/detail/wifi.html src/pingwatch/static/js/app.js src/pingwatch/static/css/pingwatch.css
git commit -m "feat(ui): RSSI<->Link-Speed toggle in one chart + source label"
git push origin feat/wlan-stick-failover
ssh pingwatch-pi 'sudo git -C /opt/pingwatch pull --ff-only'   # Source-Watcher reloads kiosk
```
Expected (im WLAN-Detail): Umschalter wechselt zwischen RSSI (dBm) und Link-Speed (MBit/s) im **gleichen** Chart/Kachel; Live-Status zeigt „Quelle: Stick · wlan1" bzw. „Intern · wlan0".

---

## Task 12: End-to-End-Verifikation auf dem Pi

> Keine Code-Änderung — Abnahme des Gesamtverhaltens. Spec-Abschnitte „Captive-Portal" und „Auf dem Pi zu verifizieren".

- [ ] **Step 1: Voraussetzungen**

```bash
ssh pingwatch-pi 'sudo systemctl restart pingwatch.service pingwatch-host-helper.service'
ssh pingwatch-pi 'iw dev'   # Stick-Interfacename bestätigen (wlxMAC vs wlanN)
```

- [ ] **Step 2: Failover prüfen**

- Stick **einstecken** → innerhalb weniger Sekunden: `ip route | grep default` zeigt Route über Stick (Metric 50); `/run/pingwatch-shared/wifi-status.json` → `interface_label:"usb"`; im WLAN-Detail erscheint ein Antennenwechsel-Marker (`source_switches`) und „Quelle: Stick".
- Stick **abziehen** → Route + Status fallen auf `wlan0` zurück; zweiter Switch-Marker.

- [ ] **Step 3: Captive Portal bestätigen**

- Nach dem Wechsel auf den Stick ggf. einmal das Portal akzeptieren (eigene MAC). RSSI/Link-Speed werden auch ohne Portal-Akzeptanz angezeigt (kommen aus der Assoziation).

- [ ] **Step 4: Stabilität**

```bash
ssh pingwatch-pi 'journalctl -u pingwatch-host-helper -u pingwatch --since "-5 min" --no-pager | tail -40'
```
Expected: keine NM-Flap-Schleife; die portal-limitierte Stick-Verbindung bleibt assoziiert (`autoconnect-retries 0`).

- [ ] **Step 5: Abschluss** — Branch fertigstellen (Skill `superpowers:finishing-a-development-branch`): PR gegen `main` oder Merge, je nach Wunsch.

---

## Self-Review-Notiz

- **Spec-Abdeckung:** Resolver (T1), Switch-Marker-Tabelle (T2), Interface-Tracking (T3/T4), Settings-Wiring + nl80211-Link-Rate (T5), Link-Rate-Serie (T6), Overview (T7), Host-Helper-Interfacewahl + Stamp (T8), Auto-Connect-Befehl (T9), udev-Plug-and-Play (T10), Umschalter + Quellen-Label (T11), Captive-Portal/Failover-Abnahme (T12). Route-Metric-Failover ist NM-getrieben (T9/T12), kein eigener Failover-Code — wie in der Spec.
- **Bewusst manuell statt Unit-Test:** Bash/udev/NM/Frontend (T8-T11) — auf dem Pi verifiziert; die testbare Logik (Resolver, DB, Monitor-Marker, Overview) ist per pytest abgedeckt.
- **Typen-Konsistenz:** `interface` (WifiSnapshot, JSON-Key, `current.interface`), `interface_label` (JSON + `current`), `link_series`/`link_rate_kbps`, `source_switches`/`from_if`/`to_if` durchgängig gleich benannt.
