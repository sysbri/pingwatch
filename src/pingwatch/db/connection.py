from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 134217728",
    "PRAGMA cache_size = -20000",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA wal_autocheckpoint = 4000",
    "PRAGMA auto_vacuum = INCREMENTAL",
)

DEFAULT_SETTINGS: tuple[tuple[str, str, str], ...] = (
    ("outage.N_consec_loss", "2", "int"),
    ("outage.K_consec_ok", "2", "int"),
    ("spike.absolute_threshold_us", "80000", "int"),
    ("spike.p50_multiplier", "3.0", "float"),
    ("multi.window_ms", "2000", "int"),
    ("uplink.min_externals", "2", "int"),
    ("wlan.reassoc_min_duration_ms", "2000", "int"),
    ("stream.enabled", "true", "bool"),
    ("stream.endpoint", "https://nbg1-speed.hetzner.com/10GB.bin", "string"),
    ("stream.target_kbps", "20", "int"),
    ("stream.daily_cap_mb", "2048", "int"),
    ("stream.zero_kbps_min_samples", "1", "int"),
    ("speedtest.provider", "cloudflare", "string"),
    ("speedtest.iperf3_server", "", "string"),
    ("speedtest.iperf3_port", "5201", "int"),
    ("heartbeat.enabled", "true", "bool"),
    ("heartbeat.target", "1.1.1.1:443", "string"),
    ("heartbeat.interval_ms", "200", "int"),
    ("heartbeat.miss_threshold_ms", "800", "int"),
    ("flaky.loss_threshold_pct", "1.0", "float"),
    ("flaky.window_hours", "24", "int"),
    ("trace.interval_seconds", "300", "int"),
    ("trace.on_outage_after_losses", "3", "int"),
    ("trace.max_hops", "30", "int"),
    ("retention.raw_pings_days", "7", "int"),
    ("retention.hourly_aggregates_days", "90", "int"),
    ("retention.daily_aggregates_days", "730", "int"),
    ("retention.outages_days", "180", "int"),
    ("retention.traces_days", "30", "int"),
    ("retention.wifi_rssi_days", "7", "int"),
    ("retention.wifi_events_days", "180", "int"),
    ("retention.stream_samples_days", "7", "int"),
    ("retention.heartbeat_days", "30", "int"),
    ("retention.notifications_days", "30", "int"),
    ("wifi.interface", "wlan0", "string"),
    ("wifi.expected_ssid", "", "string"),
    ("wifi.rssi_warning_dbm", "-75", "int"),
    ("wifi.reassoc_counts_as_outage", "true", "bool"),
    ("ui.timezone", "Europe/Berlin", "string"),
    ("ui.language", "de", "string"),
    ("ui.theme", "dark", "string"),
    ("ui.default_time_range", "24h", "string"),
    ("ui.dashboard_refresh_ms", "2000", "int"),
    ("ui.large_mode", "false", "bool"),
    ("ui.toast_on_outage", "true", "bool"),
    ("ui.beep_on_outage", "false", "bool"),
    ("device.name", "Pi 5", "string"),
)

DEFAULT_DESTINATIONS: tuple[tuple[str, str, str, str, int], ...] = (
    ("Gateway", "192.168.1.1", "ICMP", "gateway", 0),
    ("Cloudflare", "1.1.1.1", "ICMP", "external", 1),
    ("Google DNS", "8.8.8.8", "ICMP", "external", 2),
    ("heise.de", "heise.de", "ICMP", "external", 3),
)


async def _apply_schema(db: aiosqlite.Connection) -> None:
    schema = SCHEMA_FILE.read_text()
    await db.executescript(schema)
    await db.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at_ts_ms) VALUES (?, ?)",
        (1, int(time.time() * 1000)),
    )
    await db.commit()


async def _seed_defaults(db: aiosqlite.Connection) -> None:
    now_ms = int(time.time() * 1000)
    for key, value, value_type in DEFAULT_SETTINGS:
        await db.execute(
            """
            INSERT OR IGNORE INTO settings(key, value, value_type, updated_at_ts_ms)
            VALUES (?, ?, ?, ?)
            """,
            (key, value, value_type, now_ms),
        )

    # Seed destinations only if table is empty (idempotent first-run setup).
    cur = await db.execute("SELECT COUNT(*) FROM destinations")
    row = await cur.fetchone()
    if row and row[0] == 0:
        for name, address, type_, kind, ordering in DEFAULT_DESTINATIONS:
            await db.execute(
                """
                INSERT INTO destinations(name, address, type, kind, ordering)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, address, type_, kind, ordering),
            )

    await db.commit()


class DB:
    """Thin wrapper around an aiosqlite connection with PRAGMA setup + schema init."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> aiosqlite.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        for pragma in PRAGMAS:
            await conn.execute(pragma)
        await _apply_schema(conn)
        await _seed_defaults(conn)
        self._conn = conn
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DB not opened; call open() first")
        return self._conn


@asynccontextmanager
async def open_db(path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    db = DB(path)
    try:
        conn = await db.open()
        yield conn
    finally:
        await db.close()
