#!/bin/bash
# Host-side helper. Reads `cmd\tpayload\n` lines from the FIFO and maps the
# allowlisted command to a systemctl invocation. NOTE: bash, not sh — uses
# $'\t' ANSI-C quoting for IFS, which dash does not support.
# allowlisted command to a systemctl/nmcli invocation. Anything else is ignored.

set -u
FIFO="/run/pingwatch-host.fifo"

[ -p "$FIFO" ] || mkfifo -m 0620 "$FIFO"

log() { logger -t pingwatch-host-helper "$*"; }

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
        log "update_check"
        (cd /opt/pingwatch && git pull --ff-only && /usr/bin/docker compose -f /opt/pingwatch/docker/docker-compose.yml build) || true
        /bin/systemctl restart pingwatch.service || true
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
        ssid=$(nmcli -t -f NAME,TYPE,DEVICE c show --active 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1; exit}')
        link=$(iw dev wlan0 link 2>/dev/null || true)
        python3 - "$ssid" "$link" <<'PY' > /run/pingwatch-shared/wifi-status.json
import json, time, sys, re
ssid = sys.argv[1] if len(sys.argv) > 1 else ''
link = sys.argv[2] if len(sys.argv) > 2 else ''
m_sig = re.search(r'signal:\s*(-?\d+)', link)
m_bit = re.search(r'tx bitrate:\s*([\d.]+)', link)
m_freq = re.search(r'freq:\s*(\d+)', link)
rssi = int(m_sig.group(1)) if m_sig else None
# RSSI -> percent (rough): -50 dBm = 100%, -100 dBm = 0%
pct = None
if rssi is not None:
  pct = max(0, min(100, int(2 * (rssi + 100))))
print(json.dumps({
  'ts_ms': int(time.time()*1000),
  'connected': bool(ssid),
  'ssid': ssid or None,
  'signal_pct': pct,
  'rssi_dbm': rssi,
  'bitrate_mbps': float(m_bit.group(1)) if m_bit else None,
  'freq': int(m_freq.group(1)) if m_freq else None,
}))
PY
        chmod 644 /run/pingwatch-shared/wifi-status.json || true
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
      *)
        log "ignoring unknown cmd: $cmd"
        ;;
    esac
  done < "$FIFO"
done
