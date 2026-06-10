-- PingWatch SQLite schema (v1)
-- Time convention: ts_ms = epoch milliseconds UTC INTEGER.
-- Latencies stored in microseconds (_us) as INTEGER. UI divides by 1000.

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at_ts_ms INTEGER NOT NULL
);

-- ===== Destinations =====
CREATE TABLE IF NOT EXISTS destinations (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  address       TEXT    NOT NULL,
  type          TEXT    NOT NULL CHECK(type IN ('ICMP','TCP','HTTP','DNS')),
  kind          TEXT    NOT NULL CHECK(kind IN ('gateway','external')) DEFAULT 'external',
  interval_ms   INTEGER NOT NULL DEFAULT 1000,
  timeout_ms    INTEGER NOT NULL DEFAULT 2000,
  port          INTEGER,
  enabled       INTEGER NOT NULL DEFAULT 1,
  ordering      INTEGER NOT NULL DEFAULT 0,
  resolved_ip   TEXT,
  resolved_at_ts_ms INTEGER,
  created_at_ts_ms  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)*1000)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_destinations_order ON destinations(ordering);
CREATE INDEX IF NOT EXISTS ix_destinations_enabled ON destinations(enabled, ordering);

-- ===== Raw pings (heaviest table) =====
CREATE TABLE IF NOT EXISTS raw_pings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_id    INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  ts_ms      INTEGER NOT NULL,
  success    INTEGER NOT NULL,
  latency_us INTEGER,
  ttl        INTEGER,
  sequence   INTEGER,
  error_kind TEXT,
  flags      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_raw_dest_ts ON raw_pings(dest_id, ts_ms DESC);
CREATE INDEX IF NOT EXISTS ix_raw_ts      ON raw_pings(ts_ms);
CREATE INDEX IF NOT EXISTS ix_raw_loss    ON raw_pings(dest_id, ts_ms) WHERE success = 0;
CREATE INDEX IF NOT EXISTS ix_raw_spike   ON raw_pings(dest_id, ts_ms) WHERE (flags & 1) = 1;

-- ===== Hourly aggregates =====
CREATE TABLE IF NOT EXISTS hourly_aggregates (
  dest_id      INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  hour_bucket  INTEGER NOT NULL,
  count_total  INTEGER NOT NULL,
  count_ok     INTEGER NOT NULL,
  count_spike  INTEGER NOT NULL DEFAULT 0,
  p50_us       INTEGER, p95_us INTEGER, p99_us INTEGER,
  min_us       INTEGER, max_us INTEGER, mean_us INTEGER,
  loss_pct     REAL    NOT NULL,
  jitter_us    INTEGER,
  outage_count INTEGER NOT NULL DEFAULT 0,
  outage_ms    INTEGER NOT NULL DEFAULT 0,
  finalized    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (dest_id, hour_bucket)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hourly_bucket ON hourly_aggregates(hour_bucket);

-- ===== Daily aggregates =====
CREATE TABLE IF NOT EXISTS daily_aggregates (
  dest_id      INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  day_bucket   INTEGER NOT NULL,
  count_total  INTEGER NOT NULL,
  count_ok     INTEGER NOT NULL,
  count_spike  INTEGER NOT NULL DEFAULT 0,
  p50_us INTEGER, p95_us INTEGER, p99_us INTEGER,
  min_us INTEGER, max_us INTEGER, mean_us INTEGER,
  loss_pct     REAL    NOT NULL,
  jitter_us    INTEGER,
  outage_count INTEGER NOT NULL DEFAULT 0,
  outage_ms    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (dest_id, day_bucket)
) WITHOUT ROWID;

-- ===== Traceroutes =====
CREATE TABLE IF NOT EXISTS traceroutes (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_id   INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  ts_ms     INTEGER NOT NULL,
  trigger   TEXT    NOT NULL CHECK(trigger IN ('scheduled','outage','manual')),
  success   INTEGER NOT NULL,
  hop_count INTEGER NOT NULL,
  signature_hash TEXT NOT NULL,
  route_changed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_trace_dest_time ON traceroutes(dest_id, ts_ms DESC);
CREATE INDEX IF NOT EXISTS ix_trace_changed   ON traceroutes(dest_id, ts_ms DESC) WHERE route_changed = 1;
CREATE INDEX IF NOT EXISTS ix_trace_trigger   ON traceroutes(dest_id, trigger, ts_ms DESC);

-- ===== Trace hops =====
CREATE TABLE IF NOT EXISTS trace_hops (
  trace_id   INTEGER NOT NULL REFERENCES traceroutes(id) ON DELETE CASCADE,
  hop_no     INTEGER NOT NULL,
  host       TEXT,
  ip         TEXT,
  rtt_us     INTEGER,
  rtt_min_us INTEGER, rtt_max_us INTEGER,
  loss_pct   REAL    NOT NULL DEFAULT 0,
  is_suspect INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (trace_id, hop_no)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hops_ip ON trace_hops(ip);

-- ===== Route signatures =====
CREATE TABLE IF NOT EXISTS route_signatures (
  dest_id          INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  signature_hash   TEXT    NOT NULL,
  signature_text   TEXT    NOT NULL,
  first_seen_ts_ms INTEGER NOT NULL,
  last_seen_ts_ms  INTEGER NOT NULL,
  seen_count       INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (dest_id, signature_hash)
) WITHOUT ROWID;

-- ===== Outages =====
CREATE TABLE IF NOT EXISTS outages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_id_primary INTEGER REFERENCES destinations(id) ON DELETE SET NULL,
  start_ts_ms     INTEGER NOT NULL,
  end_ts_ms       INTEGER,
  duration_ms     INTEGER,
  lost_count      INTEGER NOT NULL DEFAULT 0,
  type            TEXT    NOT NULL CHECK(type IN ('EINZEL','MULTI','UPLINK','WLAN','STREAM')),
  suspect_hop_no  INTEGER,
  trace_id        INTEGER REFERENCES traceroutes(id) ON DELETE SET NULL,
  notes           TEXT
);
CREATE INDEX IF NOT EXISTS ix_outages_open      ON outages(start_ts_ms) WHERE end_ts_ms IS NULL;
CREATE INDEX IF NOT EXISTS ix_outages_dest_time ON outages(dest_id_primary, start_ts_ms DESC);
CREATE INDEX IF NOT EXISTS ix_outages_type_time ON outages(type, start_ts_ms DESC);
CREATE INDEX IF NOT EXISTS ix_outages_time      ON outages(start_ts_ms DESC);

CREATE TABLE IF NOT EXISTS outage_members (
  outage_id   INTEGER NOT NULL REFERENCES outages(id) ON DELETE CASCADE,
  dest_id     INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  start_ts_ms INTEGER NOT NULL,
  end_ts_ms   INTEGER,
  lost_count  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (outage_id, dest_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_omembers_dest ON outage_members(dest_id, start_ts_ms DESC);

-- ===== WiFi =====
CREATE TABLE IF NOT EXISTS wifi_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms       INTEGER NOT NULL,
  event_type  TEXT    NOT NULL CHECK(event_type IN
              ('disconnect','connect','reassoc','channel_change','ssid_change','roam')),
  ssid        TEXT,
  bssid       TEXT,
  rssi        INTEGER,
  channel     INTEGER,
  duration_ms INTEGER,
  raw         TEXT
);
CREATE INDEX IF NOT EXISTS ix_wifi_time ON wifi_events(ts_ms DESC);
CREATE INDEX IF NOT EXISTS ix_wifi_disc ON wifi_events(ts_ms)
  WHERE event_type IN ('disconnect','reassoc');

CREATE TABLE IF NOT EXISTS wifi_rssi_samples (
  ts_ms          INTEGER PRIMARY KEY,
  rssi           INTEGER NOT NULL,
  channel        INTEGER,
  link_rate_kbps INTEGER,
  noise_dbm      INTEGER
) WITHOUT ROWID;

-- Antennen-/Quellen-Wechsel (interne vs. USB-WLAN). Additiv: wird auf
-- Bestands-DBs per CREATE TABLE IF NOT EXISTS automatisch angelegt.
CREATE TABLE IF NOT EXISTS wifi_source_switches (
  ts_ms   INTEGER NOT NULL,
  from_if TEXT,
  to_if   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wifi_src_sw_time ON wifi_source_switches(ts_ms DESC);

-- ===== Stream =====
CREATE TABLE IF NOT EXISTS stream_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms       INTEGER NOT NULL,
  event_type  TEXT    NOT NULL CHECK(event_type IN
              ('connect','reconnect','drop','throughput_low','throughput_recover')),
  duration_ms INTEGER,
  kbps_before INTEGER,
  kbps_after  INTEGER,
  reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_stream_time ON stream_events(ts_ms DESC);

CREATE TABLE IF NOT EXISTS stream_throughput_samples (
  ts_ms       INTEGER PRIMARY KEY,
  kbps        INTEGER NOT NULL,
  tcp_rtt_us  INTEGER,
  bytes_total INTEGER
) WITHOUT ROWID;

-- ===== Heartbeat (deviations only — not 200 ms samples) =====
CREATE TABLE IF NOT EXISTS heartbeat_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms        INTEGER NOT NULL,
  event_type   TEXT    NOT NULL CHECK(event_type IN ('loss','recover','drift')),
  duration_ms  INTEGER,
  missed_count INTEGER
);
CREATE INDEX IF NOT EXISTS ix_hb_time ON heartbeat_events(ts_ms DESC);

-- ===== Settings =====
CREATE TABLE IF NOT EXISTS settings (
  key              TEXT PRIMARY KEY,
  value            TEXT NOT NULL,
  value_type       TEXT NOT NULL CHECK(value_type IN ('int','float','bool','string','json')),
  updated_at_ts_ms INTEGER NOT NULL
);

-- ===== Notifications =====
CREATE TABLE IF NOT EXISTS notifications (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms         INTEGER NOT NULL,
  severity      TEXT    NOT NULL CHECK(severity IN ('info','warn','error')),
  target_kind   TEXT    NOT NULL,
  target_id     INTEGER,
  message       TEXT    NOT NULL,
  seen_at_ts_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_notif_unseen ON notifications(ts_ms DESC) WHERE seen_at_ts_ms IS NULL;
CREATE INDEX IF NOT EXISTS ix_notif_time   ON notifications(ts_ms DESC);

-- ===== Triggers (minimal) =====
CREATE TRIGGER IF NOT EXISTS trg_outages_close
AFTER UPDATE OF end_ts_ms ON outages
WHEN NEW.end_ts_ms IS NOT NULL AND OLD.end_ts_ms IS NULL
BEGIN
  UPDATE outages
     SET duration_ms = NEW.end_ts_ms - NEW.start_ts_ms
   WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_settings_touch
BEFORE UPDATE ON settings
BEGIN
  UPDATE settings SET updated_at_ts_ms = CAST(strftime('%s','now') AS INTEGER)*1000
   WHERE key = NEW.key;
END;

-- ===== Speedtests =====
CREATE TABLE IF NOT EXISTS speedtests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  down_mbps REAL, up_mbps REAL, latency_ms REAL, jitter_ms REAL,
  server TEXT NOT NULL DEFAULT 'speed.cloudflare.com',
  bytes_down INTEGER DEFAULT 0, bytes_up INTEGER DEFAULT 0,
  duration_ms INTEGER,
  status TEXT NOT NULL CHECK(status IN ('running','done','failed','aborted')),
  error TEXT, task_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_speedtests_ts ON speedtests(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_speedtests_status ON speedtests(status);
