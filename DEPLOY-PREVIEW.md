# PingWatch — Cloud-Vorschau (ohne Raspberry Pi)

Diese Anleitung deployt PingWatch als **öffentlich erreichbare Vorschau**, damit
du den Entwicklungsstand von unterwegs anschauen kannst, ohne Zugriff auf den
Pi. Es ist eine reine **UI-/Funktions-Vorschau** — kein Ersatz für den
Produktivbetrieb auf dem Pi.

## Was läuft — und was nicht

| Funktioniert in der Cloud | Braucht den echten Pi (bleibt leer/ohne Funktion) |
|---|---|
| Dashboard-UI, Charts, WebSocket-Updates | WLAN-Telemetrie (SSID/BSSID/RSSI/Kanal) |
| Internet-Pings (Cloudflare/Google/heise) | WLAN-Wechsel aus dem Dashboard |
| Stream-Test, Speedtest, Traceroute | USB-Export |
| Outage-Klassifizierung, Settings-Overlay | Reboot / Factory-Reset / Update (Host-FIFO) |
| | Pi-Metriken (CPU/Temperatur) |

> ⚠️ **Sicherheit:** Die Vorschau hat **kein Login**. Wer die URL kennt, sieht
> das Dashboard und kann Einstellungen ändern. Nimm einen schwer erratbaren
> Namen und schalte die Instanz wieder ab, wenn du fertig bist. Daten sind
> flüchtig (Reset bei jedem Deploy) — kein persistentes Volume konfiguriert.

Das produktive `docker/docker-compose.yml` läuft **nur** auf dem Pi (es bindet
`/run/pingwatch-host.fifo`, `/sys/class/net`, `/media`, `/opt/pingwatch/src`
ein). Für die Cloud nutzen wir dasselbe `docker/Dockerfile`, aber nur mit
Env-Variablen statt dieser Mounts.

---

## Option A — Render (am einfachsten, geht auch vom Handy)

1. Auf <https://dashboard.render.com> mit GitHub anmelden.
2. **New +** → **Blueprint** → Repo `sysbri/pingwatch` auswählen.
3. Render liest [`render.yaml`](render.yaml) → **Apply** → Build abwarten (~3–5 min).
4. Öffentliche URL: `https://<name>.onrender.com`.

Hinweise: Free-Instanzen schlafen nach ~15 min Inaktivität ein und brauchen
beim nächsten Aufruf ~30–60 s zum Aufwachen. Die Blueprint zeigt standardmäßig
auf den Branch `claude/optimistic-archimedes-tp6pa`.

## Option B — Fly.io (CLI, vom Rechner)

```bash
flyctl auth login
flyctl apps create pingwatch-preview-<suffix>   # Name muss global eindeutig sein
flyctl deploy -a pingwatch-preview-<suffix>
flyctl open                                      # https://<app>.fly.dev
```

Konfiguration liegt in [`fly.toml`](fly.toml).

## Option C — Lokal (Docker, zum Selber-Anschauen)

```bash
docker build -t pingwatch:preview -f docker/Dockerfile .
docker run --rm -p 5000:5000 \
  -e PINGWATCH_BIND_HOST=0.0.0.0 \
  -e PINGWATCH_BIND_PORT=5000 \
  -e PINGWATCH_DB=/data/pingwatch.db \
  -e PINGWATCH_CONFIG=/data/config.yaml \
  pingwatch:preview
# → http://localhost:5000
```

---

## Schneller Wegwerf-Link (temporär)

Für einen sofortigen Blick ohne Account kann ein Quick-Tunnel
(`cloudflared tunnel --url http://127.0.0.1:5000`) eine `*.trycloudflare.com`-URL
liefern. Diese lebt aber nur, solange der Tunnel-Prozess läuft, und ist nicht
für Dauerbetrieb gedacht — für „von unterwegs" nimm Option A oder B.
