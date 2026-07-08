"""Tests for the data-driven display screen model + endpoints (#822)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
from httpx import ASGITransport
from PIL import Image

from helmlog.display_render import (
    HEIGHT,
    WIDTH,
    menu_regions,
    render_menu,
    render_screen,
)
from helmlog.display_screens import (
    METRICS,
    TEMPLATES,
    Screen,
    ScreenConfigError,
    SlotBinding,
    compute_vmg,
    default_screens,
    enabled_screens,
    metric_value,
    parse_screen,
    parse_screens,
    resolve_format,
    resolve_screen,
    screens_to_json,
)
from helmlog.web import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from helmlog.storage import Storage

_NOW = datetime(2026, 7, 7, 15, 30, 0, tzinfo=UTC)
_SNAP = {"bsp_kts": 6.4, "twa_deg": 42.0, "tws_kts": 12.1, "heading_deg": 210.0}


# --- metric registry -------------------------------------------------------


def test_metric_value_reads_column() -> None:
    assert metric_value("stw", _SNAP) == 6.4
    assert metric_value("twa", _SNAP) == 42.0


def test_metric_value_derived_vmg_matches_compute() -> None:
    assert metric_value("vmg", _SNAP) == compute_vmg(6.4, 42.0)


def test_metric_value_clock_is_none() -> None:
    # The clock has no snapshot value; the renderer formats it from `now`.
    assert metric_value("clock", _SNAP) is None


def test_metric_value_missing_returns_none() -> None:
    assert metric_value("sog", _SNAP) is None


def test_resolve_format_auto_uses_metric_default() -> None:
    assert resolve_format("twa", "auto") == "angle"
    assert resolve_format("stw", "auto") == "num1"
    assert resolve_format("stw", "num2") == "num2"


# --- template + screen model ----------------------------------------------


def test_default_screens_reference_only_known_templates_and_metrics() -> None:
    for s in default_screens():
        assert s.template in TEMPLATES
        tpl = TEMPLATES[s.template]
        for slot_id, binding in s.slots.items():
            assert tpl.slot(slot_id) is not None
            assert binding.metric in METRICS


def test_enabled_screens_orders_and_filters() -> None:
    screens = [
        Screen(id="b", name="B", template="single", order=2),
        Screen(id="a", name="A", template="single", order=1),
        Screen(id="c", name="C", template="single", order=3, enabled=False),
    ]
    assert [s.id for s in enabled_screens(screens)] == ["a", "b"]


def test_resolve_screen_default_is_first_enabled() -> None:
    screens = [
        Screen(id="off", name="Off", template="single", order=0, enabled=False),
        Screen(id="on", name="On", template="single", order=1),
    ]
    assert resolve_screen(screens, None).id == "on"  # type: ignore[union-attr]


def test_resolve_screen_by_id_allows_disabled() -> None:
    screens = [Screen(id="off", name="Off", template="single", order=0, enabled=False)]
    assert resolve_screen(screens, "off").id == "off"  # type: ignore[union-attr]


def test_resolve_screen_unknown_id_is_none() -> None:
    assert resolve_screen(default_screens(), "nope") is None


# --- parsing / validation --------------------------------------------------


def _valid_screen_dict() -> dict[str, object]:
    return {
        "id": "s1",
        "name": "Screen 1",
        "template": "single",
        "order": 0,
        "enabled": True,
        "slots": {"main": {"metric": "stw", "fmt": "num2", "label": "Speed"}},
    }


def test_parse_screen_roundtrips() -> None:
    s = parse_screen(_valid_screen_dict())
    assert s.id == "s1"
    assert s.slots["main"] == SlotBinding(metric="stw", label="Speed", fmt="num2")


def test_parse_screens_from_json_string() -> None:
    raw = json.dumps([_valid_screen_dict()])
    assert [s.id for s in parse_screens(raw)] == ["s1"]


def test_parse_screens_roundtrip_via_serialize() -> None:
    screens = default_screens()
    assert [s.id for s in parse_screens(screens_to_json(screens))] == [s.id for s in screens]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(template="bogus"), "unknown template"),
        (lambda d: d.update(slots={"nope": {"metric": "stw"}}), "not in template"),
        (lambda d: d.update(slots={"main": {"metric": "xyz"}}), "unknown metric"),
        (lambda d: d.update(slots={"main": {"metric": "stw", "fmt": "bad"}}), "bad format"),
        (lambda d: d.update(id=""), "id must be"),
        (lambda d: d.update(name=""), "name must be"),
        (lambda d: d.update(order="1"), "order must be an integer"),
        (lambda d: d.update(enabled="yes"), "enabled must be a boolean"),
    ],
)
def test_parse_screen_rejects_bad_config(
    mutate: Callable[[dict[str, object]], object], match: str
) -> None:
    d = _valid_screen_dict()
    mutate(d)
    with pytest.raises(ScreenConfigError, match=match):
        parse_screen(d)


def test_parse_screens_rejects_duplicate_ids() -> None:
    d = _valid_screen_dict()
    with pytest.raises(ScreenConfigError, match="duplicate"):
        parse_screens([d, dict(d)])


def test_parse_screens_rejects_non_list() -> None:
    with pytest.raises(ScreenConfigError, match="must be a JSON list"):
        parse_screens(json.dumps({"not": "a list"}))


# --- rendering -------------------------------------------------------------


@pytest.mark.parametrize("template_id", list(TEMPLATES))
def test_render_screen_every_template_is_panel_sized(template_id: str) -> None:
    tpl = TEMPLATES[template_id]
    # Bind every slot to a plausible metric (hdg for the compass dial).
    slots = {s.id: SlotBinding("hdg" if s.kind == "compass" else "stw") for s in tpl.slots}
    screen = Screen(id="t", name="T", template=template_id, slots=slots)
    img = render_screen(screen, _SNAP, now=_NOW)
    assert img.size == (WIDTH, HEIGHT)
    assert img.mode == "L"


def test_render_screen_unbound_slot_renders() -> None:
    # A screen with no bindings still produces a full image (em-dash cells).
    screen = Screen(id="empty", name="Empty", template="quad")
    img = render_screen(screen, _SNAP, now=_NOW)
    assert img.size == (WIDTH, HEIGHT)


def test_render_screen_missing_data_is_no_data_badge() -> None:
    screen = default_screens()[0]
    img = render_screen(screen, dict.fromkeys(("bsp_kts", "twa_deg", "tws_kts"), None), now=_NOW)
    assert img.size == (WIDTH, HEIGHT)


# --- menu ------------------------------------------------------------------


def test_render_menu_panel_sized() -> None:
    img = render_menu(default_screens(), now=_NOW)
    assert img.size == (WIDTH, HEIGHT)
    assert img.mode == "L"


def test_menu_regions_match_enabled_screens() -> None:
    screens = default_screens()
    regions = menu_regions(screens)
    assert len(regions) == len(enabled_screens(screens))
    ids = {r["id"] for r in regions}
    assert ids == {s.id for s in enabled_screens(screens)}


def test_menu_regions_are_within_panel_bounds() -> None:
    for r in menu_regions(default_screens()):
        assert r["x"] >= 0 and r["y"] >= 0  # type: ignore[operator]
        assert r["x"] + r["w"] <= WIDTH  # type: ignore[operator]
        assert r["y"] + r["h"] <= HEIGHT  # type: ignore[operator]


def test_menu_regions_empty_when_none_enabled() -> None:
    screens = [Screen(id="a", name="A", template="single", enabled=False)]
    assert menu_regions(screens) == []
    # Still renders (a "No screens" placeholder), not a crash.
    assert render_menu(screens, now=_NOW).size == (WIDTH, HEIGHT)


# --- endpoints -------------------------------------------------------------


async def _client(storage: Storage) -> httpx.AsyncClient:
    app = create_app(storage)
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_screens_endpoint_returns_seed(storage: Storage) -> None:
    async with await _client(storage) as client:
        resp = await client.get("/api/display/screens")
    assert resp.status_code == 200
    rows = resp.json()
    assert {"id", "name", "order"} <= set(rows[0])
    assert [r["id"] for r in rows] == [s.id for s in enabled_screens(default_screens())]


@pytest.mark.asyncio
async def test_display_png_selects_screen(storage: Storage) -> None:
    async with await _client(storage) as client:
        resp = await client.get("/api/display.png", params={"screen": "compass"})
    assert resp.status_code == 200
    assert Image.open(io.BytesIO(resp.content)).size == (WIDTH, HEIGHT)


@pytest.mark.asyncio
async def test_display_raw_default_screen_unchanged(storage: Storage) -> None:
    # No ?screen= → default (#820 layout), still a full framebuffer.
    async with await _client(storage) as client:
        resp = await client.get("/api/display.raw")
    assert resp.status_code == 200
    assert len(resp.content) == WIDTH * HEIGHT // 2


@pytest.mark.asyncio
async def test_display_png_unknown_screen_404(storage: Storage) -> None:
    async with await _client(storage) as client:
        resp = await client.get("/api/display.png", params={"screen": "nope"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_menu_endpoints(storage: Storage) -> None:
    async with await _client(storage) as client:
        img_resp = await client.get("/api/display/menu.png")
        raw_resp = await client.get("/api/display/menu.raw")
        reg_resp = await client.get("/api/display/menu")
    assert img_resp.status_code == 200
    assert Image.open(io.BytesIO(img_resp.content)).size == (WIDTH, HEIGHT)
    assert len(raw_resp.content) == WIDTH * HEIGHT // 2
    regions = reg_resp.json()
    assert regions and {"x", "y", "w", "h", "id", "name"} <= set(regions[0])


@pytest.mark.asyncio
async def test_admin_catalog_lists_templates_and_metrics(storage: Storage) -> None:
    async with await _client(storage) as client:
        resp = await client.get("/api/admin/display/catalog")
    body = resp.json()
    assert {t["id"] for t in body["templates"]} == set(TEMPLATES)
    assert {m["key"] for m in body["metrics"]} == set(METRICS)


@pytest.mark.asyncio
async def test_admin_save_and_reload_screens(storage: Storage) -> None:
    new_screens = [
        {
            "id": "only",
            "name": "Only Speed",
            "template": "single",
            "order": 0,
            "enabled": True,
            "slots": {"main": {"metric": "stw", "fmt": "auto"}},
        }
    ]
    async with await _client(storage) as client:
        put = await client.put("/api/admin/display/screens", json=new_screens)
        assert put.status_code == 200
        got = await client.get("/api/admin/display/screens")
        device = await client.get("/api/display/screens")
    assert [s["id"] for s in got.json()] == ["only"]
    assert [s["id"] for s in device.json()] == ["only"]


@pytest.mark.asyncio
async def test_admin_save_rejects_invalid(storage: Storage) -> None:
    async with await _client(storage) as client:
        resp = await client.put(
            "/api/admin/display/screens",
            json=[{"id": "x", "name": "X", "template": "bogus", "slots": {}}],
        )
    assert resp.status_code == 400
    assert "template" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_preview_renders_unsaved(storage: Storage) -> None:
    payload = {
        "id": "draft",
        "name": "Draft",
        "template": "quad",
        "slots": {"q1": {"metric": "stw"}, "q2": {"metric": "vmg"}},
    }
    async with await _client(storage) as client:
        resp = await client.post("/api/admin/display/preview.png", json=payload)
    assert resp.status_code == 200
    assert Image.open(io.BytesIO(resp.content)).size == (WIDTH, HEIGHT)


@pytest.mark.asyncio
async def test_admin_preview_rejects_invalid(storage: Storage) -> None:
    async with await _client(storage) as client:
        resp = await client.post(
            "/api/admin/display/preview.png",
            json={"id": "d", "name": "D", "template": "single", "slots": {"main": {"metric": "z"}}},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_corrupt_setting_falls_back_to_seed(storage: Storage) -> None:
    await storage.set_setting("DISPLAY_SCREENS", "{ not valid json")
    async with await _client(storage) as client:
        resp = await client.get("/api/display/screens")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [s.id for s in enabled_screens(default_screens())]


@pytest.mark.asyncio
async def test_admin_display_page_renders(storage: Storage) -> None:
    async with await _client(storage) as client:
        resp = await client.get("/admin/display")
    assert resp.status_code == 200
    assert "Display Screens" in resp.text
