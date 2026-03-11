"""Tests for the network management module."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from helmlog.network import (
    ConnectResult,
    check_internet,
    connect_to_ssid,
    disconnect_wlan,
    get_wlan_status,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# get_wlan_status
# ---------------------------------------------------------------------------


class TestGetWlanStatus:
    """WLAN status parsing from nmcli output."""

    @pytest.mark.asyncio
    async def test_connected(self) -> None:
        """Returns connected status with SSID when nmcli reports connected."""
        device_output = "wlan0:connected:MyNetwork\neth0:connected:Wired\n"
        ip_output = "IP4.ADDRESS[1]:192.168.1.50/24\n"
        wifi_output = "*:85:MyNetwork\n"

        call_count = 0

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if "device" in args and "status" in args:
                return subprocess.CompletedProcess(args, 0, stdout=device_output, stderr="")
            if "IP4.ADDRESS" in str(args):
                return subprocess.CompletedProcess(args, 0, stdout=ip_output, stderr="")
            if "wifi" in args and "list" in args:
                return subprocess.CompletedProcess(args, 0, stdout=wifi_output, stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("helmlog.network._run_nmcli", side_effect=fake_run):
            status = await get_wlan_status()

        assert status.connected is True
        assert status.ssid == "MyNetwork"
        assert status.ip_address == "192.168.1.50"
        assert status.signal_strength == 85

    @pytest.mark.asyncio
    async def test_disconnected(self) -> None:
        """Returns disconnected when wlan0 is not connected."""
        device_output = "wlan0:disconnected:--\neth0:connected:Wired\n"

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout=device_output, stderr="")

        with patch("helmlog.network._run_nmcli", side_effect=fake_run):
            status = await get_wlan_status()

        assert status.connected is False
        assert status.ssid is None

    @pytest.mark.asyncio
    async def test_nmcli_not_found(self) -> None:
        """Returns disconnected when nmcli is not available."""
        with patch("helmlog.network._run_nmcli", side_effect=FileNotFoundError("nmcli")):
            status = await get_wlan_status()

        assert status.connected is False
        assert status.ssid is None


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


class TestConnectToSSID:
    """WLAN connection via nmcli."""

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="connected", stderr="")
        with patch("helmlog.network._run_nmcli", return_value=result):
            cr = await connect_to_ssid("TestNet", "password123")

        assert cr.success is True
        assert cr.ssid == "TestNet"
        assert cr.error is None

    @pytest.mark.asyncio
    async def test_connect_failure(self) -> None:
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="No network found")
        with patch("helmlog.network._run_nmcli", return_value=result):
            cr = await connect_to_ssid("BadNet")

        assert cr.success is False
        assert cr.error == "No network found"

    @pytest.mark.asyncio
    async def test_connect_timeout(self) -> None:
        with patch("helmlog.network._run_nmcli", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            cr = await connect_to_ssid("SlowNet", "pw")

        assert cr.success is False
        assert "timed out" in (cr.error or "").lower()


class TestDisconnectWlan:
    """WLAN disconnection."""

    @pytest.mark.asyncio
    async def test_disconnect_success(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="disconnected", stderr="")
        with patch("helmlog.network._run_nmcli", return_value=result):
            cr = await disconnect_wlan()

        assert cr.success is True

    @pytest.mark.asyncio
    async def test_disconnect_failure(self) -> None:
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="Device not managed")
        with patch("helmlog.network._run_nmcli", return_value=result):
            cr = await disconnect_wlan()

        assert cr.success is False


# ---------------------------------------------------------------------------
# check_internet
# ---------------------------------------------------------------------------


class TestCheckInternet:
    @pytest.mark.asyncio
    async def test_internet_reachable(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("subprocess.run", return_value=result):
            assert await check_internet() is True

    @pytest.mark.asyncio
    async def test_internet_unreachable(self) -> None:
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with patch("subprocess.run", return_value=result):
            assert await check_internet() is False


# ---------------------------------------------------------------------------
# Storage CRUD for wlan_profiles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wlan_profile_crud(storage: Storage) -> None:
    """Full CRUD lifecycle for WLAN profiles."""
    # Initially empty
    profiles = await storage.list_wlan_profiles()
    assert profiles == []

    # Add
    pid = await storage.add_wlan_profile("Home", "HomeNet", "secret", is_default=True)
    assert pid > 0

    # List
    profiles = await storage.list_wlan_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "Home"
    assert profiles[0]["ssid"] == "HomeNet"
    assert profiles[0]["password"] == "secret"
    assert profiles[0]["is_default"] == 1

    # Get by id
    profile = await storage.get_wlan_profile(pid)
    assert profile is not None
    assert profile["ssid"] == "HomeNet"

    # Update
    ok = await storage.update_wlan_profile(pid, "Marina", "MarinaWiFi", "pass2", is_default=False)
    assert ok is True
    updated = await storage.get_wlan_profile(pid)
    assert updated is not None
    assert updated["name"] == "Marina"
    assert updated["ssid"] == "MarinaWiFi"
    assert updated["is_default"] == 0

    # Delete
    ok = await storage.delete_wlan_profile(pid)
    assert ok is True
    assert await storage.get_wlan_profile(pid) is None

    # Delete non-existent
    ok = await storage.delete_wlan_profile(9999)
    assert ok is False


@pytest.mark.asyncio
async def test_wlan_profile_default_exclusivity(storage: Storage) -> None:
    """Setting a profile as default clears the default on others."""
    pid1 = await storage.add_wlan_profile("Net1", "SSID1", is_default=True)
    pid2 = await storage.add_wlan_profile("Net2", "SSID2", is_default=True)

    p1 = await storage.get_wlan_profile(pid1)
    p2 = await storage.get_wlan_profile(pid2)
    assert p1 is not None and p1["is_default"] == 0
    assert p2 is not None and p2["is_default"] == 1


# ---------------------------------------------------------------------------
# Auto-switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_switch_race_start_disabled(storage: Storage) -> None:
    """auto_switch_for_race_start returns None when disabled."""
    from helmlog.network import auto_switch_for_race_start

    result = await auto_switch_for_race_start(storage)
    assert result is None


@pytest.mark.asyncio
async def test_auto_switch_race_start_connects_camera(storage: Storage) -> None:
    """auto_switch_for_race_start connects to first camera with SSID."""
    from helmlog.network import auto_switch_for_race_start

    await storage.set_setting("NETWORK_AUTO_SWITCH", "true")
    await storage.add_camera("bow", "192.168.42.1", wifi_ssid="Insta360_BOW", wifi_password="pw")

    with patch(
        "helmlog.network.connect_to_ssid",
        return_value=ConnectResult(success=True, ssid="Insta360_BOW"),
    ) as mock_connect:
        result = await auto_switch_for_race_start(storage)

    assert result is not None
    assert result.success is True
    mock_connect.assert_called_once_with("Insta360_BOW", "pw")


@pytest.mark.asyncio
async def test_auto_switch_race_end_reverts(storage: Storage) -> None:
    """auto_switch_for_race_end reverts to default profile."""
    from helmlog.network import auto_switch_for_race_end

    pid = await storage.add_wlan_profile("Home", "HomeNet", "secret", is_default=True)
    await storage.set_setting("NETWORK_AUTO_SWITCH", "true")
    await storage.set_setting("NETWORK_DEFAULT_PROFILE", str(pid))

    with patch(
        "helmlog.network.connect_to_ssid",
        return_value=ConnectResult(success=True, ssid="HomeNet"),
    ) as mock_connect:
        result = await auto_switch_for_race_end(storage)

    assert result is not None
    assert result.success is True
    mock_connect.assert_called_once_with("HomeNet", "secret")
