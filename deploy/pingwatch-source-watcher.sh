#!/bin/bash
# Watches the static + templates dirs and reloads the kiosk Chromium
# whenever a file changes. Debounces bursts (rsync writes many files in
# rapid succession) by waiting 1.5 s after the LAST event before reloading.
#
# Runs as root via pingwatch-source-watcher.service; invokes user-scope
# systemctl as the pingwatch user (uid 1002).

set -euo pipefail

WATCH_DIRS=(
  "/opt/pingwatch/src/pingwatch/static"
  "/opt/pingwatch/src/pingwatch/templates"
)

PW_USER="pingwatch"
PW_UID="$(id -u "$PW_USER")"
DEBOUNCE_S="1.5"

log() {
  echo "[$(date +'%H:%M:%S')] $*" | logger -t pingwatch-source-watcher -s
}

reload_kiosk() {
  log "Source changed -> reloading kiosk Chromium"
  # Clear Chromium HTTP cache so fresh CSS/JS is fetched
  rm -rf "/home/${PW_USER}/.cache/pingwatch-chromium-cache/"* 2>/dev/null || true
  sudo -u "$PW_USER" \
    XDG_RUNTIME_DIR="/run/user/${PW_UID}" \
    systemctl --user restart pingwatch-kiosk.service \
    && log "kiosk reloaded" \
    || log "kiosk reload FAILED"
}

log "Watching: ${WATCH_DIRS[*]}"

# inotifywait outputs one line per event; we read with a timeout to
# implement debouncing: when no event for $DEBOUNCE_S seconds after the
# last burst, trigger reload.
inotifywait -m -r -q \
  -e modify,create,delete,move,attrib \
  --format '%T %w%f' --timefmt '%H:%M:%S' \
  "${WATCH_DIRS[@]}" |
while true; do
  # Block-wait for first event
  read -r first || break
  log "first event: $first"
  # Drain bursts; reset the timer whenever a new event comes in
  while read -r -t "$DEBOUNCE_S" more; do
    : # ignore; just absorb the burst
  done
  reload_kiosk
done
