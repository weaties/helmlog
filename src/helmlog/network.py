"""WLAN profile switching via NetworkManager (nmcli).

Hardware isolation: all wireless interface management lives in this module so
the rest of the codebase can be tested without physical Wi-Fi hardware.

The Pi's ``wlan0`` interface connects either to an Insta360 camera AP (for
camera control during races) or to a shore/marina network for general
connectivity.  This module drives ``nmcli`` to switch between saved profiles.

Camera Wi-Fi credentials come from the ``cameras`` table (managed by
``/admin/cameras``).  Non-camera profiles live in the ``wlan_profiles`` table.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WlanProfile:
    """A saved WLAN profile (non-camera network)."""

    id: int
    name: str
    ssid: str
    password: str | None
    is_default: bool


@dataclass(frozen=True)
class WlanStatus:
    """Current state of the wlan0 interface."""

    connected: bool
    ssid: str | None
    ip_address: str | None
    signal_strength: int | None  # 0–100 percentage
    interface: str = "wlan0"


@dataclass(frozen=True)
class InterfaceInfo:
    """Status of a single network interface."""

    name: str
    state: str  # "up" / "down" / "unknown"
    ip_address: str | None
    mac_address: str | None


@dataclass(frozen=True)
class ConnectResult:
    """Result of a WLAN connect/disconnect operation."""

    success: bool
    ssid: str | None
    error: str | None = None


# ---------------------------------------------------------------------------
# nmcli helpers
# ---------------------------------------------------------------------------


def _run_nmcli(args: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    """Run an nmcli command and return the result."""
    cmd = ["nmcli", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603


async def _run_nmcli_async(
    args: list[str], *, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    """Run nmcli in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(_run_nmcli, args, timeout=timeout)


# ---------------------------------------------------------------------------
# WLAN status
# ---------------------------------------------------------------------------


async def get_wlan_status(interface: str = "wlan0") -> WlanStatus:
    """Query the current WLAN connection status via nmcli."""
    try:
        result = await _run_nmcli_async(["-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"])
        if result.returncode != 0:
            logger.warning("nmcli device status failed: {}", result.stderr.strip())
            return WlanStatus(connected=False, ssid=None, ip_address=None, signal_strength=None)

        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == interface:
                state = parts[1]
                connection = parts[2] if parts[2] != "--" else None
                connected = state == "connected"
                break
        else:
            return WlanStatus(connected=False, ssid=None, ip_address=None, signal_strength=None)

        # Get IP address and SSID details
        ip_address: str | None = None
        ssid: str | None = connection
        signal: int | None = None

        if connected:
            ip_result = await _run_nmcli_async(
                ["-t", "-f", "IP4.ADDRESS", "device", "show", interface]
            )
            if ip_result.returncode == 0:
                for ip_line in ip_result.stdout.strip().splitlines():
                    if ip_line.startswith("IP4.ADDRESS"):
                        # Format: "IP4.ADDRESS[1]:192.168.1.5/24"
                        addr = ip_line.split(":", 1)[1] if ":" in ip_line else None
                        if addr:
                            ip_address = addr.split("/")[0]
                        break

            # Get signal strength
            sig_result = await _run_nmcli_async(
                ["-t", "-f", "IN-USE,SIGNAL,SSID", "device", "wifi", "list", "ifname", interface]
            )
            if sig_result.returncode == 0:
                for sig_line in sig_result.stdout.strip().splitlines():
                    if sig_line.startswith("*:"):
                        parts = sig_line.split(":")
                        if len(parts) >= 3:
                            with contextlib.suppress(ValueError):
                                signal = int(parts[1])
                            ssid = parts[2]
                        break

        return WlanStatus(
            connected=connected,
            ssid=ssid,
            ip_address=ip_address,
            signal_strength=signal,
            interface=interface,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("WLAN status check failed: {}", exc)
        return WlanStatus(connected=False, ssid=None, ip_address=None, signal_strength=None)


# ---------------------------------------------------------------------------
# Interface listing
# ---------------------------------------------------------------------------


async def list_interfaces() -> list[InterfaceInfo]:
    """List all network interfaces with their status."""
    try:
        result = await _run_nmcli_async(
            ["-t", "-f", "DEVICE,STATE,TYPE,IP4.ADDRESS", "device", "show"]
        )
        if result.returncode != 0:
            # Fallback: simpler query
            result = await _run_nmcli_async(["-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
            if result.returncode != 0:
                return []

            interfaces: list[InterfaceInfo] = []
            for line in result.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[1] in ("wifi", "ethernet", "loopback"):
                    interfaces.append(
                        InterfaceInfo(
                            name=parts[0],
                            state="up" if parts[2] == "connected" else "down",
                            ip_address=None,
                            mac_address=None,
                        )
                    )
            return interfaces

        # Parse full device show output
        interfaces = []
        current: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                if current.get("DEVICE") and current.get("DEVICE") != "lo":
                    interfaces.append(
                        InterfaceInfo(
                            name=current["DEVICE"],
                            state="up" if current.get("STATE") == "connected" else "down",
                            ip_address=current.get("IP4.ADDRESS[1]", "").split("/")[0] or None,
                            mac_address=current.get("HWADDR"),
                        )
                    )
                current = {}
            elif ":" in line:
                key, _, val = line.partition(":")
                current[key.strip()] = val.strip()

        if current.get("DEVICE") and current.get("DEVICE") != "lo":
            interfaces.append(
                InterfaceInfo(
                    name=current["DEVICE"],
                    state="up" if current.get("STATE") == "connected" else "down",
                    ip_address=current.get("IP4.ADDRESS[1]", "").split("/")[0] or None,
                    mac_address=current.get("HWADDR"),
                )
            )

        return interfaces
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("Interface listing failed: {}", exc)
        return []


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


async def connect_to_ssid(
    ssid: str,
    password: str | None = None,
    interface: str = "wlan0",
) -> ConnectResult:
    """Connect the WLAN interface to the given SSID."""
    try:
        if password:
            result = await _run_nmcli_async(
                ["device", "wifi", "connect", ssid, "password", password, "ifname", interface],
                timeout=30.0,
            )
        else:
            result = await _run_nmcli_async(
                ["device", "wifi", "connect", ssid, "ifname", interface],
                timeout=30.0,
            )

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            logger.warning("WLAN connect to {} failed: {}", ssid, error)
            return ConnectResult(success=False, ssid=ssid, error=error)

        logger.info("WLAN connected to {}", ssid)
        return ConnectResult(success=True, ssid=ssid)
    except subprocess.TimeoutExpired:
        logger.warning("WLAN connect to {} timed out", ssid)
        return ConnectResult(success=False, ssid=ssid, error="Connection timed out")
    except (FileNotFoundError, OSError) as exc:
        logger.warning("WLAN connect failed: {}", exc)
        return ConnectResult(success=False, ssid=ssid, error=str(exc))


async def disconnect_wlan(interface: str = "wlan0") -> ConnectResult:
    """Disconnect the WLAN interface."""
    try:
        result = await _run_nmcli_async(["device", "disconnect", interface])
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            logger.warning("WLAN disconnect failed: {}", error)
            return ConnectResult(success=False, ssid=None, error=error)

        logger.info("WLAN disconnected ({})", interface)
        return ConnectResult(success=True, ssid=None)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("WLAN disconnect failed: {}", exc)
        return ConnectResult(success=False, ssid=None, error=str(exc))


# ---------------------------------------------------------------------------
# Internet connectivity check
# ---------------------------------------------------------------------------


async def check_internet() -> bool:
    """Check whether the Pi can reach the internet (ping 1.1.1.1)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ping", "-c", "1", "-W", "3", "1.1.1.1"],
            capture_output=True,
            timeout=5.0,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Race auto-switch
# ---------------------------------------------------------------------------


async def auto_switch_for_race_start(storage: Storage) -> ConnectResult | None:
    """If auto-switch is enabled and the active camera has a WiFi SSID, connect to it.

    Returns the ConnectResult if a switch was attempted, or None if skipped.
    """
    from helmlog.storage import get_effective_setting

    auto_switch = await get_effective_setting(storage, "NETWORK_AUTO_SWITCH", "false")
    if auto_switch.lower() != "true":
        return None

    rows = await storage.list_cameras()
    for cam in rows:
        ssid = cam.get("wifi_ssid")
        password = cam.get("wifi_password")
        if ssid:
            logger.info("Race start: auto-switching WLAN to camera {} ({})", cam["name"], ssid)
            return await connect_to_ssid(ssid, password)

    return None


async def auto_switch_for_race_end(storage: Storage) -> ConnectResult | None:
    """If auto-switch is enabled, revert to the default WLAN profile after a race.

    Returns the ConnectResult if a switch was attempted, or None if skipped.
    """
    from helmlog.storage import get_effective_setting

    auto_switch = await get_effective_setting(storage, "NETWORK_AUTO_SWITCH", "false")
    if auto_switch.lower() != "true":
        return None

    default_profile_id = await get_effective_setting(storage, "NETWORK_DEFAULT_PROFILE", "")
    if not default_profile_id:
        return None

    try:
        profile_id = int(default_profile_id)
    except ValueError:
        return None

    profile = await storage.get_wlan_profile(profile_id)
    if profile is None:
        return None

    logger.info("Race end: auto-switching WLAN back to profile {}", profile["name"])
    return await connect_to_ssid(profile["ssid"], profile.get("password"))
