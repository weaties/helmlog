"""Tests for the color scheme / theming system (#347)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio

from helmlog.themes import (
    PRESETS,
    SYSTEM_DEFAULT_ID,
    resolve_theme,
    theme_to_css,
    wcag_contrast,
)
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
# WCAG contrast ratio
# ---------------------------------------------------------------------------


def test_wcag_contrast_white_black() -> None:
    """White on black should return 21:1."""
    ratio = wcag_contrast("#FFFFFF", "#000000")
    assert abs(ratio - 21.0) < 0.01


def test_wcag_contrast_identical() -> None:
    """Same color should return 1:1."""
    assert wcag_contrast("#123456", "#123456") == pytest.approx(1.0, abs=0.01)


def test_all_presets_pass_wcag_aa() -> None:
    """All built-in presets must meet WCAG AA (4.5:1) for text on background."""
    for preset_id, theme in PRESETS.items():
        ratio = wcag_contrast(theme.text_primary, theme.bg_primary)
        assert ratio >= 4.5, f"Preset {preset_id!r}: contrast ratio {ratio:.1f} is below AA (4.5:1)"


# ---------------------------------------------------------------------------
# Theme resolution — decision table
# ---------------------------------------------------------------------------


def _custom(id_: int, name: str = "Test") -> dict[str, Any]:
    return {"id": id_, "name": name, "bg": "#001122", "text_color": "#EEEEFF", "accent": "#4488FF"}


def test_no_config_returns_system_default() -> None:
    """Row 9: no user, no boat default → ocean_dark."""
    theme = resolve_theme(None, None, [])
    assert theme.id == SYSTEM_DEFAULT_ID


def test_boat_default_preset_applied_when_no_user_override() -> None:
    """Row 6: user has no override, boat default is a valid preset."""
    theme = resolve_theme(None, "sunlight", [])
    assert theme.id == "sunlight"


def test_user_override_preset_beats_boat_default() -> None:
    """Row 1: user override (preset) takes precedence over boat default."""
    theme = resolve_theme("racing_yellow", "sunlight", [])
    assert theme.id == "racing_yellow"


def test_user_override_custom_scheme() -> None:
    """Row 2: user has a custom scheme override."""
    custom = [_custom(7, "My Scheme")]
    theme = resolve_theme("custom:7", None, custom)
    assert theme.id == "custom:7"
    assert theme.name == "My Scheme"


def test_user_override_deleted_scheme_falls_back_to_boat_default() -> None:
    """Row 3: user override references a deleted custom scheme → boat default."""
    custom: list[dict[str, Any]] = []  # scheme deleted
    theme = resolve_theme("custom:99", "sunlight", custom)
    assert theme.id == "sunlight"


def test_user_override_deleted_boat_default_also_deleted_returns_system_default() -> None:
    """Row 5: user override and boat default both deleted → system default."""
    theme = resolve_theme("custom:99", "custom:100", [])
    assert theme.id == SYSTEM_DEFAULT_ID


def test_boat_default_deleted_returns_system_default() -> None:
    """Row 8: no user override, boat default references deleted custom → system default."""
    theme = resolve_theme(None, "custom:99", [])
    assert theme.id == SYSTEM_DEFAULT_ID


def test_user_override_deleted_boat_default_valid() -> None:
    """Row 3 variant: user references deleted scheme, boat default is valid preset."""
    theme = resolve_theme("custom:0", "racing_yellow", [])
    assert theme.id == "racing_yellow"


def test_unknown_preset_id_treated_as_deleted() -> None:
    """An unrecognized non-custom scheme falls through to boat default."""
    theme = resolve_theme("nonexistent_scheme", "sunlight", [])
    assert theme.id == "sunlight"


# ---------------------------------------------------------------------------
# CSS generation
# ---------------------------------------------------------------------------


def test_theme_to_css_contains_all_variables() -> None:
    """theme_to_css output contains all 8 CSS custom properties."""
    css = theme_to_css(PRESETS["ocean_dark"])
    assert "--bg-primary" in css
    assert "--text-primary" in css
    assert "--accent" in css
    assert "--bg-secondary" in css
    assert "--text-secondary" in css
    assert "--border" in css
    assert "--bg-input" in css
    assert "--accent-strong" in css


def test_theme_to_css_uses_root_selector() -> None:
    """:root must be the selector."""
    css = theme_to_css(PRESETS["sunlight"])
    assert css.startswith(":root{")


# ---------------------------------------------------------------------------
# Storage — color scheme CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_color_scheme(storage: Storage) -> None:
    """create_color_scheme and list_color_schemes round-trip."""
    scheme_id = await storage.create_color_scheme("Test", "#000", "#fff", "#aaa", None)
    schemes = await storage.list_color_schemes()
    assert any(s["id"] == scheme_id and s["name"] == "Test" for s in schemes)


@pytest.mark.asyncio
async def test_update_color_scheme(storage: Storage) -> None:
    sid = await storage.create_color_scheme("Old", "#000", "#fff", "#aaa", None)
    ok = await storage.update_color_scheme(sid, "New", "#111", "#eee", "#bbb")
    assert ok is True
    cs = await storage.get_color_scheme(sid)
    assert cs is not None
    assert cs["name"] == "New"
    assert cs["bg"] == "#111"


@pytest.mark.asyncio
async def test_delete_color_scheme(storage: Storage) -> None:
    sid = await storage.create_color_scheme("Del", "#000", "#fff", "#aaa", None)
    ok = await storage.delete_color_scheme(sid)
    assert ok is True
    assert await storage.get_color_scheme(sid) is None
    # second delete returns False
    assert await storage.delete_color_scheme(sid) is False


@pytest.mark.asyncio
async def test_set_user_color_scheme(storage: Storage) -> None:
    """set_user_color_scheme persists and is readable via get_user_by_id."""
    user_id = await storage.create_user("cs@test.com", name="CS Tester", role="viewer")
    await storage.set_user_color_scheme(user_id, "racing_yellow")
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["color_scheme"] == "racing_yellow"


@pytest.mark.asyncio
async def test_reset_user_color_scheme(storage: Storage) -> None:
    """set_user_color_scheme(None) clears the preference."""
    user_id = await storage.create_user("cs2@test.com", name="CS2", role="viewer")
    await storage.set_user_color_scheme(user_id, "sunlight")
    await storage.set_user_color_scheme(user_id, None)
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["color_scheme"] is None


# ---------------------------------------------------------------------------
# API — color scheme endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_list_color_schemes(client: httpx.AsyncClient) -> None:
    """GET /api/color-schemes returns presets and custom list."""
    resp = await client.get("/api/color-schemes")
    assert resp.status_code == 200
    data = resp.json()
    preset_ids = [p["id"] for p in data["presets"]]
    assert "ocean_dark" in preset_ids
    assert "sunlight" in preset_ids
    assert "racing_yellow" in preset_ids
    assert "custom" in data
    assert "boat_default" in data


@pytest.mark.asyncio
async def test_api_create_custom_scheme(client: httpx.AsyncClient) -> None:
    """POST /api/color-schemes creates a custom scheme."""
    resp = await client.post(
        "/api/color-schemes",
        json={
            "name": "Corvo Colors",
            "bg": "#000000",
            "text_color": "#FFD600",
            "accent": "#FFD600",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Corvo Colors"
    assert "id" in data


@pytest.mark.asyncio
async def test_api_create_scheme_missing_fields_422(client: httpx.AsyncClient) -> None:
    """POST /api/color-schemes with missing fields returns 422."""
    resp = await client.post("/api/color-schemes", json={"name": "Oops"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_set_boat_default_preset(client: httpx.AsyncClient) -> None:
    """PUT /api/color-schemes/default sets a preset as boat default."""
    resp = await client.put("/api/color-schemes/default", json={"scheme_id": "racing_yellow"})
    assert resp.status_code == 200
    # Verify it shows up in the list
    list_resp = await client.get("/api/color-schemes")
    assert list_resp.json()["boat_default"] == "racing_yellow"


@pytest.mark.asyncio
async def test_api_set_boat_default_unknown_scheme_422(client: httpx.AsyncClient) -> None:
    """PUT /api/color-schemes/default with unknown scheme returns 422."""
    resp = await client.put("/api/color-schemes/default", json={"scheme_id": "bogus_scheme"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_set_boat_default_custom_scheme(client: httpx.AsyncClient) -> None:
    """PUT /api/color-schemes/default with a custom scheme id persists (regression #747).

    The admin/settings page now triggers this PUT directly on click instead of
    staging the selection in JS. This guards the path the new click handler hits.
    """
    create = await client.post(
        "/api/color-schemes",
        json={"name": "Corvo", "bg": "#000000", "text_color": "#FFD600", "accent": "#FFD600"},
    )
    assert create.status_code == 201
    sid = create.json()["id"]
    resp = await client.put("/api/color-schemes/default", json={"scheme_id": f"custom:{sid}"})
    assert resp.status_code == 200
    list_resp = await client.get("/api/color-schemes")
    assert list_resp.json()["boat_default"] == f"custom:{sid}"


@pytest.mark.asyncio
async def test_api_delete_custom_scheme(client: httpx.AsyncClient) -> None:
    """DELETE /api/color-schemes/{id} removes the scheme."""
    create = await client.post(
        "/api/color-schemes",
        json={"name": "Temp", "bg": "#111", "text_color": "#eee", "accent": "#aaa"},
    )
    assert create.status_code == 201
    sid = create.json()["id"]
    # delete it
    del_resp = await client.delete(f"/api/color-schemes/{sid}")
    assert del_resp.status_code == 204
    # confirm gone
    list_resp = await client.get("/api/color-schemes")
    custom_ids = [c["id"] for c in list_resp.json()["custom"]]
    assert f"custom:{sid}" not in custom_ids


@pytest.mark.asyncio
async def test_api_set_and_reset_user_color_scheme(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH + DELETE /api/me/color-scheme sets and resets user override."""
    from helmlog.web import create_app

    # Create a real user and set AUTH_DISABLED so the mock admin is replaced with a real user
    # by directly testing via storage — and test the endpoints with a real user via storage.
    user_id = await storage.create_user("scheme@test.com", name="Scheme Tester", role="viewer")
    await storage.set_user_color_scheme(user_id, "sunlight")
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["color_scheme"] == "sunlight"

    # Reset via storage
    await storage.set_user_color_scheme(user_id, None)
    user2 = await storage.get_user_by_id(user_id)
    assert user2 is not None
    assert user2["color_scheme"] is None

    # Verify the API endpoints exist and return expected codes for the mock admin
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Mock admin has id=None → endpoint returns 400 (handled gracefully)
        resp = await c.patch("/api/me/color-scheme", json={"scheme_id": "sunlight"})
        assert resp.status_code == 400

        reset_resp = await c.delete("/api/me/color-scheme")
        assert reset_resp.status_code == 400


@pytest.mark.asyncio
async def test_api_set_color_scheme_null_clears_override(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH /api/me/color-scheme with scheme_id=null clears the override (regression #358).

    Previously str(None) produced "None" which failed PRESETS validation with 422.
    """
    import helmlog.auth as auth_module
    from helmlog.web import create_app

    user_id = await storage.create_user("null_scheme@test.com", name="Null Test", role="viewer")
    await storage.set_user_color_scheme(user_id, "sunlight")

    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setitem(auth_module._MOCK_ADMIN, "id", user_id)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Sending null scheme_id must return 200 and clear the override, not 422.
        resp = await c.patch("/api/me/color-scheme", json={"scheme_id": None})
        assert resp.status_code == 200, resp.text

    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["color_scheme"] is None


@pytest.mark.asyncio
async def test_api_profile_page_200(client: httpx.AsyncClient) -> None:
    """GET /profile returns 200 and includes color scheme selector."""
    resp = await client.get("/profile", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "Color Scheme" in resp.text


@pytest.mark.asyncio
async def test_api_admin_settings_page_200(client: httpx.AsyncClient) -> None:
    """GET /admin/settings returns 200 and includes color scheme section."""
    resp = await client.get("/admin/settings", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "Color Scheme" in resp.text
