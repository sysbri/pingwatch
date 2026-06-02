#!/bin/bash
# External watchdog. Belt-and-suspenders alongside docker's own healthcheck:
# - Verifies container is healthy
# - Verifies raw_pings are being written (queries the DB directly)
# - On N consecutive failures: restart container. On further failure: reboot.
#
# Runs every 60s via systemd timer.

set -uo pipefail

STATE_DIR="/var/lib/pingwatch-watchdog"
FAIL_COUNTER="${STATE_DIR}/fail_count"
DB_PATH="/var/lib/docker/volumes/pingwatch_pingwatch-data/_data/pingwatch.db"
HEALTHZ_URL="http://127.0.0.1:5000/healthz"

CONTAINER_RESTART_AFTER=3   # restart container after 3 consecutive failures
PI_REBOOT_AFTER=8           # reboot pi after 8 consecutive failures
PING_FRESHNESS_S=300        # raw_pings must be no older than 5 minutes

mkdir -p "$STATE_DIR"
[ -f "$FAIL_COUNTER" ] || echo 0 > "$FAIL_COUNTER"

log() { logger -t pingwatch-watchdog "$*"; echo "[$(date +%T)] $*" >&2; }

get_fails() { cat "$FAIL_COUNTER" 2>/dev/null || echo 0; }
set_fails() { echo "$1" > "$FAIL_COUNTER"; }

# 1. Container present + healthy?
status="$(docker inspect pingwatch --format '{{.State.Status}}' 2>/dev/null || echo missing)"
health="$(docker inspect pingwatch --format '{{.State.Health.Status}}' 2>/dev/null || echo none)"

if [ "$status" != "running" ]; then
  log "container status=$status (not running) -> docker compose up -d"
  ( cd /opt/pingwatch/docker && docker compose up -d ) 2>&1 | logger -t pingwatch-watchdog
  set_fails "$(( $(get_fails) + 1 ))"
  exit 0
fi

# 2. Healthz responsive?
http_code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$HEALTHZ_URL" 2>/dev/null || echo 000)"

# 3. raw_pings frisch?
ping_age_ok=true
if [ -r "$DB_PATH" ]; then
  latest_ts="$(sqlite3 "$DB_PATH" 'SELECT COALESCE(MAX(ts_ms),0) FROM raw_pings' 2>/dev/null || echo 0)"
  now_ms="$(( $(date +%s) * 1000 ))"
  age_ms=$(( now_ms - latest_ts ))
  age_s=$(( age_ms / 1000 ))
  if [ "$latest_ts" -eq 0 ] || [ "$age_s" -gt "$PING_FRESHNESS_S" ]; then
    ping_age_ok=false
    log "raw_pings stale: age=${age_s}s threshold=${PING_FRESHNESS_S}s"
  fi
fi

if [ "$http_code" = "200" ] && [ "$ping_age_ok" = true ]; then
  # all green
  if [ "$(get_fails)" != "0" ]; then
    log "recovered: healthz=200 pings fresh -> fail_count reset"
  fi
  set_fails 0
  exit 0
fi

# unhealthy — increment counter and decide
fails=$(( $(get_fails) + 1 ))
set_fails "$fails"
log "UNHEALTHY: http=$http_code ping_age_ok=$ping_age_ok (fail #$fails)"

if [ "$fails" -ge "$PI_REBOOT_AFTER" ]; then
  log "fail_count=$fails >= $PI_REBOOT_AFTER -> rebooting Pi"
  set_fails 0
  /bin/systemctl reboot
elif [ "$fails" -ge "$CONTAINER_RESTART_AFTER" ]; then
  log "fail_count=$fails >= $CONTAINER_RESTART_AFTER -> restarting container"
  docker restart pingwatch 2>&1 | logger -t pingwatch-watchdog
fi

exit 0
