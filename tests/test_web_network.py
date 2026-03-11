"""Tests for network admin web API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.network import ConnectResult, InterfaceInfo, WlanStatus
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest.mark.asyncio
async def test_network_page_returns_html(storage: Storage) -> None:
    """GET /admin/network returns the network admin page."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/network")

    assert resp.status_code == 200
    assert "Network" in resp.text


@pytest.mark.asyncio
async def test_network_status_endpoint(storage: Storage) -> None:
    """GET /api/network/status returns WLAN status and interfaces."""
    app = create_app(storage)

    mock_wlan = WlanStatus(
        connected=True, ssid="TestNet", ip_address="10.0.0.5", signal_strength=72
    )
    mock_ifaces = [
        InterfaceInfo(name="eth0", state="up", ip_address="192.168.1.2", mac_address=None),
        InterfaceInfo(name="wlan0", state="up", ip_address="10.0.0.5", mac_address=None),
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with (
            patch("helmlog.network.get_wlan_status", return_value=mock_wlan),
            patch("helmlog.network.list_interfaces", return_value=mock_ifaces),
            patch("helmlog.network.check_internet", return_value=True),
        ):
            resp = await client.get("/api/network/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["wlan"]["connected"] is True
    assert data["wlan"]["ssid"] == "TestNet"
    assert data["internet"] is True
    assert len(data["interfaces"]) == 2


@pytest.mark.asyncio
async def test_network_profiles_lists_camera_and_saved(storage: Storage) -> None:
    """GET /api/network/profiles returns both camera and saved profiles."""
    app = create_app(storage)
    await storage.add_camera("bow", "192.168.42.1", wifi_ssid="Insta360_BOW", wifi_password="pw")
    await storage.add_wlan_profile("Home", "HomeNet", "secret")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/network/profiles")

    assert resp.status_code == 200
    profiles = resp.json()
    assert len(profiles) == 2
    sources = {p["source"] for p in profiles}
    assert sources == {"camera", "saved"}


@pytest.mark.asyncio
async def test_add_and_delete_wlan_profile_via_api(storage: Storage) -> None:
    """POST + DELETE /api/network/profiles round-trip."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Add
        resp = await client.post(
            "/api/network/profiles",
            json={"name": "Marina", "ssid": "MarinaWiFi", "password": "pass"},
        )
        assert resp.status_code == 201
        pid = resp.json()["id"]

        # Verify listed
        resp = await client.get("/api/network/profiles")
        assert any(p["name"] == "Marina" for p in resp.json())

        # Delete
        resp = await client.delete(f"/api/network/profiles/{pid}")
        assert resp.status_code == 204


@pytest.mark.asyncio
async def test_network_connect_camera(storage: Storage) -> None:
    """POST /api/network/connect with a camera profile."""
    app = create_app(storage)
    await storage.add_camera("bow", "192.168.42.1", wifi_ssid="CamNet", wifi_password="pw")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with patch(
            "helmlog.network.connect_to_ssid",
            return_value=ConnectResult(success=True, ssid="CamNet"),
        ):
            resp = await client.post(
                "/api/network/connect",
                json={"profile_id": "camera:bow"},
            )

    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_network_disconnect(storage: Storage) -> None:
    """POST /api/network/disconnect returns success."""
    app = create_app(storage)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with patch(
            "helmlog.network.disconnect_wlan",
            return_value=ConnectResult(success=True, ssid=None),
        ):
            resp = await client.post(
                "/api/network/disconnect",
                json={},
            )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
