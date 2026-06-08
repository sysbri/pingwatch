"""Tests for the WLAN interface resolver."""
from __future__ import annotations

from pathlib import Path

from pingwatch.netif import (
    label_for,
    list_wlan_interfaces,
    resolve_active_interface,
)


def _mk_iface(root: Path, name: str, *, wireless: bool = True, bus: str = "usb") -> None:
    """Build a fake /sys/class/net/<name> entry with a device symlink on *bus*."""
    d = root / name
    d.mkdir(parents=True)
    if wireless:
        (d / "wireless").mkdir()
    busdir = root / "_devices" / bus / name
    busdir.mkdir(parents=True)
    (d / "device").symlink_to(busdir)


def test_lists_only_wireless(tmp_path: Path) -> None:
    _mk_iface(tmp_path, "wlan0", bus="mmc")
    _mk_iface(tmp_path, "eth0", wireless=False, bus="platform")
    assert list_wlan_interfaces(str(tmp_path)) == ["wlan0"]


def test_prefers_usb_over_onboard(tmp_path: Path) -> None:
    _mk_iface(tmp_path, "wlan0", bus="mmc")        # onboard
    _mk_iface(tmp_path, "wlan1", bus="usb")        # stick
    assert resolve_active_interface(str(tmp_path)) == "wlan1"
    assert label_for("wlan1", str(tmp_path)) == "usb"
    assert label_for("wlan0", str(tmp_path)) == "intern"


def test_falls_back_to_onboard_without_usb(tmp_path: Path) -> None:
    _mk_iface(tmp_path, "wlan0", bus="mmc")
    assert resolve_active_interface(str(tmp_path)) == "wlan0"


def test_fallback_name_when_sysfs_empty(tmp_path: Path) -> None:
    assert resolve_active_interface(str(tmp_path), fallback="wlan0") == "wlan0"
