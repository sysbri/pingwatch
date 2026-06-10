#!/bin/bash
# Host-side helper. Reads `cmd\tpayload\n` lines from the FIFO and maps the
# allowlisted command to a systemctl invocation. NOTE: bash, not sh — uses
# $'\t' ANSI-C quoting for IFS, which dash does not support.
# allowlisted command to a systemctl/nmcli invocation. Anything else is ignored.

set -u
FIFO="/run/pingwatch-host.fifo"

[ -p "$FIFO" ] || mkfifo -m 0620 "$FIFO"

log() { logger -t pingwatch-host-helper "$*"; }

WLAN_IF="${PINGWATCH_WLAN_IF:-wlan0}"
SHARED_DIR="/run/pingwatch-shared"
PW_SRC="${PINGWATCH_SRC:-/opt/pingwatch/src}"

resolve_wlan_if() {
  # USB-bevorzugt; Fallback = konfiguriertes wlan0. Reiner stdlib-Aufruf.
  PYTHONPATH="$PW_SRC" python3 -m pingwatch.netif \
    --sysfs /sys/class/net --fallback "$WLAN_IF" 2>/dev/null \
    || echo "$WLAN_IF"
}

iface_label() {
  # "usb", wenn das Interface auf dem USB-Bus sitzt, sonst "intern".
  if [ -e "/sys/class/net/$1/device" ] && \
     readlink -f "/sys/class/net/$1/device" 2>/dev/null | grep -q "/usb"; then
    echo "usb"
  else
    echo "intern"
  fi
}

get_security() {
  # Security of the in-use AP (e.g. WPA2). Best-effort; nmcli only.
  nmcli -t -f IN-USE,SECURITY dev wifi 2>/dev/null \
    | awk -F: '$1=="*"{print $2; exit}'
}

write_wifi_status() {
  # Render the current association to wifi-status.json. Everything but the
  # security string comes from a single `iw link` call. Written to a unique
  # temp file and atomically renamed so readers never see a partial JSON.
  local security="${1:-}"
  local wlan_if label link gw tmp
  wlan_if=$(resolve_wlan_if)
  label=$(iface_label "$wlan_if")
  link=$(iw dev "$wlan_if" link 2>/dev/null || true)
  # Default-Route-Gateway (niedrigste Metric zuerst) — der Container haelt
  # damit das Gateway-Ziel aktuell (gateway_sync).
  # Nur `via`-Routen liefern eine Gateway-IP; `default dev ppp0` o.ae. wuerde
  # sonst den Devicenamen extrahieren.
  gw=$(ip route show default 2>/dev/null | awk '$1=="default" && $2=="via" {print $3; exit}')
  tmp=$(mktemp "${SHARED_DIR}/.wifi-status.XXXXXX" 2>/dev/null) || return 0
  python3 - "$link" "$security" "$wlan_if" "$label" "$gw" > "$tmp" <<'PY'
import json, time, sys, re
link = sys.argv[1] if len(sys.argv) > 1 else ''
security = sys.argv[2] if len(sys.argv) > 2 else ''
iface = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
label = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
gateway = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None
connected = 'Connected to' in link

def find(rx, cast=str):
  m = re.search(rx, link)
  if not m:
    return None
  try:
    return cast(m.group(1))
  except (TypeError, ValueError):
    return None

def freq_to_chan(f):
  if f is None: return None
  if 2412 <= f <= 2472: return (f - 2407) // 5
  if f == 2484: return 14
  if 5180 <= f <= 5825: return (f - 5000) // 5
  if 5955 <= f <= 7115: return (f - 5950) // 5
  return None

ssid = find(r'SSID:\s*(.+)')
rssi = find(r'signal:\s*(-?\d+)', int)
freq = find(r'freq:\s*(\d+)', int)
pct = max(0, min(100, int(2 * (rssi + 100)))) if rssi is not None else None
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
  'gateway_ip': gateway,
}))
PY
  chmod 644 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "${SHARED_DIR}/wifi-status.json" 2>/dev/null || rm -f "$tmp"
}

wifi_status_loop() {
  # ~1 Hz background refresh so the in-container monitor and the Live-Status UI
  # always have fresh association data, without anyone poking the FIFO. nmcli
  # (security) is heavier and rarely changes, so refresh it only every ~15 s.
  local i=0 security=""
  while true; do
    if [ $(( i % 15 )) -eq 0 ]; then
      security=$(get_security)
    fi
    i=$(( i + 1 ))
    write_wifi_status "$security"
    sleep 1
  done
}

mkdir -p "$SHARED_DIR" 2>/dev/null || true
wifi_status_loop &
WIFI_LOOP_PID=$!
trap 'kill "$WIFI_LOOP_PID" 2>/dev/null || true' EXIT

while true; do
  # Block-read one line at a time. The `cat` reopens the FIFO when writers close.
  while IFS=$'\t' read -r cmd payload; do
    log "received cmd=$cmd"
    case "$cmd" in
      reboot)
        log "rebooting (reason: $payload)"
        /bin/systemctl reboot
        ;;
      factory_reset)
        log "factory reset requested"
        # `down -v` removes the named volume by its compose-internal name,
        # so we don't depend on the project-prefix. The explicit volume rm
        # is a belt-and-suspenders fallback in case the volume outlives the
        # compose stack.
        /usr/bin/docker compose -f /opt/pingwatch/docker/docker-compose.yml down -v || true
        /usr/bin/docker volume rm pingwatch_pingwatch-data || true
        /usr/bin/docker volume rm pingwatch-data || true
        /bin/systemctl reboot
        ;;
      update_check)
        # Vollständiges Update (pull + idempotenter Installer + Restarts) im
        # ENTKOPPELTEN Runner (eigene transiente Unit): ueberlebt den
        # Helper-Neustart, blockiert den FIFO-Dispatch nicht und schreibt
        # Fortschritt nach update-result.json + /tmp/pingwatch-update.log.
        runner="/usr/local/bin/pingwatch-update"
        # Direkt nach einem git pull (vor dem ersten Installer-Lauf) liegt
        # der Runner evtl. nur im Repo.
        [ -x "$runner" ] || runner="/opt/pingwatch/deploy/pingwatch-update"
        log "update_check: starte detached runner $runner"
        rm -f "${SHARED_DIR}/update-result.json" 2>/dev/null || true
        systemd-run --quiet --collect --unit pingwatch-update "$runner" 2>&1 \
          | logger -t pingwatch-host-helper || true
        ;;
      check_update)
        # Remote prüfen und update-status.json schreiben (von der UI gelesen).
        log "check_update"
        ( cd /opt/pingwatch \
            && git fetch --quiet origin 2>/dev/null \
            && cur=$(git rev-parse --short HEAD 2>/dev/null) \
            && rem=$(git rev-parse --short origin/main 2>/dev/null) \
            && behind=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0) \
            && branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) \
            && python3 -c "import json,time,sys; print(json.dumps({'ts_ms':int(time.time()*1000),'current_sha':sys.argv[1] or None,'remote_sha':sys.argv[2] or None,'behind':int(sys.argv[3] or 0),'branch':sys.argv[4] or None}))" \
                 "$cur" "$rem" "$behind" "$branch" > "${SHARED_DIR}/update-status.json" \
            && chmod 644 "${SHARED_DIR}/update-status.json" ) || true
        ;;
      restart_app)
        log "restart_app"
        /bin/systemctl restart pingwatch.service
        ;;
      wifi_scan)
        log "wifi_scan"
        nmcli dev wifi rescan 2>/dev/null || true
        sleep 2
        nmcli -t -f IN-USE,BSSID,SSID,SIGNAL,SECURITY,FREQ,CHAN dev wifi list 2>/dev/null > /tmp/pw-wifi-scan-raw.txt || true
        python3 - <<'PY' > /run/pingwatch-shared/wifi-scan.json
import json, time, re
nets = []
try:
  with open('/tmp/pw-wifi-scan-raw.txt') as f:
    for line in f:
      line = line.rstrip('\n')
      if not line:
        continue
      # nmcli -t escapes ':' inside fields as '\:'. Split on unescaped ':'.
      parts = re.split(r'(?<!\\):', line)
      parts = [p.replace('\\:', ':') for p in parts]
      if len(parts) < 7:
        continue
      iu, bssid, ssid, signal, sec, freq, chan = parts[:7]
      if not ssid:
        continue
      nets.append({
        'in_use': iu == '*',
        'bssid': bssid,
        'ssid': ssid,
        'signal': int(signal) if signal.isdigit() else 0,
        'security': sec,
        'freq': int(freq) if freq.isdigit() else 0,
        'channel': int(chan) if chan.isdigit() else 0,
      })
except FileNotFoundError:
  pass
nets.sort(key=lambda n: -n['signal'])
seen = set(); out = []
for n in nets:
  if n['ssid'] in seen:
    continue
  seen.add(n['ssid'])
  out.append(n)
print(json.dumps({'ts_ms': int(time.time()*1000), 'networks': out}))
PY
        chmod 644 /run/pingwatch-shared/wifi-scan.json || true
        rm -f /tmp/pw-wifi-scan-raw.txt
        ;;
      wifi_status)
        log "wifi_status"
        write_wifi_status "$(get_security)"
        ;;
      wifi_connect)
        # payload format: "SSID\tPASSWORD" (password may be empty for open WLAN)
        ssid=$(printf '%s' "$payload" | awk -F'\t' '{print $1}')
        pw=$(printf '%s' "$payload" | awk -F'\t' '{print $2}')
        log "wifi_connect ssid=$ssid pw=[REDACTED-${#pw}chars]"
        if [ -z "$pw" ]; then
          out=$(nmcli dev wifi connect "$ssid" 2>&1)
        else
          out=$(nmcli dev wifi connect "$ssid" password "$pw" 2>&1)
        fi
        rc=$?
        python3 - "$ssid" "$rc" "$out" <<'PY' > /run/pingwatch-shared/wifi-result.json
import json, time, sys
ssid = sys.argv[1]
rc = int(sys.argv[2])
out = sys.argv[3]
print(json.dumps({'ts_ms': int(time.time()*1000), 'ssid': ssid, 'ok': rc == 0, 'message': out[:500]}))
PY
        chmod 644 /run/pingwatch-shared/wifi-result.json || true
        ;;
      wifi_forget)
        log "wifi_forget name=$payload"
        nmcli c delete "$payload" 2>&1 | logger -t pingwatch-host-helper || true
        ;;
      open_portal)
        # payload = URL (erkannte Portal-Redirect-URL bzw. HTTP-Fallback).
        # Oeffnet ein temporaeres Chromium-Fenster im laufenden cage-Kiosk
        # (neues Toplevel liegt automatisch im Vordergrund). Schliesst sich
        # nach 3 Minuten selbst; vorher per Strg+W moeglich.
        url="$payload"
        case "$url" in
          http://*|https://*) : ;;
          *) log "open_portal: refusing non-http url"; url="" ;;
        esac
        if [ -n "$url" ]; then
          pw_uid=$(id -u pingwatch 2>/dev/null)
          wld=$(ls "/run/user/${pw_uid}/" 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -1)
          wld="${wld:-wayland-0}"
          log "open_portal url=$url display=$wld"
          # Eigene transiente Unit: RuntimeMaxSec uebernimmt das Auto-Close
          # (robust gegen Helper-Restarts, kein sleep-Timer noetig) und
          # close_portal kann die Unit gezielt stoppen. Alte Instanz vorher weg.
          systemctl stop pingwatch-portal.service 2>/dev/null || true
          pkill -u pingwatch -f "pingwatch-portal-chromium" 2>/dev/null || true
          systemd-run --quiet --collect --unit pingwatch-portal \
            --uid=pingwatch \
            --setenv=XDG_RUNTIME_DIR="/run/user/${pw_uid}" \
            --setenv=WAYLAND_DISPLAY="$wld" \
            --property=RuntimeMaxSec=180 \
            chromium --ozone-platform=wayland \
            --user-data-dir="/home/pingwatch/.cache/pingwatch-portal-chromium" \
            --app="$url" 2>&1 | logger -t pingwatch-host-helper || true
        fi
        ;;
      close_portal)
        log "close_portal"
        systemctl stop pingwatch-portal.service 2>/dev/null || true
        # Fallback fuer Instanzen, die noch vom alten sleep-Timer-Weg stammen.
        pkill -u pingwatch -f "pingwatch-portal-chromium" 2>/dev/null || true
        ;;
      wifi_prefer_stick)
        # payload = USB-WLAN-Interface-Name. Eigenes, fest ans Stick-Interface
        # gepinntes Profil "pingwatch-stick" mit niedrigerer Route-Metric —
        # der Stick gewinnt die Default-Route, wenn er verbindet. Das
        # Onboard-Profil wird NIE angefasst: `nmcli dev wifi connect` wuerde
        # das bestehende SSID-Profil auf den Stick UMZIEHEN, und wenn der
        # Stick dann versagt (z.B. Unterspannung), ist die Verbindung komplett
        # weg — eigenes Profil = onboard bleibt als Fallback verbunden.
        # Hinweis: nur fuer offene Netze (kein PSK im Profil).
        iface="$payload"
        ssid="$(nmcli -t -f IN-USE,SSID dev wifi 2>/dev/null | awk -F: '$1=="*"{print $2; exit}')"
        [ -z "$ssid" ] && ssid="${PINGWATCH_EXPECTED_SSID:-}"
        log "wifi_prefer_stick iface=$iface ssid=${ssid:-<none>}"
        if [ -n "$iface" ] && [ -n "$ssid" ]; then
          nmcli c delete "pingwatch-stick" 2>/dev/null || true
          nmcli c add type wifi ifname "$iface" con-name "pingwatch-stick" \
            ssid "$ssid" \
            ipv4.route-metric 50 ipv6.route-metric 50 \
            connection.autoconnect yes connection.autoconnect-retries 0 \
            2>&1 | logger -t pingwatch-host-helper || true
          nmcli c up "pingwatch-stick" 2>&1 | logger -t pingwatch-host-helper || true
        fi
        ;;
      *)
        log "ignoring unknown cmd: $cmd"
        ;;
    esac
  done < "$FIFO"
done
