"""1 Hz WiFi monitor.

Each tick we read the current association (SSID, BSSID, RSSI, channel,
link-rate) and store it as a snapshot. Snapshots are diffed via
``wifi.events`` to produce discrete WifiEvents and RSSI samples are persisted.

Sampling source, in order of preference:

1. **Host-helper status file** (``/run/pingwatch-shared/wifi-status.json``).
   On the Pi the container cannot read the WLAN hardware itself (it has no
   privileged access to nl80211/``iw``), so the privileged host-helper samples
   the association ~1 Hz and writes it to a shared file we consume here. This
   is the only path that actually produces data on the appliance.
2. **In-process nl80211 / ``iw``** -- fallback for dev/CI/cloud environments
   that have no host-helper but might have direct interface access.

An ``iw event`` subprocess is launched in parallel as a supplementary signal --
its lines are parsed opportunistically; the snapshot diff remains the source of
truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

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
    status_file: str = "/run/pingwatch-shared/wifi-status.json"
    status_stale_after_s: float = 12.0


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
        self._status_path = Path(self._cfg.status_file)

    async def run(self) -> None:
        iw_event_task = asyncio.create_task(self._iw_event_loop())
        try:
            while not self._stop.is_set():
                try:
                    snapshot = await self._sample()
                except Exception:  # noqa: BLE001
                    log.exception("wifi.sample_error")
                    snapshot = None
                # ``None`` means "no fresh data this tick" (e.g. the host-helper
                # file is stale/missing). We skip rather than fabricate a
                # disconnect, so a brief host-helper hiccup can't spam events.
                if snapshot is not None:
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

    async def _sample(self) -> WifiSnapshot | None:
        ts_ms = int(time.time() * 1000)
        # Primary path on the Pi: the host-helper refreshes a shared status
        # file ~1 Hz. The container cannot read the WLAN hardware directly, so
        # this file is the only source that actually yields data here.
        if self._status_path.exists():
            return await asyncio.to_thread(self._sample_from_file, ts_ms)
        # Fallback for environments without the host-helper (dev/CI/cloud).
        if _HAS_NL80211:
            with contextlib.suppress(Exception):
                return await asyncio.to_thread(self._sample_nl80211, ts_ms)
        return await self._sample_iw(ts_ms)

    def _sample_from_file(self, ts_ms: int) -> WifiSnapshot | None:
        """Build a snapshot from the host-helper's shared status file.

        Returns ``None`` (skip this tick) when the file is missing, unparseable
        or stale -- a lagging host-helper must not look like a WLAN disconnect.
        A *fresh* ``connected: false`` file does yield a disassociated snapshot,
        so genuine disconnects are still detected and recorded.
        """
        try:
            st = self._status_path.stat()
            if time.time() - st.st_mtime > self._cfg.status_stale_after_s:
                return None
            data = json.loads(self._status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        file_ts = data.get("ts_ms")
        ts = int(file_ts) if isinstance(file_ts, (int, float)) else ts_ms
        if not data.get("connected"):
            return WifiSnapshot(
                ts_ms=ts,
                ssid=None,
                bssid=None,
                rssi=None,
                channel=None,
                link_rate_kbps=None,
                associated=False,
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
        )

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
        cmd = "iw event -f -t"
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
