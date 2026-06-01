#!/bin/sh
set -e

# Sanity check: unprivileged ICMP datagram socket. On the host this requires
# net.ipv4.ping_group_range to include our gid. We warn but do not fail —
# the app gracefully degrades to non-ICMP probes if needed.
if ! python -c "import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()" 2>/dev/null; then
  echo "WARN: unprivileged ICMP socket unavailable. Set on host:" >&2
  echo "      sudo sysctl -w net.ipv4.ping_group_range='0 2147483647'" >&2
fi

exec "$@"
