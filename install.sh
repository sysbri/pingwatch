#!/usr/bin/env bash
#
# PingWatch Quick-Install Wrapper
#
# Usage (on a fresh Raspberry Pi OS Bookworm 64-bit Lite):
#   curl -fsSL https://raw.githubusercontent.com/sysbri/pingwatch/main/install.sh | sudo bash
#
# This script clones the repo into /opt/pingwatch and hands off to the
# detailed installer at deploy/install-pingwatch.sh.

set -euo pipefail

GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
RESET='\033[0m'

REPO_URL="https://github.com/sysbri/pingwatch.git"
INSTALL_DIR="/opt/pingwatch"

log()  { printf "${GREEN}[pingwatch]${RESET} %s\n" "$*" >&2; }
warn() { printf "${YELLOW}[pingwatch]${RESET} %s\n" "$*" >&2; }
err()  { printf "${RED}[pingwatch]${RESET} %s\n" "$*" >&2; }

printf "${GREEN}========================================${RESET}\n" >&2
printf "${GREEN}        PingWatch Installer${RESET}\n" >&2
printf "${GREEN}========================================${RESET}\n" >&2

# 1. Root check
if [[ $EUID -ne 0 ]]; then
  err "This installer must run as root."
  err "Try:  curl -fsSL https://raw.githubusercontent.com/sysbri/pingwatch/main/install.sh | sudo bash"
  exit 1
fi

# 2. OS / package manager check
if ! command -v apt-get >/dev/null 2>&1; then
  err "apt-get not found. This installer supports Debian-based systems only"
  err "(tested on Raspberry Pi OS Bookworm 64-bit Lite)."
  exit 1
fi

# 3. Ensure git is installed
if ! command -v git >/dev/null 2>&1; then
  log "Installing git ..."
  apt-get update -y
  apt-get install -y git
fi

# 4. Handle existing install dir
if [[ -d "$INSTALL_DIR" ]]; then
  warn "$INSTALL_DIR already exists."
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    if [[ -t 0 ]]; then
      read -r -p "Update existing checkout via 'git pull'? [Y/n] " ans
      ans="${ans:-Y}"
      if [[ ! "$ans" =~ ^[Yy]$ ]]; then
        err "Aborted by user."
        exit 1
      fi
    else
      log "Non-interactive run: updating existing checkout."
    fi
    log "Updating $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" fetch --all --tags
    git -C "$INSTALL_DIR" pull --ff-only
  else
    err "$INSTALL_DIR exists but is not a git checkout. Refusing to overwrite."
    err "Move or remove it, then re-run this installer."
    exit 1
  fi
else
  # 5. Fresh clone
  log "Cloning $REPO_URL into $INSTALL_DIR ..."
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

# 6. Hand off to the detailed installer
cd "$INSTALL_DIR"

if [[ ! -f deploy/install-pingwatch.sh ]]; then
  err "deploy/install-pingwatch.sh not found in $INSTALL_DIR."
  err "Repository layout may have changed."
  exit 1
fi

chmod +x deploy/install-pingwatch.sh
log "Handing off to deploy/install-pingwatch.sh ..."
exec bash deploy/install-pingwatch.sh "$@"
