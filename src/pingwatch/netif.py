"""Resolve the active WLAN interface — USB stick preferred over onboard.

Pure stdlib so the bash host-helper can call it as a CLI
(`PYTHONPATH=/opt/pingwatch/src python3 -m pingwatch.netif`). Reads a
sysfs-style root (the container mounts the host's at /host/sys/class/net).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_SYSFS = "/sys/class/net"


def _is_wireless(iface_dir: Path) -> bool:
    return (iface_dir / "wireless").exists() or (iface_dir / "phy80211").exists()


def _is_usb(iface_dir: Path) -> bool:
    try:
        target = os.path.realpath(iface_dir / "device")
    except OSError:
        return False
    return "/usb" in target


def list_wlan_interfaces(sysfs_root: str = DEFAULT_SYSFS) -> list[str]:
    root = Path(sysfs_root)
    if not root.is_dir():
        return []
    return sorted(e.name for e in root.iterdir() if _is_wireless(e))


def label_for(iface: str, sysfs_root: str = DEFAULT_SYSFS) -> str:
    return "usb" if _is_usb(Path(sysfs_root) / iface) else "intern"


def resolve_active_interface(sysfs_root: str = DEFAULT_SYSFS, fallback: str = "wlan0") -> str:
    ifaces = list_wlan_interfaces(sysfs_root)
    if not ifaces:
        return fallback
    usb = [i for i in ifaces if _is_usb(Path(sysfs_root) / i)]
    if usb:
        return usb[0]
    if fallback in ifaces:
        return fallback
    return ifaces[0]


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the active WLAN interface.")
    ap.add_argument("--sysfs", default=DEFAULT_SYSFS)
    ap.add_argument("--fallback", default="wlan0")
    args = ap.parse_args()
    print(resolve_active_interface(args.sysfs, args.fallback))


if __name__ == "__main__":
    main()
