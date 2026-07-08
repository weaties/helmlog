"""Remote e-paper display endpoints (#820).

Serves a pre-rendered instrument-repeater image for the LilyGo T5-S3 4.7"
e-paper panel (960x540, 16 grey levels).

- ``GET /api/display.png`` — human-previewable PNG (open it in a browser to
  see exactly what the device shows).
- ``GET /api/display.raw`` — the same frame packed into the device's 4-bit
  framebuffer layout, streamed straight onto the panel with no decoding.

Both accept ``?invert=1`` (flip black/white — a panel-polarity escape hatch)
and the ``.raw`` response carries the server-controlled refresh cadence in
``X-Refresh-Seconds`` / ``X-Full-Refresh-Seconds`` headers so the panel can be
retuned from HelmLog's settings without reflashing.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from helmlog.auth import require_auth
from helmlog.display_render import pack_epd_4bit, render_display
from helmlog.routes._helpers import get_storage

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


async def _render_current(request: Request) -> Image.Image:
    """Render the current instrument snapshot to a greyscale image."""
    storage = get_storage(request)
    instruments = await storage.latest_instruments()
    tz = await _current_timezone(request)
    return render_display(instruments, now=datetime.now(UTC), tz=tz)


@router.get("/api/display.png")
async def api_display_png(
    request: Request,
    invert: bool = Query(False),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Render the current instruments as a 960x540 greyscale PNG."""
    img = await _render_current(request)
    if invert:
        from PIL import ImageOps

        img = ImageOps.invert(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"}
    )


@router.get("/api/display.raw")
async def api_display_raw(
    request: Request,
    invert: bool = Query(False),  # noqa: B008
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Return the current frame packed as EPD47 4-bit framebuffer bytes.

    The body is exactly ``WIDTH * HEIGHT / 2`` bytes (259200) in row-major,
    2-pixels-per-byte order — read it straight into the device framebuffer.
    """
    img = await _render_current(request)
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
