"""Tests for admin settings (storage helpers + web API)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.storage import get_effective_setting
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:  # type: ignore[misc]
    """Authenticated admin client (auth disabled via env)."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Storage helper tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_set_roundtrip(storage: Storage) -> None:
    """set_setting + get_setting roundtrip."""
    assert await storage.get_setting("FOO") is None
    await storage.set_setting("FOO", "bar")
    assert await storage.get_setting("FOO") == "bar"


@pytest.mark.asyncio
async def test_set_updates_existing(storage: Storage) -> None:
    """set_setting upserts an existing key."""
    await storage.set_setting("K", "v1")
    await storage.set_setting("K", "v2")
    assert await storage.get_setting("K") == "v2"


@pytest.mark.asyncio
async def test_delete_setting(storage: Storage) -> None:
    """delete_setting removes a key and returns True."""
    await storage.set_setting("K", "v")
    assert await storage.delete_setting("K") is True
    assert await storage.get_setting("K") is None
    # deleting non-existent returns False
    assert await storage.delete_setting("K") is False


@pytest.mark.asyncio
async def test_list_settings(storage: Storage) -> None:
    """list_settings returns all stored settings."""
    await storage.set_setting("A", "1")
    await storage.set_setting("B", "2")
    result = await storage.list_settings()
    keys = [r["key"] for r in result]
    assert "A" in keys
    assert "B" in keys


@pytest.mark.asyncio
async def test_get_effective_setting_priority(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB > env > default priority."""
    monkeypatch.delenv("MY_SETTING", raising=False)

    # default
    assert await get_effective_setting(storage, "MY_SETTING", "fallback") == "fallback"

    # env beats default
    monkeypatch.setenv("MY_SETTING", "from-env")
    assert await get_effective_setting(storage, "MY_SETTING", "fallback") == "from-env"

    # DB beats env
    await storage.set_setting("MY_SETTING", "from-db")
    assert await get_effective_setting(storage, "MY_SETTING", "fallback") == "from-db"


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_returns_all_defs(client: httpx.AsyncClient) -> None:
    """GET /api/settings returns metadata for all curated settings."""
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    keys = [s["key"] for s in data["settings"]]
    assert "TRANSCRIBE_URL" in keys
    assert "WHISPER_MODEL" in keys
    assert "CAMERA_START_TIMEOUT" in keys

    # Each setting has expected fields
    for s in data["settings"]:
        assert "label" in s
        assert "source" in s
        assert s["source"] in ("db", "env", "default")
        assert "effective_value" in s


@pytest.mark.asyncio
async def test_put_settings_saves_and_updates_env(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /api/settings saves to DB and updates os.environ."""
    monkeypatch.delenv("TRANSCRIBE_URL", raising=False)

    resp = await client.put(
        "/api/settings",
        json={"TRANSCRIBE_URL": "http://mac:8321"},
    )
    assert resp.status_code == 200
    assert "TRANSCRIBE_URL" in resp.json()["updated"]

    # Verify it shows up in GET
    resp2 = await client.get("/api/settings")
    settings = {s["key"]: s for s in resp2.json()["settings"]}
    assert settings["TRANSCRIBE_URL"]["source"] == "db"
    assert settings["TRANSCRIBE_URL"]["effective_value"] == "http://mac:8321"

    import os

    assert os.environ.get("TRANSCRIBE_URL") == "http://mac:8321"
    # cleanup
    monkeypatch.delenv("TRANSCRIBE_URL", raising=False)


@pytest.mark.asyncio
async def test_put_empty_deletes_override(client: httpx.AsyncClient) -> None:
    """PUT with empty string deletes the DB override."""
    # First set it
    await client.put("/api/settings", json={"TRANSCRIBE_URL": "http://mac:8321"})
    # Then clear it
    resp = await client.put("/api/settings", json={"TRANSCRIBE_URL": ""})
    assert resp.status_code == 200

    resp2 = await client.get("/api/settings")
    settings = {s["key"]: s for s in resp2.json()["settings"]}
    assert settings["TRANSCRIBE_URL"]["source"] in ("env", "default")


@pytest.mark.asyncio
async def test_put_unknown_key_422(client: httpx.AsyncClient) -> None:
    """PUT with an unknown key returns 422."""
    resp = await client.put("/api/settings", json={"BOGUS_KEY": "val"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_sensitive_field_masked(client: httpx.AsyncClient) -> None:
    """Sensitive settings are masked in GET response."""
    await client.put("/api/settings", json={"PI_SESSION_COOKIE": "secret123"})
    resp = await client.get("/api/settings")
    settings = {s["key"]: s for s in resp.json()["settings"]}
    assert settings["PI_SESSION_COOKIE"]["effective_value"] == "••••••••"


@pytest.mark.asyncio
async def test_monitor_interval_setting_exists(client: httpx.AsyncClient) -> None:
    """MONITOR_INTERVAL_S is listed with correct default and type."""
    resp = await client.get("/api/settings")
    settings = {s["key"]: s for s in resp.json()["settings"]}
    assert "MONITOR_INTERVAL_S" in settings
    s = settings["MONITOR_INTERVAL_S"]
    assert s["effective_value"] == "2"
    assert s["input_type"] == "number"


@pytest.mark.asyncio
async def test_settings_page_returns_200(client: httpx.AsyncClient) -> None:
    """GET /admin/settings returns the HTML page."""
    resp = await client.get("/admin/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text
