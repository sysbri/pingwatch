# PingWatch

Ein dauerhafter Netzwerk-Monitor fuer einen Raspberry Pi mit HDMI-Display. PingWatch ueberwacht ein WLAN rund um die Uhr, klassifiziert jeden Aussetzer (EINZEL / MULTI / UPLINK / WLAN / STREAM) und zeigt das Ergebnis im Kiosk-Modus auf dem angeschlossenen Bildschirm.

## Quick Start (Fresh Raspberry Pi)

1. Flash Raspberry Pi OS Bookworm 64-bit (Lite) auf SD-Karte (Raspberry Pi Imager, im Imager Hostname/SSH/WLAN vorkonfigurieren).
2. Boot Pi mit Ethernet oder pre-configured WiFi.
3. SSH ein und ausfuehren:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/sysbri/pingwatch/main/install.sh | sudo bash
   ```

4. Pi rebooten (`sudo reboot`) — das Dashboard erscheint nach 30–45 s auf HDMI.

Der Installer zieht das Repo nach `/opt/pingwatch`, baut das Docker-Image, legt den Kiosk-User an, konfiguriert udev/sysctl/systemd und startet den Container. Manuelle Installation ohne `curl | bash` siehe [INSTALL.md](INSTALL.md).

## Features

- **4-Ziele-Monitor** — Gateway, Cloudflare (1.1.1.1), Google (8.8.8.8) und heise.de werden parallel per ICMP/TCP/HTTP/DNS gepingt. Ziele sind im UI editierbar.
- **Live-Stream-Test** — kontinuierlicher Throughput-Strom gegen `speed.cloudflare.com` zur Erkennung von Stream-Drops unabhaengig vom Ping-Verlust.
- **Outage-Klassifizierung** — EINZEL/MULTI/UPLINK/WLAN/STREAM-Tagging mit N=2/K=2-Logik und 2 s-MULTI-Korrelationsfenster.
- **WLAN-Wechsel-UI** — SSID/BSSID/RSSI/Kanal-Telemetrie bei 1 Hz; Wechsel auf ein anderes WLAN direkt aus dem Dashboard.
- **Speedtest** — On-Demand-Throughput-Messung gegen Cloudflare, Resultat in der Historie.
- **HDMI-Kiosk-Dashboard** — Fullscreen Chromium unter Cage/Wayland, Auto-Login, kein Desktop, keine Anmeldung noetig.
- **USB-Export** — FAT32/exFAT-Stick einstecken, Export-Button druecken, ZIP mit DB + CSV + JSON wird auf den Stick geschrieben.

## Hardware

- Raspberry Pi 4 (4 GB) oder Raspberry Pi 5 (2 GB minimum, 4 GB empfohlen).
- HDMI-Display (1080p ideal) + Micro-HDMI-Kabel.
- WLAN-Verbindung zum zu ueberwachenden Netz.
- Optional: NVMe-HAT (ab ~30 Tagen Laufzeit sinnvoll), UPS-HAT, FAT32/exFAT-USB-Stick fuer Exporte.

## Software-Stack

- **Backend**: Python 3.12, FastAPI, aiosqlite, uvicorn/uvloop, pyroute2.
- **Frontend**: Alpine.js + Chart.js, server-rendered mit WebSocket-Updates.
- **Persistenz**: SQLite (WAL-Mode), stuendliche Online-Backups.
- **Runtime**: Docker (network_mode: host, non-root UID 10001).
- **Kiosk**: Cage (Wayland) + Chromium auf tty1.

## Konfiguration

Alle Einstellungen — Ziele, Schwellenwerte, Intervalle, WLAN-Wechsel, Reboot, Factory-Reset, Update-Check — sind ueber das Settings-Overlay im Dashboard (Zahnrad oben rechts) editierbar. USB-Maus anstecken reicht. Die Konfiguration wird in `config.yaml` im benannten Volume `pingwatch-data` persistiert und ohne Container-Restart uebernommen.

## Updates

```bash
sudo /opt/pingwatch/install.sh
```

Der Installer ist idempotent: er macht `git pull --ff-only` in `/opt/pingwatch`, baut das Image neu falls noetig und startet die Services. Alternativ via UI: Einstellungen → System & Diagnose → "Update pruefen".

## Troubleshooting

| Problem | Diagnose / Fix |
|---|---|
| HDMI bleibt schwarz nach Reboot | `sudo systemctl --user --machine pingwatch@.host status pingwatch-kiosk`. Meistens wartet der Kiosk noch auf den Container. `docker logs pingwatch --tail 200` zeigt den Grund. |
| Container restart loop | `sudo systemctl status pingwatch.service` und `docker logs pingwatch`. Haeufige Ursachen: korrupte DB nach unsauberem Stromausfall (`PRAGMA integrity_check`), fehlender sysctl-Eintrag. |
| WLAN-Wechsel nicht moeglich / Telemetrie leer | Interface heisst nicht `wlan0` — pruefen mit `ip link`, ggf. `PINGWATCH_WLAN_IF` in `docker/docker-compose.yml` anpassen. Test im Container: `docker exec pingwatch iw dev wlan0 link`. |
| Pings schlagen alle fehl (RTT immer null) | `sysctl net.ipv4.ping_group_range` muss `0 2147483647` zurueckgeben. Falls nicht: `sudo sysctl --system` und Container neu starten. |
| USB-Stick wird nicht erkannt | `findmnt /media/pingwatch-usb` muss eine Zeile liefern. Falls nicht: `sudo udevadm control --reload` und Stick neu einstecken. |

Weitere Details und das vollstaendige Troubleshooting-Kapitel: siehe [INSTALL.md](INSTALL.md).

## Entwicklung

```bash
make dev      # uvicorn --reload auf dem Host
make test     # pytest -q
make lint     # ruff check + mypy
make build    # docker compose build
make up       # docker compose up -d
make down     # docker compose down
make logs     # docker logs -f
```

## License

MIT — siehe [LICENSE](LICENSE).
