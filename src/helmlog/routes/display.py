"""Remote e-paper display endpoints (#820, #822).

Serves pre-rendered instrument-repeater images for the LilyGo T5-S3 4.7"
e-paper panel (960x540, 16 grey levels).

#820 shipped one fixed layout.  #822 makes screens **data-driven** — a screen
is a template + slots bound to metrics, stored as JSON in the
``DISPLAY_SCREENS`` app-setting and composed from the web UI.

Device-facing (viewer auth):
- ``GET /api/display.png`` / ``.raw`` — render a screen (``?screen=<id>``, or
  the default when omitted).  ``.raw`` is the 4-bit framebuffer; ``.png`` is a
  browser preview.  Both accept ``?invert=1`` (panel-polarity escape hatch).
- ``GET /api/display/screens`` — enabled screens ``[{id, name, order}]`` for
  the device to build its touch menu.
- ``GET /api/display/menu.png`` / ``.raw`` — the menu image.
- ``GET /api/display/menu`` — tap regions ``[{index, x, y, w, h, id, name}]``
  so a touch maps to a screen.

Composer (admin auth):
- ``GET/PUT /api/admin/display/screens`` — read/replace the full screen list.
- ``GET /api/admin/display/catalog`` — templates + metrics for the editor.
- ``POST /api/admin/display/preview.png`` — render an unsaved screen.
- ``GET /admin/display`` — the composer page.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from loguru import logger

from helmlog.auth import require_auth
from helmlog.display_render import (
    menu_regions,
    pack_epd_4bit,
    render_menu,
    render_screen,
)
from helmlog.display_screens import (
    FORMATS,
    METRICS,
    SCREENS_SETTING_KEY,
    TEMPLATES,
    Screen,
    ScreenConfigError,
    default_screens,
    enabled_screens,
    parse_screen,
    parse_screens,
    resolve_screen,
    screen_to_dict,
    screens_to_json,
)
from helmlog.routes._helpers import get_storage, templates, tpl_ctx

if TYPE_CHECKING:
    from PIL import Image

router = APIRouter()


async def _current_timezone(request: Request) -> str:
    """Effective display timezone: DB setting → env → default (mirrors /api/state)."""
    from helmlog.routes._helpers import SETTINGS_BY_KEY
    from helmlog.storage import get_effective_setting

    tz_def = SETTINGS_BY_KEY.get("TIMEZONE")
    tz_default = tz_def.default if tz_def else "UTC"
    return await get_effective_setting(get_storage(request), "TIMEZONE", tz_default)


async def _refresh_seconds(request: Request, key: str) -> int:
    """Read a display-refresh interval setting as a positive int, with fallback."""
    from helmlog.routes._helpers import SETTINGS_BY_KEY
    from helmlog.storage import get_effective_setting

    setting = SETTINGS_BY_KEY[key]
    raw = await get_effective_setting(get_storage(request), key, setting.default)
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = int(setting.default)
    return max(1, value)


async def _load_screens(request: Request) -> list[Screen]:
    """Load the configured screens, falling back to the seed when unset/bad.

    The PUT endpoint validates before saving, so a stored value is normally
    valid; a corrupt value (e.g. hand-edited env override) is logged and the
    seed is used so the panel never goes dark.
    """
    raw = await get_storage(request).get_setting(SCREENS_SETTING_KEY)
    if not raw:
        return default_screens()
    try:
        return parse_screens(raw)
    except ScreenConfigError as exc:
        logger.warning("Invalid {} setting, using defaults: {}", SCREENS_SETTING_KEY, exc)
        return default_screens()


async def _render_current(request: Request, screen_id: str | None) -> Image.Image:
    """Render the requested screen (or the default) from live instruments."""
    screens = await _load_screens(request)
    screen = resolve_screen(screens, screen_id)
    if screen is None:
        detail = f"unknown screen {screen_id!r}" if screen_id else "no screens configured"
        raise HTTPException(status_code=404, detail=detail)
    storage = get_storage(request)
    instruments = await storage.latest_instruments()
    tz = await _current_timezone(request)
    return render_screen(screen, instruments, now=datetime.now(UTC), tz=tz)


def _png_response(img: Image.Image) -> Response:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"}
    )


# ---------------------------------------------------------------------------
# Device-facing screen rendering
# ---------------------------------------------------------------------------


@router.get("/api/display.png")
async def api_display_png(
    request: Request,
    screen: str | None = Query(None),  # noqa: B008
    invert: bool = Query(False),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Render a screen as a 960x540 greyscale PNG (default when ``?screen=`` omitted)."""
    img = await _render_current(request, screen)
    if invert:
        from PIL import ImageOps

        img = ImageOps.invert(img)
    return _png_response(img)


@router.get("/api/display.raw")
async def api_display_raw(
    request: Request,
    screen: str | None = Query(None),  # noqa: B008
    invert: bool = Query(False),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Return a screen packed as EPD47 4-bit framebuffer bytes.

    The body is exactly ``WIDTH * HEIGHT / 2`` bytes (259200) in row-major,
    2-pixels-per-byte order — read it straight into the device framebuffer.
    """
    img = await _render_current(request, screen)
    packed = pack_epd_4bit(img, invert=invert)

    refresh = await _refresh_seconds(request, "DISPLAY_REFRESH_SECONDS")
    full_refresh = await _refresh_seconds(request, "DISPLAY_FULL_REFRESH_SECONDS")
    return Response(
        content=packed,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Refresh-Seconds": str(refresh),
            "X-Full-Refresh-Seconds": str(full_refresh),
        },
    )


@router.get("/api/display/screens")
async def api_display_screens(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List enabled screens ``[{id, name, order}]`` for the device menu."""
    screens = await _load_screens(request)
    return JSONResponse(
        [{"id": s.id, "name": s.name, "order": s.order} for s in enabled_screens(screens)]
    )


# ---------------------------------------------------------------------------
# Touch menu (milestone 2)
# ---------------------------------------------------------------------------


@router.get("/api/display/menu.png")
async def api_display_menu_png(
    request: Request,
    invert: bool = Query(False),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Render the screen-picker menu as a PNG."""
    screens = await _load_screens(request)
    tz = await _current_timezone(request)
    img = render_menu(screens, now=datetime.now(UTC), tz=tz)
    if invert:
        from PIL import ImageOps

        img = ImageOps.invert(img)
    return _png_response(img)


@router.get("/api/display/menu.raw")
async def api_display_menu_raw(
    request: Request,
    invert: bool = Query(False),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Return the menu image packed as EPD47 4-bit framebuffer bytes."""
    screens = await _load_screens(request)
    tz = await _current_timezone(request)
    img = render_menu(screens, now=datetime.now(UTC), tz=tz)
    packed = pack_epd_4bit(img, invert=invert)
    return Response(
        content=packed,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/display/menu")
async def api_display_menu_regions(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Tap regions ``[{index, x, y, w, h, id, name}]`` matching the menu tiles."""
    screens = await _load_screens(request)
    return JSONResponse(menu_regions(screens))


# ---------------------------------------------------------------------------
# Composer (admin)
# ---------------------------------------------------------------------------


@router.get("/api/admin/display/screens")
async def api_admin_display_screens(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Return the full screen list (including disabled) for the composer."""
    screens = await _load_screens(request)
    return JSONResponse([screen_to_dict(s) for s in screens])


@router.put("/api/admin/display/screens")
async def api_admin_display_screens_save(
    request: Request,
    payload: list[dict[str, Any]] = Body(...),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Validate and replace the whole screen list."""
    try:
        screens = parse_screens(payload)
    except ScreenConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await get_storage(request).set_setting(SCREENS_SETTING_KEY, screens_to_json(screens))
    return JSONResponse([screen_to_dict(s) for s in screens])


@router.get("/api/admin/display/catalog")
async def api_admin_display_catalog(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Templates (with their slots) + bindable metrics + formats for the editor."""
    return JSONResponse(
        {
            "templates": [
                {
                    "id": t.id,
                    "name": t.name,
                    "slots": [{"id": s.id, "kind": s.kind} for s in t.slots],
                }
                for t in TEMPLATES.values()
            ],
            "metrics": [{"key": m.key, "label": m.label, "unit": m.unit} for m in METRICS.values()],
            "formats": list(FORMATS),
        }
    )


@router.post("/api/admin/display/preview.png")
async def api_admin_display_preview(
    request: Request,
    payload: dict[str, Any] = Body(...),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Render an unsaved screen config to a PNG for the composer's live preview."""
    try:
        screen = parse_screen(payload)
    except ScreenConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    storage = get_storage(request)
    instruments = await storage.latest_instruments()
    tz = await _current_timezone(request)
    img = render_screen(screen, instruments, now=datetime.now(UTC), tz=tz)
    return _png_response(img)


@router.get("/admin/display", response_class=HTMLResponse, include_in_schema=False)
async def admin_display_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Composer page: CRUD screens with a live preview."""
    return templates.TemplateResponse(
        request, "admin/display.html", tpl_ctx(request, "/admin/display")
    )
