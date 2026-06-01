"""1 Hz WiFi monitor with pyroute2 primary path and ``iw`` subprocess fallback.

Each tick we read the current association (SSID, BSSID, RSSI, channel,
link-rate) and store it as a snapshot. Snapshots are diffed via
``wifi.events`` to produce discrete WifiEvents. An ``iw event`` subprocess
is launched in parallel as a supplementary signal -- its lines are parsed
opportunistically; the snapshot diff remains the source of truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import time
from dataclasses import dataclass

import structlog

from pingwatch.bus import Bus, get_bus
from pingwatch.db import queries
from pingwatch.models import WifiEvent, WifiEventType, WifiSnapshot
from pingwatch.wifi.events import diff_snapshots

log = structlog.get_logger(__name__)

try:  # pragma: no cover - import-time only
    from pyroute2 import NL80211  # type: ignore[import-untyped]

    _HAS_NL80211 = True
except Exception:  # noqa: BLE001
    NL80211 = None  # type: ignore[assignment]
    _HAS_NL80211 = False


@dataclass(slots=True)
class WifiConfig:
    interface: str = "wlan0"
    sample_interval_s: float = 1.0
    reassoc_min_duration_ms: int = 2000


_IW_LINK_RE = {
    "ssid": re.compile(r"^\s*SSID:\s*(.+)$", re.MULTILINE),
    "bssid": re.compile(r"Connected to ([0-9a-fA-F:]+)"),
    "freq": re.compile(r"freq:\s*(\d+)"),
    "signal": re.compile(r"signal:\s*(-?\d+)"),
    "rx_bitrate": re.compile(r"rx bitrate:\s*([\d.]+)\s*MBit/s"),
}


class WifiMonitor:
    def __init__(
        self,
        conn: object,
        bus: Bus | None = None,
        config: WifiConfig | None = None,
    ) -> None:
        self._conn = conn
        self._bus = bus or get_bus()
        self._cfg = config or WifiConfig()
        self._stop = asyncio.Event()
        self._prev_snapshot: WifiSnapshot | None = None
        self._pending_disconnect_ts: int | None = None

    async def run(self) -> None:
        iw_event_task = asyncio.create_task(self._iw_event_loop())
        try:
            while not self._stop.is_set():
                try:
                    snapshot = await self._sample()
                except Exception:  # noqa: BLE001
                    log.exception("wifi.sample_error")
                    snapshot = WifiSnapshot(
                        ts_ms=int(time.time() * 1000),
                        ssid=None,
                        bssid=None,
                        rssi=None,
                        channel=None,
                        link_rate_kbps=None,
                        associated=False,
                    )
                await self._handle(snapshot)
                await asyncio.sleep(self._cfg.sample_interval_s)
        finally:
            iw_event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await iw_event_task

    async def _handle(self, snap: WifiSnapshot) -> None:
        await self._bus.publish("wifi.samples", snap)
        if snap.associated and snap.rssi is not None:
            await queries.insert_rssi_sample(
                self._conn,
                snap.ts_ms,
                snap.rssi,
                snap.channel,
                snap.link_rate_kbps,
            )
        raw_events = diff_snapshots(self._prev_snapshot, snap)
        events = await self._coalesce_reassoc(raw_events, snap)
        for ev in events:
            await queries.insert_wifi_event(self._conn, ev)
            await self._bus.publish("wifi.events", ev)
        self._prev_snapshot = snap

    async def _coalesce_reassoc(
        self, events: list[WifiEvent], snap: WifiSnapshot
    ) -> list[WifiEvent]:
        out: list[WifiEvent] = []
        for ev in events:
            if ev.event_type == WifiEventType.DISCONNECT:
                self._pending_disconnect_ts = ev.ts_ms
                out.append(ev)
            elif ev.event_type == WifiEventType.CONNECT and self._pending_disconnect_ts is not None:
                duration = ev.ts_ms - self._pending_disconnect_ts
                self._pending_disconnect_ts = None
                if duration < self._cfg.reassoc_min_duration_ms:
                    out.append(
                        WifiEvent(
                            ts_ms=ev.ts_ms,
                            event_type=WifiEventType.REASSOC,
                            ssid=ev.ssid,
                            bssid=ev.bssid,
                            rssi=ev.rssi,
                            channel=ev.channel,
                            duration_ms=duration,
                        )
                    )
                else:
                    out.append(
                        WifiEvent(
                            ts_ms=ev.ts_ms,
                            event_type=WifiEventType.CONNECT,
                            ssid=ev.ssid,
                            bssid=ev.bssid,
                            rssi=ev.rssi,
                            channel=ev.channel,
                            duration_ms=duration,
                        )
                    )
            else:
                out.append(ev)
        return out

    async def _sample(self) -> WifiSnapshot:
        ts_ms = int(time.time() * 1000)
        if _HAS_NL80211:
            with contextlib.suppress(Exception):
                return await asyncio.to_thread(self._sample_nl80211, ts_ms)
        return await self._sample_iw(ts_ms)

    def _sample_nl80211(self, ts_ms: int) -> WifiSnapshot:  # pragma: no cover - hw-dependent
        assert NL80211 is not None
        nl = NL80211()
        try:
            nl.bind()
            try:
                ifidx = nl.get_interface_by_name(self._cfg.interface)["ifindex"]
            except Exception:
                return WifiSnapshot(
                    ts_ms=ts_ms,
                    ssid=None,
                    bssid=None,
                    rssi=None,
                    channel=None,
                    link_rate_kbps=None,
                    associated=False,
                )
            scan = nl.get_associated_bss(ifidx) or []
            if not scan:
                return WifiSnapshot(
                    ts_ms=ts_ms,
                    ssid=None,
                    bssid=None,
                    rssi=None,
                    channel=None,
                    link_rate_kbps=None,
                    associated=False,
                )
            entry = scan[0]
            attrs = dict(entry.get("attrs", []))
            bss = dict(attrs.get("NL80211_ATTR_BSS", {}).get("attrs", []))
            ies = bss.get("NL80211_BSS_INFORMATION_ELEMENTS")
            ssid = None
            if isinstance(ies, dict):
                ssid = ies.get("SSID")
            return WifiSnapshot(
                ts_ms=ts_ms,
                ssid=ssid,
                bssid=bss.get("NL80211_BSS_BSSID"),
                rssi=_dbm_from_mbm(bss.get("NL80211_BSS_SIGNAL_MBM")),
                channel=_freq_to_channel(bss.get("NL80211_BSS_FREQUENCY")),
                link_rate_kbps=None,
                associated=True,
            )
        finally:
            with contextlib.suppress(Exception):
                nl.close()

    async def _sample_iw(self, ts_ms: int) -> WifiSnapshot:
        proc = await asyncio.create_subprocess_exec(
            "iw",
            "dev",
            self._cfg.interface,
            "link",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode("utf-8", errors="replace")
        if "Not connected." in text or not text.strip():
            return WifiSnapshot(
                ts_ms=ts_ms,
                ssid=None,
                bssid=None,
                rssi=None,
                channel=None,
                link_rate_kbps=None,
                associated=False,
            )
        ssid = _match1(_IW_LINK_RE["ssid"], text)
        bssid = _match1(_IW_LINK_RE["bssid"], text)
        freq = _match1_int(_IW_LINK_RE["freq"], text)
        signal = _match1_int(_IW_LINK_RE["signal"], text)
        rx_mbit = _match1_float(_IW_LINK_RE["rx_bitrate"], text)
        return WifiSnapshot(
            ts_ms=ts_ms,
            ssid=ssid,
            bssid=bssid,
            rssi=signal,
            channel=_freq_to_channel(freq) if freq is not None else None,
            link_rate_kbps=int(rx_mbit * 1000) if rx_mbit is not None else None,
            associated=True,
        )

    async def _iw_event_loop(self) -> None:
        cmd = f"iw event -f -t"
        while not self._stop.is_set():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *shlex.split(cmd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                return
            assert proc.stdout is not None
            try:
                while not self._stop.is_set():
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace").strip()
                    if decoded:
                        log.debug("wifi.iw_event", line=decoded)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            await asyncio.sleep(1.0)

    def stop(self) -> None:
        self._stop.set()


def _match1(rx: re.Pattern[str], text: str) -> str | None:
    m = rx.search(text)
    return m.group(1).strip() if m else None


def _match1_int(rx: re.Pattern[str], text: str) -> int | None:
    s = _match1(rx, text)
    return int(s) if s is not None else None


def _match1_float(rx: re.Pattern[str], text: str) -> float | None:
    s = _match1(rx, text)
    return float(s) if s is not None else None


def _dbm_from_mbm(mbm: object) -> int | None:
    if mbm is None:
        return None
    try:
        return int(int(mbm) / 100)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _freq_to_channel(freq_mhz: object) -> int | None:
    if freq_mhz is None:
        return None
    try:
        f = int(freq_mhz)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if f == 2484:
        return 14
    if 5180 <= f <= 5825:
        return (f - 5000) // 5
    if 5955 <= f <= 7115:
        return (f - 5950) // 5
    return None


async def run_wifi_monitor(conn, bus) -> None:
    await WifiMonitor(conn, bus).run()
