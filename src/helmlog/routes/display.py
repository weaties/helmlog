"""Remote e-paper display endpoints (#820).

Serves a pre-rendered instrument-repeater image for the LilyGo T5-S3 4.7"
e-paper panel.  ``/api/display.png`` is the human-previewable form (open it
in a browser to see exactly what the device shows); the device-facing packed
``.raw`` endpoint is added in a later milestone.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from helmlog.auth import require_auth
from helmlog.display_render import render_display
from helmlog.routes._helpers import get_storage

router = APIRouter()


async def _current_timezone(request: Request) -> str:
    """Effective display timezone: DB setting → env → default (mirrors /api/state)."""
    from helmlog.routes._helpers import SETTINGS_BY_KEY
    from helmlog.storage import get_effective_setting

    tz_def = SETTINGS_BY_KEY.get("TIMEZONE")
    tz_default = tz_def.default if tz_def else "UTC"
    return await get_effective_setting(get_storage(request), "TIMEZONE", tz_default)


@router.get("/api/display.png")
async def api_display_png(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Render the current instruments as a 960x540 greyscale PNG."""
    storage = get_storage(request)
    instruments = await storage.latest_instruments()
    tz = await _current_timezone(request)

    img = render_display(instruments, now=datetime.now(UTC), tz=tz)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
