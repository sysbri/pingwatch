/* PingWatch — Alpine root component.
 *
 * Holds reactive state for all three screens. Establishes WebSocket connections
 * with exponential backoff, and exposes helpers that the templates call from
 * `@click`/`x-text`/`x-show`.
 */

function fitFrame() {
  const w = document.getElementById('wrap');
  const f = document.getElementById('frame');
  if (!w || !f) return;
  const sw = w.clientWidth / 1920;
  const sh = w.clientHeight / 1080;
  const s = Math.min(sw, sh);
  f.style.transform = 'scale(' + s + ')';
  f.style.transformOrigin = 'top left';
  f.style.zoom = '';
  // Globaler Scale-Wert fuer Chart.js Event-Kompensation.
  window.__pwScale = s;
}
window.addEventListener('resize', fitFrame);
window.addEventListener('load', fitFrame);

function pingwatch() {
  return {
    // ---------- top-level UI ----------
    currentScreen: 'dashboard',
    settings_sub: 'allgemein',
    clockText: '--:--:--',

    // ---------- domain state ----------
    dashboard: { hero: {}, stream: {}, wifi: {}, cards: [], timeline: [], events: [], worst_trace: null },
    detail: {
      destId: null,
      key: null,
      name: '',
      address: '',
      status: 'ok',
      statusLabel: '',
      tab: 'uebersicht',
      range: '24h',
      overview: null,
      trace: null,
      traces: [],
      tracesChangedOnly: false,
      selectedTraceId: null,
      hopTrends: null,
      outages: [],
      filterTypes: [],
      filterSearch: '',
      selectedOutageId: null,
      selectedOutage: null,
      livePings: [],
      rawPings: [],
      pingStatuses: [],
      pingSearch: '',
      wifi_overview: null,
      wifiMetric: 'rssi',
    },
    settings: {},
    targets: [],
    newTarget: { name: '', address: '', type: 'ICMP', kind: 'external', interval_ms: 1000, timeout_ms: 2000, enabled: true, ordering: 0 },
    system: {},
    update: { current_sha: null, remote_sha: null, behind: null, branch: null, ts_ms: null, checking: false, installing: false, phase: null },
    exportRange: '24h',
    streamRange: '1h',
    streamSeries: [],
    streamDrops: [],
    exportBusy: null,
    usbAvailable: null,
    toast: null,
    speedtest: { status: 'idle', last: null, error: null, elapsed: 0, history: [], _poll: null, _t0: 0 },
    wifi: {
      status: null,           // { connected, ssid, signal_pct, rssi_dbm, bitrate_mbps, freq }
      networks: [],
      channelUsage: [],       // [{ channel, band, count, pct }] for the env scan
      sorted: [],             // networks sorted by signal (env scan)
      scanTs: null,
      scanning: false,
      connecting: null,       // ssid wenn gerade ein Connect laeuft
      lastError: null,
      showConnectModal: false,
      modalSsid: '',
      modalSecurity: '',
      modalPassword: '',
    },

    // ---------- bootstrap ----------
    async boot() {
      fitFrame();
      this.startClock();
      await Promise.all([this.fetchDashboard(), this.loadSettings(), this.loadTargets()]);
      this.loadSpeedtestHistory();
      this.connectDashboardWS();
      this.connectEventsWS();
      this.startSettingsRefresh();
      this.loadStreamSeries();
      // Keep the stream chart fresh on its selected window (only while the
      // dashboard is visible — the canvas doesn't exist on other screens).
      setInterval(() => {
        if (this.currentScreen === 'dashboard') this.loadStreamSeries();
      }, 10000);
      // Theme + large-mode watcher: keep the .pw root in sync with settings.
      this.applyUiAttrs();
      this.$watch('settings', () => this.applyUiAttrs());
      // Always start on the dashboard. A previous Settings visit can leave a
      // "#settings/..." hash in the URL; clear it so reloading the page never
      // dumps you back into Settings -- a refresh should land "home".
      if (window.location.hash) {
        try {
          history.replaceState(null, '', window.location.pathname + window.location.search);
        } catch (e) { /* */ }
      }
    },

    applyUiAttrs() {
      try {
        const root = document.querySelector('.pw');
        if (!root) return;
        const theme = this.settings && this.settings['ui.theme'];
        if (theme) root.setAttribute('data-theme', theme);
        else root.removeAttribute('data-theme');
        const large = !!(this.settings && this.settings['ui.large_mode']);
        if (large) root.setAttribute('data-mode', 'large');
        else root.removeAttribute('data-mode');
      } catch (e) { /* */ }
    },

    startClock() {
      const tick = () => {
        const d = new Date();
        const pad = (n) => String(n).padStart(2, '0');
        this.clockText = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
      };
      tick();
      setInterval(tick, 1000);
    },

    startSettingsRefresh() {
      // Settings rarely change, but they CAN change from another device or via
      // the API. Re-fetch once a minute so display labels (e.g. the stream
      // target on the dashboard) stay current without a manual kiosk reload.
      // Skip while the Settings screen is open: the inputs bind to
      // `settings[...]` via x-model, so a background refresh would clobber
      // half-typed, unsaved input.
      setInterval(() => {
        if (this.currentScreen !== 'settings') this.loadSettings();
      }, 60000);
    },

    // ---------- navigation ----------
    goto(screen, ctx) {
      this.currentScreen = screen;
      if (screen === 'detail') {
        this.openDestinationByKey(ctx);
      } else if (screen === 'settings') {
        if (ctx) this.switchSettingsSub(ctx);
      }
      window.scrollTo(0, 0);
    },

    switchTab(name) {
      this.detail.tab = name;
      if (this.detail.key === 'wifi') { this.loadWifiOverview(); return; }
      if (name === 'uebersicht') this.loadOverview();
      if (name === 'aussetzer') this.loadOutages();
      if (name === 'trace') this.loadTraces();
      if (name === 'roh') this.loadRawPings();
    },

    switchSettingsSub(name) {
      // Ensure we land on the settings screen even when invoked from outside
      // the settings context (e.g. a sidebar click before the screen swap).
      // We intentionally do NOT write the URL hash: a reload should always go
      // to the dashboard, not back into the last-open settings sub-screen.
      this.currentScreen = 'settings';
      this.settings_sub = name;
      if (name === 'system') { this.loadSystem(); this.loadUpdateStatus(); }
      if (name === 'wlan') this.loadWifiStatus();
    },

    setRange(r) {
      this.detail.range = r;
      if (this.detail.key === 'wifi') { this.loadWifiOverview(); return; }
      this.loadOverview();
    },

    setWifiMetric(m) {
      this.detail.wifiMetric = m;
      this.$nextTick(() => this._renderRssiChart());
    },

    // ---------- dashboard ----------
    async fetchDashboard() {
      try {
        const res = await fetch('/api/dashboard');
        if (res.ok) this.dashboard = await res.json();
      } catch (e) { console.warn('dashboard fetch failed', e); }
    },

    // The stream chart is driven by its own range-aware series (1h/12h/24h),
    // independent of the live dashboard payload, so the WebSocket tick can keep
    // updating the numbers without clobbering the selected window.
    async loadStreamSeries() {
      try {
        const r = await fetch('/api/stream/series?range=' + encodeURIComponent(this.streamRange));
        if (r.ok) {
          const d = await r.json();
          this.streamSeries = d.series || [];
          this.streamDrops = d.drops || [];
        }
      } catch (e) { /* */ }
      this.$nextTick(() => this._renderStreamChart());
    },

    setStreamRange(r) {
      if (this.streamRange === r) return;
      this.streamRange = r;
      this.loadStreamSeries();
    },

    _renderStreamChart() {
      const sc = document.getElementById('stream-chart-canvas');
      if (!sc || !window.PingWatchCharts) return;
      try { this._streamChart && this._streamChart.destroy && this._streamChart.destroy(); } catch (e) { /* */ }
      this._streamChart = window.PingWatchCharts.streamThroughputChart(
        sc,
        this.streamSeries || [],
        this.streamDrops || [],
      );
    },

    connectDashboardWS() {
      this._connect('/ws/dashboard', (msg) => {
        if (msg && msg.hero) this.dashboard = msg;
      });
    },

    connectEventsWS() {
      this._connect('/ws/events', (msg) => {
        if (!msg || !msg.payload) return;
        if (msg.topic === 'outages.new') {
          this.dashboard.events = [msg.payload, ...(this.dashboard.events || [])].slice(0, 50);
          if (this.settings['ui.toast_on_outage']) this.showToast('Neuer Aussetzer: ' + (msg.payload.type || ''));
        } else {
          this.dashboard.events = [msg.payload, ...(this.dashboard.events || [])].slice(0, 50);
        }
      });
    },

    _connect(url, onMsg) {
      const fullUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + url;
      let backoff = 1000;
      const connect = () => {
        const ws = new WebSocket(fullUrl);
        ws.onmessage = (ev) => {
          try { onMsg(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
          backoff = 1000;
        };
        ws.onclose = () => setTimeout(connect, backoff = Math.min(backoff * 2, 30000));
        ws.onerror = () => ws.close();
      };
      connect();
    },

    // ---------- detail ----------
    openDestinationByKey(key) {
      const target = (this.targets || []).find((t) => this.makeKey(t.name) === key);
      if (target) {
        this.detail.destId = target.id;
        this.detail.key = key;
        this.detail.name = target.name;
        this.detail.address = target.address;
      } else if (key === 'stream') {
        this.detail.destId = null;
        this.detail.key = 'stream';
        this.detail.name = 'Live-Stream';
        this.detail.address = this.settings['stream.endpoint'] || '—';
      } else if (key === 'wifi') {
        this.detail.destId = null;
        this.detail.key = 'wifi';
        this.detail.name = 'WLAN-Verbindung';
        this.detail.address = (this.dashboard.wifi && this.dashboard.wifi.ssid) || '—';
      } else if ((this.targets || []).length > 0) {
        const t = this.targets[0];
        this.detail.destId = t.id;
        this.detail.key = this.makeKey(t.name);
        this.detail.name = t.name;
        this.detail.address = t.address;
      }
      this.switchTab(this.detail.tab || 'uebersicht');
      this.connectRawPingsWS();
    },

    async loadOverview() {
      if (!this.detail.destId) return;
      try {
        const r = await fetch('/api/destinations/' + this.detail.destId + '/overview?range=' + this.detail.range);
        if (r.ok) this.detail.overview = await r.json();
        // FIX 2: load current traceroute for the "Aktuelle Route" panel.
        try {
          const traceRes = await fetch('/api/destinations/' + this.detail.destId + '/trace/latest');
          if (traceRes.ok) this.detail.trace = await traceRes.json();
        } catch (e) { /* */ }
      } catch (e) { /* */ }
      // FIX 1: render latency chart canvas with data + outage/spike markers.
      this.$nextTick(() => this._renderLatencyChart());
    },

    _renderLatencyChart() {
      const lc = document.getElementById('detail-latency-canvas');
      if (!lc || !window.PingWatchCharts || !this.detail.overview) return;
      try {
        if (this._latChart && this._latChart.destroy) this._latChart.destroy();
      } catch (e) { /* */ }
      this._latChart = null;
      const init = () => {
        this._latChart = window.PingWatchCharts.latencyChart(
          lc,
          this.detail.overview.series || [],
          this.detail.overview.outage_markers || [],
          this.detail.overview.spike_markers || [],
        );
      };
      if (lc.clientWidth === 0 || lc.clientHeight === 0) {
        requestAnimationFrame(() => requestAnimationFrame(init));
      } else {
        init();
      }
    },

    async loadWifiOverview() {
      try {
        const r = await fetch('/api/wifi/overview?range=' + this.detail.range);
        if (r.ok) this.detail.wifi_overview = await r.json();
      } catch (e) { console.warn('wifi overview fetch failed', e); }
      this.$nextTick(() => this._renderRssiChart());
    },

    _renderRssiChart() {
      const cv = document.getElementById('detail-rssi-canvas');
      if (!cv || !window.PingWatchCharts || !this.detail.wifi_overview) return;
      try {
        if (this._rssiChart && this._rssiChart.destroy) this._rssiChart.destroy();
      } catch (e) { /* */ }
      this._rssiChart = null;
      const init = () => {
        const fn = (window.PingWatchCharts.rssiChart || window.PingWatchCharts.latencyChart);
        const ov = this.detail.wifi_overview;
        if (this.detail.wifiMetric === 'link') {
          const pts = (ov.link_series || []).map((p) => ({ ts_ms: p.ts_ms, rssi: Math.round(p.link_rate_kbps / 100) / 10 }));
          this._rssiChart = fn(cv, pts, { label: 'Link-Speed', color: '#34d399', bg: 'rgba(52,211,153,0.10)', unit: 'MBit/s', yMin: 0, yMax: null });
        } else {
          this._rssiChart = fn(cv, ov.series || []);
        }
      };
      if (cv.clientWidth === 0 || cv.clientHeight === 0) {
        requestAnimationFrame(() => requestAnimationFrame(init));
      } else {
        init();
      }
    },

    async loadOutages() {
      if (!this.detail.destId) return;
      const types = this.detail.filterTypes.join(',');
      const url = '/api/outages?dest_id=' + this.detail.destId
        + (types ? '&types=' + types : '')
        + (this.detail.filterSearch ? '&search=' + encodeURIComponent(this.detail.filterSearch) : '')
        + '&limit=200';
      try {
        const r = await fetch(url);
        if (r.ok) {
          const data = await r.json();
          this.detail.outages = data.items || [];
        }
      } catch (e) { /* */ }
    },

    async selectOutage(id) {
      this.detail.selectedOutageId = id;
      const r = await fetch('/api/outages/' + id);
      if (r.ok) this.detail.selectedOutage = await r.json();
      // FIX 1: render ±30s context chart for the selected outage.
      this.$nextTick(() => this._renderOutageContextChart());
    },

    _renderOutageContextChart() {
      const oc = document.getElementById('outage-context-canvas');
      if (!oc || !window.PingWatchCharts) return;
      const series = this.detail.selectedOutage && this.detail.selectedOutage.context_series;
      if (!series) return;
      try { this._outageChart && this._outageChart.destroy && this._outageChart.destroy(); } catch (e) { /* */ }
      this._outageChart = window.PingWatchCharts.latencyChart(oc, series, [], []);
    },

    toggleFilter(t) {
      const i = this.detail.filterTypes.indexOf(t);
      if (i < 0) this.detail.filterTypes.push(t); else this.detail.filterTypes.splice(i, 1);
      this.loadOutages();
    },

    async loadTraces() {
      if (!this.detail.destId) return;
      const url = '/api/destinations/' + this.detail.destId + '/traces?limit=100&changed_only=' + this.detail.tracesChangedOnly;
      try {
        const r = await fetch(url);
        if (r.ok) {
          const d = await r.json();
          this.detail.traces = d.items || [];
        }
        const rt = await fetch('/api/destinations/' + this.detail.destId + '/hop-trends');
        if (rt.ok) this.detail.hopTrends = await rt.json();
      } catch (e) { /* */ }
    },

    selectTrace(id) { this.detail.selectedTraceId = id; },

    togglePingStatus(s) {
      const i = this.detail.pingStatuses.indexOf(s);
      if (i < 0) this.detail.pingStatuses.push(s); else this.detail.pingStatuses.splice(i, 1);
      this.loadRawPings();
    },

    async loadRawPings() {
      if (!this.detail.destId) return;
      const statuses = this.detail.pingStatuses.join(',');
      const url = '/api/destinations/' + this.detail.destId + '/pings?limit=200'
        + (statuses ? '&statuses=' + statuses : '')
        + (this.detail.pingSearch ? '&search=' + encodeURIComponent(this.detail.pingSearch) : '');
      try {
        const r = await fetch(url);
        if (r.ok) {
          const d = await r.json();
          this.detail.rawPings = d.items || [];
        }
      } catch (e) { /* */ }
    },

    connectRawPingsWS() {
      if (this._pingWs) { try { this._pingWs.close(); } catch (e) { /* */ } this._pingWs = null; }
      if (!this.detail.destId) return;
      const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host
        + '/ws/raw-pings?dest_id=' + this.detail.destId;
      const ws = new WebSocket(url);
      this._pingWs = ws;
      ws.onmessage = (ev) => {
        try {
          const sample = JSON.parse(ev.data);
          if (sample.type === 'ping') return;
          this.detail.livePings = [sample, ...this.detail.livePings].slice(0, 200);
        } catch (e) { /* */ }
      };
      ws.onclose = () => { this._pingWs = null; };
    },

    triggerTrace() { fetch('/api/destinations/' + this.detail.destId + '/trace', { method: 'POST' }).catch(() => {}); },

    exportDetail(fmt) { this.exportData(fmt); },

    // ---------- settings ----------
    async loadSettings() {
      try {
        const r = await fetch('/api/settings');
        if (r.ok) this.settings = await r.json();
      } catch (e) { /* */ }
      this.applyUiAttrs && this.applyUiAttrs();
    },

    async saveSettings() {
      try {
        const r = await fetch('/api/settings', {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(this.settings),
        });
        if (r.ok) this.showToast('Einstellungen gespeichert');
      } catch (e) { /* */ }
      this.applyUiAttrs && this.applyUiAttrs();
    },

    async loadTargets() {
      try {
        const r = await fetch('/api/targets');
        if (r.ok) this.targets = await r.json();
      } catch (e) { /* */ }
    },

    async setTraceDest(value) {
      // Speichert dashboard.trace_dest_id (id oder 'auto') und laedt Dashboard neu.
      try {
        await fetch('/api/settings', {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ 'dashboard.trace_dest_id': value }),
        });
        this.settings['dashboard.trace_dest_id'] = value;
        await this.fetchDashboard();
      } catch (e) { /* */ }
    },

    async createTarget() {
      try {
        const r = await fetch('/api/targets', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(this.newTarget),
        });
        if (r.ok) {
          this.newTarget = { name: '', address: '', type: 'ICMP', kind: 'external', interval_ms: 1000, timeout_ms: 2000, enabled: true, ordering: 0 };
          await this.loadTargets();
        }
      } catch (e) { /* */ }
    },

    async deleteTarget(id) {
      // Confirm is handled inline at the call-site for the new card UI; keep
      // the defensive prompt for any other invocation path.
      if (arguments.length < 2 && !confirm('Ziel löschen?')) return;
      await fetch('/api/targets/' + id, { method: 'DELETE' });
      await this.loadTargets();
    },

    // FIX 12: inline-edit support for the redesigned Ziele-Karten.
    async updateTarget(id, fields) {
      try {
        const r = await fetch('/api/targets/' + id, {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(fields),
        });
        if (r.ok) await this.loadTargets();
        else this.showToast('Aktualisierung fehlgeschlagen');
      } catch (e) { this.showToast('Aktualisierung fehlgeschlagen'); }
    },

    async testTarget(id) {
      const r = await fetch('/api/targets/' + id + '/test', { method: 'POST' });
      if (r.ok) {
        const result = await r.json();
        this.showToast(result.success ? ('OK: ' + (result.latency_us / 1000).toFixed(1) + ' ms') : ('Fail: ' + (result.error_kind || '?')));
      }
    },

    // ---------- system ----------
    async loadSystem() {
      try {
        const r = await fetch('/api/system');
        if (r.ok) this.system = await r.json();
      } catch (e) { /* */ }
    },

    async loadUpdateStatus() {
      try {
        const r = await fetch('/api/system/update-status');
        if (r.ok) this.update = { ...this.update, ...(await r.json()), checking: false };
      } catch (e) { /* */ }
    },
    async checkUpdate() {
      this.update.checking = true;
      this.showToast('Suche nach Updates…');
      try {
        await fetch('/api/system/check-update', { method: 'POST' });
        // host-helper does a git fetch (~1-3 s); poll the status briefly.
        await new Promise((res) => setTimeout(res, 3000));
        await this.loadUpdateStatus();
        if (this.update.behind > 0) this.showToast('Update verfügbar (' + this.update.behind + ')');
        else if (this.update.behind === 0) this.showToast('Bereits aktuell');
        else this.showToast('Status nicht verfügbar');
      } catch (e) { this.showToast('Prüfung fehlgeschlagen'); }
      this.update.checking = false;
    },
    async installUpdate() {
      if (!confirm('Update installieren? Der Pi lädt, baut und startet neu (~1–3 Min).')) return;
      let started = false;
      try {
        const r = await fetch('/api/system/update', { method: 'POST' });
        const d = r.ok ? await r.json() : null;
        started = !!(d && d.ok);
      } catch (e) { /* */ }
      if (!started) { this.showToast('Update-Start fehlgeschlagen (Host-Helper nicht erreichbar)'); return; }
      this.update.installing = true;
      this.update.phase = 'gestartet';
      this.showToast('Update gestartet…');
      // Poll the detached runner's progress. The app restarts mid-update,
      // so fetch errors are expected — keep polling through them.
      for (let i = 0; i < 100; i++) {
        await new Promise((res) => setTimeout(res, 3000));
        try {
          const pr = await fetch('/api/system/update-result');
          if (!pr.ok) continue;
          const p = await pr.json();
          if (p.phase) this.update.phase = p.detail ? (p.phase + ' — ' + p.detail) : p.phase;
          if (p.phase === 'done') {
            this.update.installing = false;
            this.showToast('Update abgeschlossen');
            await this.checkUpdate();
            return;
          }
          if (p.phase === 'failed') {
            this.update.installing = false;
            this.showToast('Update fehlgeschlagen: ' + (p.detail || 'siehe Log'));
            return;
          }
        } catch (e) { /* app restarting */ }
      }
      this.update.installing = false;
      this.showToast('Update-Status unklar — bitte "Nach Updates suchen" klicken');
    },

    async downloadDiagnose() { window.location = '/api/system/diagnose-bundle'; },
    async clearData() {
      if (!confirm('Alle Metriken löschen?')) return;
      await fetch('/api/system/clear-data', { method: 'POST' });
      this.showToast('Daten gelöscht');
    },
    async restartSystem() {
      if (!confirm('System neu starten?')) return;
      await fetch('/api/system/restart', { method: 'POST' });
    },
    async factoryReset() {
      if (!confirm('Werksreset — alle Daten verloren. Fortfahren?')) return;
      await fetch('/api/system/factory-reset?confirm=yes', { method: 'POST' });
    },

    // ---------- export ----------
    async exportData(fmt) {
      if (this.exportBusy) return;
      this.exportBusy = fmt;
      const range = this.exportRange || '24h';
      const url = '/api/export/' + fmt + '?range=' + encodeURIComponent(range);
      const labels = { csv: 'CSV', json: 'JSON', zip: 'ZIP', 'db-snapshot': 'DB-Snapshot' };
      this.showToast((labels[fmt] || fmt) + '-Export wird vorbereitet…');
      try {
        const r = await fetch(url, { method: 'POST' });
        if (!r.ok) {
          const txt = await r.text().catch(() => '');
          this.showToast('Export fehlgeschlagen (' + r.status + ')' + (txt ? ': ' + txt.slice(0, 80) : ''));
          return;
        }
        const blob = await r.blob();
        // Filename aus Content-Disposition extrahieren, sonst Fallback.
        let filename = 'pingwatch-' + range + '-' + Date.now();
        const cd = r.headers.get('Content-Disposition') || '';
        const m = cd.match(/filename="?([^";]+)"?/i);
        if (m) filename = m[1];
        else if (fmt === 'csv') filename += '.csv';
        else if (fmt === 'json') filename += '.json';
        else if (fmt === 'zip') filename += '.zip';
        else if (fmt === 'db-snapshot') filename += '.sqlite';
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = filename;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(objUrl), 4000);
        this.showToast((labels[fmt] || fmt) + '-Export bereit: ' + filename);
      } catch (e) {
        this.showToast('Export fehlgeschlagen: ' + (e && e.message ? e.message : 'Netzwerkfehler'));
      } finally {
        this.exportBusy = null;
      }
    },

    // ---------- speedtest ----------
    async runSpeedtest() {
      if (this.speedtest.status === 'running') return;
      this.speedtest.status = 'running';
      this.speedtest.error = null;
      this.speedtest._t0 = Date.now();
      this.speedtest.elapsed = 0;
      try {
        const r = await fetch('/api/speedtest/run', { method: 'POST' });
        if (!r.ok) {
          let msg = 'HTTP ' + r.status;
          try { const j = await r.json(); if (j && (j.detail || j.error)) msg = j.detail || j.error; } catch (e) { /* */ }
          if (r.status === 409) msg = msg || 'Tages-Budget erschöpft';
          this.speedtest.status = 'failed';
          this.speedtest.error = msg;
          return;
        }
        this._startSpeedtestPoll();
      } catch (e) {
        this.speedtest.status = 'failed';
        this.speedtest.error = (e && e.message) || 'Netzwerkfehler';
      }
    },

    _startSpeedtestPoll() {
      if (this.speedtest._poll) { clearInterval(this.speedtest._poll); this.speedtest._poll = null; }
      this.speedtest._poll = setInterval(async () => {
        this.speedtest.elapsed = Math.round((Date.now() - this.speedtest._t0) / 1000);
        try {
          const r = await fetch('/api/speedtest/status');
          if (!r.ok) return;
          const d = await r.json();
          const st = d && d.status;
          if (st === 'done' || st === 'failed' || st === 'aborted') {
            clearInterval(this.speedtest._poll);
            this.speedtest._poll = null;
            this.speedtest.status = st;
            if (d.last) this.speedtest.last = d.last;
            if (st === 'failed' || st === 'aborted') {
              this.speedtest.error = (d.last && d.last.error) || (st === 'aborted' ? 'abgebrochen' : 'fehlgeschlagen');
            }
            this.loadSpeedtestHistory();
          }
        } catch (e) { /* */ }
      }, 1000);
    },

    // ---------- wifi (NetworkManager via host-helper) ----------
    async loadWifiStatus() {
      try {
        const r = await fetch('/api/wifi/status');
        if (r.ok) this.wifi.status = await r.json();
      } catch (e) { /* */ }
    },
    async openPortal() {
      try {
        const r = await fetch('/api/wifi/open-portal', { method: 'POST' });
        const d = r.ok ? await r.json() : null;
        if (d && d.ok) this.showToast('Portal-Seite öffnet auf dem Bildschirm — schließt nach 3 Min automatisch');
        else this.showToast('Portal öffnen fehlgeschlagen (Host-Helper nicht erreichbar)');
      } catch (e) { this.showToast('Portal öffnen fehlgeschlagen'); }
    },
    async scanNetworks() {
      if (this.wifi.scanning) return;
      this.wifi.scanning = true;
      this.wifi.lastError = null;
      try {
        const r = await fetch('/api/wifi/scan', { method: 'POST' });
        if (!r.ok) throw new Error('Scan fehlgeschlagen');
        const j = await r.json();
        this.wifi.networks = j.networks || [];
        this.wifi.scanTs = j.ts_ms || Date.now();
        this._computeWifiEnv();
      } catch (e) {
        this.wifi.lastError = String(e.message || e);
      } finally {
        this.wifi.scanning = false;
      }
    },

    apBand(freq) {
      if (!freq) return '';
      if (freq < 2500) return '2.4';
      if (freq < 5925) return '5';
      return '6';
    },

    // Derive the env-scan views (channel occupancy + signal-sorted list) once
    // per scan so the templates don't recompute on every render.
    _computeWifiEnv() {
      const nets = this.wifi.networks || [];
      const byCh = {};
      for (const n of nets) {
        const ch = n.channel || 0;
        if (!ch) continue;
        if (!byCh[ch]) byCh[ch] = { channel: ch, band: this.apBand(n.freq), count: 0 };
        byCh[ch].count++;
      }
      const usage = Object.values(byCh).sort((a, b) => b.count - a.count || a.channel - b.channel);
      const max = usage.reduce((m, c) => Math.max(m, c.count), 1);
      usage.forEach((c) => { c.pct = Math.round((c.count / max) * 100); });
      this.wifi.channelUsage = usage;
      this.wifi.sorted = [...nets].sort((a, b) => (b.signal || 0) - (a.signal || 0));
    },
    openConnectModal(net) {
      this.wifi.modalSsid = net.ssid;
      this.wifi.modalSecurity = net.security || '';
      this.wifi.modalPassword = '';
      this.wifi.showConnectModal = true;
      this.wifi.lastError = null;
    },
    closeConnectModal() {
      this.wifi.showConnectModal = false;
      this.wifi.modalSsid = '';
      this.wifi.modalPassword = '';
    },
    async connectWifi() {
      if (this.wifi.connecting) return;
      const ssid = this.wifi.modalSsid;
      if (!ssid) return;
      this.wifi.connecting = ssid;
      this.wifi.lastError = null;
      try {
        const r = await fetch('/api/wifi/connect', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ ssid, password: this.wifi.modalPassword }),
        });
        const j = await r.json();
        if (r.ok && j.ok) {
          this.wifi.showConnectModal = false;
          this.wifi.modalPassword = '';
          // Status nach kurzer Wartezeit neu laden
          setTimeout(() => this.loadWifiStatus(), 1500);
        } else {
          this.wifi.lastError = j.message || 'Verbindung fehlgeschlagen';
        }
      } catch (e) {
        this.wifi.lastError = String(e.message || e);
      } finally {
        this.wifi.connecting = null;
      }
    },

    async loadSpeedtestHistory() {
      try {
        const r = await fetch('/api/speedtest/history?limit=10');
        if (!r.ok) return;
        const d = await r.json();
        this.speedtest.history = (d && d.items) || [];
        if (!this.speedtest.last) {
          const first = this.speedtest.history.find(x => x && x.status === 'done');
          if (first) this.speedtest.last = first;
        }
      } catch (e) { /* */ }
    },

    fmtMbps(v) {
      if (v === null || v === undefined) return '–';
      const n = Number(v);
      if (!isFinite(n)) return '–';
      if (n >= 1000) return n.toFixed(0);
      return n.toFixed(1);
    },

    fmtAgo(ts) {
      if (!ts) return '';
      const s = (Date.now() - Number(ts)) / 1000;
      if (s < 60) return Math.max(0, Math.round(s)) + 's her';
      if (s < 3600) return Math.round(s / 60) + 'min her';
      return Math.round(s / 3600) + 'h her';
    },

    async exportUSB() {
      if (this.exportBusy) return;
      this.exportBusy = 'usb';
      const range = this.exportRange || '24h';
      this.showToast('USB-Export wird vorbereitet…');
      try {
        const r = await fetch('/api/export/usb?range=' + encodeURIComponent(range) + '&format=zip', { method: 'POST' });
        if (!r.ok) {
          this.showToast('USB-Export fehlgeschlagen (' + r.status + ')');
          this.usbAvailable = false;
          return;
        }
        const d = await r.json();
        if (d.ok) {
          this.usbAvailable = true;
          this.showToast('Auf USB geschrieben: ' + (d.detail || 'OK'));
        } else {
          this.usbAvailable = false;
          this.showToast('USB nicht verfügbar: ' + (d.detail || 'kein Stick erkannt'));
        }
      } catch (e) {
        this.usbAvailable = false;
        this.showToast('USB-Export fehlgeschlagen: ' + (e && e.message ? e.message : 'Netzwerkfehler'));
      } finally {
        this.exportBusy = null;
      }
    },

    // ---------- formatting helpers ----------
    formatTime(ts) {
      if (!ts) return '--:--:--';
      const d = new Date(ts);
      const pad = (n) => String(n).padStart(2, '0');
      return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    },
    formatDate(ts) {
      if (!ts) return '';
      const d = new Date(ts);
      return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' });
    },
    formatDuration(ms) {
      ms = +ms || 0;
      if (ms < 1000) return ms + ' ms';
      if (ms < 60_000) return (ms / 1000).toFixed(1) + ' s';
      if (ms < 3_600_000) return Math.floor(ms / 60_000) + 'm ' + Math.floor((ms % 60_000) / 1000) + 's';
      return Math.floor(ms / 3_600_000) + 'h ' + Math.floor((ms % 3_600_000) / 60_000) + 'm';
    },
    formatLatency(us) {
      if (us == null || us === 0) return '—';
      return (us / 1000).toFixed(1);
    },
    formatLossPct(p) { return (p == null ? '0.0' : (+p).toFixed(2)); },
    formatNumber(n) {
      if (n == null) return '0';
      return (+n).toLocaleString('de-DE');
    },
    formatBytes(b) {
      if (!b) return '0 B';
      const k = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0;
      while (b >= 1024 && i < k.length - 1) { b /= 1024; i++; }
      return b.toFixed(1) + ' ' + k[i];
    },

    cardKey(card) { return this.makeKey(card.name); },
    cardKeyByName(name) { return this.makeKey(name); },
    makeKey(name) {
      if (!name) return 'unknown';
      const n = name.toLowerCase();
      if (n.indexOf('google') >= 0) return 'google';
      if (n.indexOf('cloud') >= 0) return 'cloudflare';
      if (n.indexOf('gateway') >= 0) return 'gateway';
      if (n.indexOf('heise') >= 0) return 'heise';
      return name.replace(/[^a-z0-9]/gi, '').toLowerCase();
    },

    destNameById(id) {
      const t = (this.targets || []).find((x) => x.id === id);
      return t ? t.name : ('Dest ' + id);
    },

    sparkHeight(v, all) {
      if (!all || !all.length) return '2px';
      const max = Math.max(...all, 1);
      return Math.max(2, Math.round((v / max) * 24)) + 'px';
    },

    gapStyle(gap) {
      // FIX 5: enforce a minimum visible width so single-ping gaps render as a
      // discernible marker on the timeline bar. Accept either pct-based
      // (start_pct/width_pct) or ms-based (start_ms/end_ms) inputs.
      if (gap && (gap.start_pct != null || gap.width_pct != null)) {
        const left = Math.max(0, Math.min(100, +gap.start_pct || 0));
        const width = Math.max(0.4, Math.min(100 - left, +gap.width_pct || 0.4));
        return 'left:' + left.toFixed(2) + '%;width:' + width.toFixed(2) + '%';
      }
      const now = Date.now();
      const start = +gap.start_ms;
      const end = +(gap.end_ms || now);
      const dayMs = 24 * 3_600_000;
      const left = Math.max(0, Math.min(100, ((start - (now - dayMs)) / dayMs) * 100));
      const width = Math.max(0.4, Math.min(100 - left, ((end - start) / dayMs) * 100));
      return 'left:' + left.toFixed(2) + '%;width:' + width.toFixed(2) + '%';
    },

    // FIX 6: helper for hop sparkline bars (trace tab).
    sparkBarHeight(v, all) {
      if (!all || !all.length) return '2px';
      const max = Math.max(...all, 1);
      return Math.max(2, Math.round(((+v || 0) / max) * 22)) + 'px';
    },

    rowClass(ev) {
      if (ev.source === 'wifi') return 'wifi';
      if (ev.source === 'stream') return 'stream';
      if (ev.event_type === 'UPLINK') return 'uplink';
      return '';
    },
    tagClass(ev) {
      const t = (ev.event_type || '').toLowerCase();
      if (t === 'multi') return 'multi';
      if (t === 'uplink') return 'uplink';
      if (ev.source === 'wifi') return 'wifi';
      if (ev.source === 'stream') return 'stream';
      return '';
    },
    tagLabel(ev) { return (ev.event_type || ev.source || '').slice(0, 6).toUpperCase(); },
    eventLabel(ev) {
      if (ev.source === 'outage') return (ev.dest_name || 'unbekannt') + ' <small>· ' + (ev.lost_count || 0) + ' verloren</small>';
      if (ev.source === 'wifi') return 'WLAN ' + (ev.event_type || '') + ' <small>· ' + (ev.ssid || '') + '</small>';
      if (ev.source === 'stream') return 'Stream ' + (ev.event_type || '');
      return ev.source || '';
    },
    openEvent(ev) {
      if (ev.source === 'outage') {
        this.goto('detail', this.cardKeyByName(ev.dest_name || ''));
        this.switchTab('aussetzer');
        if (ev.id) this.selectOutage(ev.id);
      }
    },
    hopClass(hop) {
      if (hop.is_suspect) return 'suspect';
      const rtt_ms = (hop.rtt_us || 0) / 1000;
      if (rtt_ms > 50) return 'warn';
      return '';
    },

    percBarWidth(v, all) {
      if (!all) return '0%';
      const max = Math.max(...Object.values(all), 1);
      return ((v / max) * 100).toFixed(0) + '%';
    },
    histBinHeight(c, all) {
      // sqrt-Skala: ein dominanter Bucket (z.B. 282 Pings) druckt sonst alle
      // anderen optisch komplett platt. sqrt zieht die kleinen Werte hoch
      // ohne die Reihenfolge zu aendern.
      const max = Math.sqrt(Math.max(...(all || []), 1));
      const scaled = Math.sqrt(Math.max(0, c));
      const pxMax = 80;
      const px = c === 0 ? 1 : Math.max(3, Math.round((scaled / max) * pxMax));
      return px + 'px';
    },

    showToast(text) {
      this.toast = text;
      clearTimeout(this._toastT);
      this._toastT = setTimeout(() => { this.toast = null; }, 3500);
    },
  };
}
