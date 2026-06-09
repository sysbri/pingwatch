#!/usr/bin/env bash
# One-shot installer for PingWatch on a fresh Raspberry Pi OS Bookworm 64-bit Lite.
#
# Expected flow (preferred): /opt/pingwatch is a git clone of
# https://github.com/sysbri/pingwatch (placed there by the install.sh wrapper).
# Legacy fallback: /tmp/pingwatch.tar.gz is extracted into /opt/pingwatch.
#
# This script is idempotent — running it a second time updates the host config
# in place (used for upgrades after `git pull` in /opt/pingwatch).

set -euo pipefail

# ---- 0. Prereqs ----
[ "$EUID" -eq 0 ] || { echo "Run with sudo"; exit 1; }

DEPLOY_DIR="/opt/pingwatch/deploy"
APP_DIR="/opt/pingwatch"

apt-get update
apt-get install -y --no-install-recommends \
    curl ca-certificates git \
    chromium cage seatd xwayland \
    fonts-noto-core mesa-utils

# seatd is required for cage to acquire a seat on Wayland. Without it the
# HDMI screen stays black after boot.
systemctl enable --now seatd

# pingwatch.service depends on network-online.target, which is only reached
# if a wait-online helper is enabled. On Bookworm Lite NetworkManager is the
# default; on netplan/systemd-networkd setups the corresponding unit name
# differs. Enable whichever exists; absent units are silently skipped.
for unit in NetworkManager-wait-online.service systemd-networkd-wait-online.service; do
  if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
    systemctl enable "$unit" 2>/dev/null || true
  fi
done

# ---- 1. Docker ----
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# ---- 2. Sysctl for unprivileged ICMP ----
if [ -f "${DEPLOY_DIR}/10-pingwatch.conf" ]; then
  install -m 0644 "${DEPLOY_DIR}/10-pingwatch.conf" /etc/sysctl.d/10-pingwatch.conf
else
  cat >/etc/sysctl.d/10-pingwatch.conf <<'EOF'
net.ipv4.ping_group_range = 0 2147483647
EOF
fi
sysctl --system >/dev/null

# ---- 3. App tree ----
install -d -o root -g root "${APP_DIR}"

# Preferred path: ${APP_DIR} is already a git clone (placed by install.sh
# wrapper or manual `git clone`). In that case, do nothing — the wrapper is
# responsible for `git pull` on upgrades, this script just (re)applies host
# config and rebuilds the container image.
if [ -d "${APP_DIR}/.git" ]; then
  echo "[install] Using existing git checkout at ${APP_DIR}"
elif [ -f /tmp/pingwatch.tar.gz ]; then
  # Legacy fallback for offline / pre-git installs.
  echo "[install] Extracting legacy tarball /tmp/pingwatch.tar.gz"
  tar -xzf /tmp/pingwatch.tar.gz -C "${APP_DIR}"
elif [ -d "${DEPLOY_DIR}" ]; then
  # /opt/pingwatch was populated by some other means (rsync, manual copy).
  echo "[install] Using pre-populated ${APP_DIR}"
else
  cat >&2 <<'ERR'
ERROR: /opt/pingwatch is empty and no /tmp/pingwatch.tar.gz was found.

Use the one-liner installer instead:

    curl -fsSL https://raw.githubusercontent.com/sysbri/pingwatch/main/install.sh | sudo bash

That wrapper clones the repo into /opt/pingwatch and then runs this script.
ERR
  exit 2
fi

# Every subsequent `install` call below references files under ${DEPLOY_DIR}.
if [ ! -d "${DEPLOY_DIR}" ]; then
  echo "ERROR: ${DEPLOY_DIR} missing after population step — checkout is incomplete." >&2
  exit 2
fi

cd "${APP_DIR}"

# Build the container image locally before bringing the stack up. Idempotent:
# docker compose build is a no-op when nothing changed.
docker compose -f "${APP_DIR}/docker/docker-compose.yml" build

# ---- 4. Kiosk user ----
if ! id pingwatch >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G video,render,input,plugdev,dialout pingwatch
  passwd -d pingwatch
fi
loginctl enable-linger pingwatch

# ---- 5. Autologin tty1 ----
install -d /etc/systemd/system/getty@tty1.service.d
cat >/etc/systemd/system/getty@tty1.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin pingwatch --noclear %I $TERM
Type=simple
EOF

# ---- 6. wait-for-pingwatch helper ----
install -m 0755 "${DEPLOY_DIR}/wait-for-pingwatch" /usr/local/bin/wait-for-pingwatch

# ---- 7. Kiosk user-scope units ----
install -d -o pingwatch -g pingwatch /home/pingwatch/.config/systemd/user
install -m 0644 -o pingwatch -g pingwatch \
  "${DEPLOY_DIR}/pingwatch-kiosk.service" \
  /home/pingwatch/.config/systemd/user/pingwatch-kiosk.service
install -m 0644 -o pingwatch -g pingwatch \
  "${DEPLOY_DIR}/pingwatch-kiosk-restart.service" \
  /home/pingwatch/.config/systemd/user/pingwatch-kiosk-restart.service
install -m 0644 -o pingwatch -g pingwatch \
  "${DEPLOY_DIR}/pingwatch-kiosk-restart.timer" \
  /home/pingwatch/.config/systemd/user/pingwatch-kiosk-restart.timer

# ---- 8. USB udev rules ----
install -m 0755 "${DEPLOY_DIR}/pingwatch-usb-mount"   /usr/local/bin/pingwatch-usb-mount
install -m 0755 "${DEPLOY_DIR}/pingwatch-usb-umount"  /usr/local/bin/pingwatch-usb-umount
install -m 0644 "${DEPLOY_DIR}/99-pingwatch-usb.rules" /etc/udev/rules.d/99-pingwatch-usb.rules
# USB-WLAN-Stick auto-prefer (Plug-and-Play-Trigger für wifi_prefer_stick).
install -m 0644 "${DEPLOY_DIR}/99-pingwatch-wlan.rules" /etc/udev/rules.d/99-pingwatch-wlan.rules
install -m 0755 "${DEPLOY_DIR}/pingwatch-wlan-prefer"   /usr/local/bin/pingwatch-wlan-prefer
udevadm control --reload
mount --make-rshared /

# `mount --make-rshared /` is not persistent across reboots. Install a tiny
# systemd unit that re-applies it before docker.service starts, otherwise the
# container fails to come up after the next reboot with a mount-propagation
# error.
cat >/etc/systemd/system/pingwatch-rshared.service <<'EOF'
[Unit]
Description=Make / mount-propagation rshared for PingWatch USB bind-mount
DefaultDependencies=no
Before=docker.service
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/bin/mount --make-rshared /
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable pingwatch-rshared.service

# ---- 9. Host helper service (named pipe for reboot/factory-reset) ----
# MUST be installed and started BEFORE pingwatch.service: the compose stack
# bind-mounts /run/pingwatch-host.fifo into the container, so the FIFO file
# must already exist on the host at container start.
if [ -f "${DEPLOY_DIR}/pingwatch-host-helper.service" ]; then
  install -m 0755 "${DEPLOY_DIR}/pingwatch-host-helper.sh" /usr/local/bin/pingwatch-host-helper.sh
  install -m 0644 "${DEPLOY_DIR}/pingwatch-host-helper.service" /etc/systemd/system/pingwatch-host-helper.service
  systemctl daemon-reload
  systemctl enable --now pingwatch-host-helper.service
fi

# ---- 9b. Shared dir for host-helper result JSON files (wifi-*.json) ----
install -d -m 0755 /run/pingwatch-shared
# Re-create on every boot via tmpfiles.d (since /run is tmpfs)
install -m 0644 /dev/stdin /etc/tmpfiles.d/pingwatch.conf <<'TMPFILES'
d /run/pingwatch-shared 0755 root root -
TMPFILES

# ---- 9c. Source watcher (auto-reload kiosk on file changes) ----
if [ -f "${DEPLOY_DIR}/pingwatch-source-watcher.service" ]; then
  apt-get install -y --no-install-recommends inotify-tools
  install -m 0755 "${DEPLOY_DIR}/pingwatch-source-watcher.sh" /usr/local/bin/pingwatch-source-watcher.sh
  install -m 0644 "${DEPLOY_DIR}/pingwatch-source-watcher.service" /etc/systemd/system/pingwatch-source-watcher.service
  systemctl daemon-reload
  systemctl enable --now pingwatch-source-watcher.service
fi

# ---- 9d. External watchdog: verifies container + ping freshness ----
if [ -f "${DEPLOY_DIR}/pingwatch-watchdog.service" ]; then
  apt-get install -y --no-install-recommends sqlite3
  install -m 0755 "${DEPLOY_DIR}/pingwatch-watchdog.sh" /usr/local/bin/pingwatch-watchdog.sh
  install -m 0644 "${DEPLOY_DIR}/pingwatch-watchdog.service" /etc/systemd/system/pingwatch-watchdog.service
  install -m 0644 "${DEPLOY_DIR}/pingwatch-watchdog.timer" /etc/systemd/system/pingwatch-watchdog.timer
  install -d -m 0755 /var/lib/pingwatch-watchdog
  systemctl daemon-reload
  systemctl enable --now pingwatch-watchdog.timer
fi

# ---- 10. Compose system unit ----
install -m 0644 "${DEPLOY_DIR}/pingwatch.service" /etc/systemd/system/pingwatch.service
systemctl daemon-reload
systemctl enable pingwatch.service
# `restart` (instead of start) makes this idempotent: on upgrades the freshly
# rebuilt image is picked up immediately. On a first install restart == start.
systemctl restart pingwatch.service

# ---- 11. logind: never blank ----
install -d /etc/systemd/logind.conf.d
install -m 0644 "${DEPLOY_DIR}/no-idle.conf" /etc/systemd/logind.conf.d/no-idle.conf

# ---- 12. HDMI always-on ----
if [ -f /boot/firmware/config.txt ] && ! grep -q '^hdmi_blanking' /boot/firmware/config.txt; then
  echo 'hdmi_blanking=0' >>/boot/firmware/config.txt
fi

# ---- 13. Enable user units (after linger) ----
sudo -u pingwatch XDG_RUNTIME_DIR=/run/user/"$(id -u pingwatch)" \
  systemctl --user daemon-reload
sudo -u pingwatch XDG_RUNTIME_DIR=/run/user/"$(id -u pingwatch)" \
  systemctl --user enable pingwatch-kiosk.service pingwatch-kiosk-restart.timer

echo
echo "===================================================================="
echo "Installation done. PingWatch will start on next boot."
echo "After reboot, dashboard should appear on HDMI in ~30-45s."
echo "===================================================================="

# Skip interactive prompt when stdin is not a TTY (e.g. piped through curl).
if [ -t 0 ]; then
  read -r -p "Reboot now? (y/n) " REPLY || REPLY="n"
  case "${REPLY}" in
    y|Y|yes|YES) echo "Rebooting..."; systemctl reboot ;;
    *)           echo "Skipping reboot. Run 'sudo reboot' when ready." ;;
  esac
else
  echo "Run 'sudo reboot' to start PingWatch."
fi
