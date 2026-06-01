# PingWatch — Installation

Fuer den Standard-Weg auf einem frischen Pi siehe den **Quick Start** in der [README](README.md):

```bash
curl -fsSL https://raw.githubusercontent.com/sysbri/pingwatch/main/install.sh | sudo bash
```

Dieses Dokument beschreibt den **manuellen Install-Flow** (ohne `curl | bash`), die einzelnen Phasen des Installers und das Troubleshooting bei der Erstinbetriebnahme.

## Hardware-Checkliste

**Pflicht:**

- Raspberry Pi 4 (4 GB) oder Pi 5 (2 GB minimum, 4 GB empfohlen — Chromium im Kiosk belegt 600–900 MB).
- USB-C-Netzteil 5 V / 5 A (offiziell oder gleichwertig). Unterspannung erzeugt reproduzierbare WLAN-Drops.
- microSD ab 16 GB, A1 oder besser (SanDisk Extreme, Samsung Pro Endurance, Kingston Industrial).
- HDMI-Display + passendes Kabel (1080p ideal).
- USB-Tastatur fuer die Erstinbetriebnahme.

**Empfohlen:** NVMe-HAT + SSD ab 30 Tagen Laufzeit, UPS-HAT, FAT32/exFAT-USB-Stick fuer Exporte, Gehaeuse mit aktiver Kuehlung.

## Schritt 1 — Pi OS flashen

Raspberry Pi Imager → Pi 5 → **Raspberry Pi OS Lite (64-bit)** (Bookworm). Erweiterte Optionen (Zahnrad):

| Feld | Wert |
|---|---|
| Hostname | `pingwatch` |
| Benutzer | `pi` (oder eigenen) |
| WLAN | SSID **des zu ueberwachenden Netzes**, PSK, Land `DE` |
| Locale | `Europe/Berlin`, Tastatur `de` |
| SSH | aktivieren, idealerweise mit Public Key |

Wichtig: kein Desktop-Image. PingWatch bringt seinen eigenen Kiosk mit.

## Schritt 2 — Erste Verbindung

SD-Karte rein, HDMI/Strom an, 60–90 s warten. IP per Router-DHCP oder am Pi mit `ip a show wlan0` ermitteln.

```bash
ssh pi@<pi-ip>
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

## Schritt 3 — Repo klonen und Installer starten

Manuelle Variante (statt `curl | bash`):

```bash
sudo apt install -y git
sudo install -d -o $USER -g $USER /opt/pingwatch
git clone https://github.com/sysbri/pingwatch.git /opt/pingwatch
cd /opt/pingwatch
sudo ./install.sh
```

Das ist exakt das, was die Quick-Start-`curl`-Variante macht — nur eben sichtbar.

## Schritt 4 — Was der Installer tut

`install.sh` ruft intern `deploy/install-pingwatch.sh` auf. Phasen:

1. **Sudo-Check** — bricht ab wenn nicht root.
2. **APT-Pakete** — `chromium-browser`, `cage`, `seatd`, `xwayland`, Schriften (~250 MB, 3–6 min).
3. **Docker** — Install via `get.docker.com` falls nicht vorhanden, Daemon enable.
4. **ICMP-Sysctl** — `net.ipv4.ping_group_range = 0 2147483647` nach `/etc/sysctl.d/10-pingwatch.conf` (damit UID 10001 unprivileged ICMP-Sockets oeffnen darf).
5. **App-Tree** — `/opt/pingwatch` vorhanden? Dann `docker compose build` (8–15 min beim ersten Mal — arm64-Wheels werden teils nativ kompiliert).
6. **Kiosk-User** — User `pingwatch` (UID 10001) ohne Passwort, `loginctl enable-linger`.
7. **Autologin tty1** — getty `--autologin pingwatch`.
8. **wait-for-pingwatch** — Helper-Script, das auf `/healthz` wartet.
9. **Kiosk-User-Units** — `pingwatch-kiosk.service` + Restart-Timer im User-Scope.
10. **USB-udev** — Auto-Mount-Helper + `99-pingwatch-usb.rules` + `mount --make-rshared /`.
11. **Compose-System-Unit** — `pingwatch.service` enable+start.
12. **Host-Helper** — Mini-Daemon liest `/run/pingwatch-host.fifo` fuer Reboot/Factory-Reset/Update aus dem UI.
13. **logind & HDMI** — kein Bildschirmschoner, `hdmi_blanking=0` in `/boot/firmware/config.txt`.
14. **User-Units aktivieren** — Kiosk-Service + Restart-Timer.

Am Ende:

```
====================================================================
PingWatch installed. Reboot now:  sudo reboot
After reboot, dashboard should appear on HDMI in ~30-45s.
====================================================================
```

## Schritt 5 — Reboot & Verifikation

```bash
sudo reboot
```

Erwartete Sequenz auf HDMI: 0–15 s Boot → 15–25 s Autologin als `pingwatch`, schwarzer Screen (Cage baut Wayland auf) → 25–45 s Chromium startet, Dashboard erscheint.

Health-Check vom Host:

```bash
curl -s http://127.0.0.1:5000/healthz
```

## Updates

Idempotenter Re-Run des Installers:

```bash
sudo /opt/pingwatch/install.sh
```

Macht intern `git pull --ff-only` in `/opt/pingwatch`, Rebuild des Images falls noetig, `systemctl restart pingwatch.service`. Alternativ aus dem UI: Einstellungen → System & Diagnose → "Update pruefen".

## Troubleshooting (Install)

| Symptom | Diagnose |
|---|---|
| Bildschirm bleibt schwarz | `sudo systemctl --user --machine pingwatch@.host status pingwatch-kiosk` und `docker logs pingwatch --tail 200`. |
| `docker compose build` haengt | arm64-Wheels werden nativ kompiliert (besonders `aiosqlite`, `uvloop`) — geduldig sein, 10+ min normal. |
| Container nicht oben | `sudo systemctl status pingwatch.service`, dann `docker logs pingwatch`. |
| Pings alle null | `sysctl net.ipv4.ping_group_range` muss `0 2147483647` enthalten. Falls nicht: `sudo sysctl --system` + Container-Restart. |
| WLAN-Telemetrie leer | Interface != `wlan0`? `ip link` pruefen, ggf. `PINGWATCH_WLAN_IF` in `docker/docker-compose.yml`. |
| USB-Stick nicht gemounted | `findmnt /media/pingwatch-usb` + `dmesg | tail -20` nach dem Einstecken; `sudo udevadm control --reload`. |
| Installer bricht in Phase X ab | `set -euo pipefail` — die Zeilennummer im Output zeigt die Stelle; meist fehlt Internet beim ersten `apt-get update`. |

## Deinstallation

```bash
sudo systemctl disable --now pingwatch.service pingwatch-host-helper.service
sudo docker compose -f /opt/pingwatch/docker/docker-compose.yml down
sudo docker volume rm pingwatch-data
sudo docker image rm pingwatch:local
sudo rm -rf /opt/pingwatch
sudo rm -f /etc/systemd/system/pingwatch*.service \
           /etc/udev/rules.d/99-pingwatch-usb.rules \
           /etc/sysctl.d/10-pingwatch.conf
sudo userdel -r pingwatch
sudo systemctl daemon-reload
sudo reboot
```

Nach dem Reboot ist der Pi wieder nacktes Bookworm-Lite.
