"""Render the remote e-paper display image (#820).

HelmLog drives a LilyGo T5-S3 4.7" e-paper panel (960x540, 16 grey levels)
as a cockpit instrument repeater.  Rather than lay out fonts on the ESP32,
the panel is a thin frame-buffer client: it fetches a pre-rendered image
from HelmLog and blits it.  This module does the rendering with Pillow.

The layout shows six live values plus a derived one:

    STW   boat speed through water          (bsp_kts)
    VMG   velocity made good to wind        (derived: STW * cos(TWA))
    TWA   true wind angle                   (twa_deg)
    TWS   true wind speed                   (tws_kts)
    AWA   apparent wind angle               (awa_deg)
    AWS   apparent wind speed               (aws_kts)

Rendering is a pure function of the instrument snapshot so it is trivially
testable and the same code path serves both the human-preview ``.png`` and
the device ``.raw`` endpoints in ``routes/display.py``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

# Pillow returns a FreeType face for a real .ttf and a bitmap face from
# load_default(); draw.text/textbbox accept either.
type AnyFont = ImageFont.FreeTypeFont | ImageFont.ImageFont

# Panel geometry (LilyGo T5-S3 4.7"). Origin top-left, landscape.
WIDTH = 960
HEIGHT = 540

# Greyscale values (8-bit "L"); the device quantises to 16 levels.
BLACK = 0
DARK = 64
GREY = 128
WHITE = 255

_FONT_DIR = Path(__file__).parent / "static" / "fonts"


def _font(size: int, *, bold: bool = True) -> AnyFont:
    """Load the bundled DejaVu face at *size*.

    ``DISPLAY_FONT_DIR`` overrides the search directory so a boat can drop in
    a different face without code changes.  Falls back to Pillow's built-in
    bitmap font only if the TrueType file is genuinely missing.
    """
    base = Path(os.getenv("DISPLAY_FONT_DIR", str(_FONT_DIR)))
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = base / name
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def compute_vmg(stw_kts: float | None, twa_deg: float | None) -> float | None:
    """Velocity made good to the wind: ``STW * cos(TWA)``.

    Matches HelmLog's semantic-layer definition (see ``semantic_layer.py``).
    Positive upwind (TWA near 0), negative downwind (TWA near 180) — i.e. the
    component of boat speed directly toward the true-wind direction.  Returns
    None if either input is missing.
    """
    if stw_kts is None or twa_deg is None:
        return None
    return round(stw_kts * math.cos(math.radians(twa_deg)), 2)


def _fmt(value: float | None, decimals: int) -> str:
    """Format a number, or an em-dash placeholder when unavailable."""
    if value is None:
        return "—"
    return f"{value:.{decimals}f}"


def _fmt_angle(value: float | None) -> str:
    """Format a wind angle as a whole-degree value with a degree sign."""
    if value is None:
        return "—"
    return f"{round(value)}°"


def _centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: AnyFont,
    *,
    fill: int = BLACK,
) -> None:
    """Draw *text* centered within *box* = (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    tw = right - left
    th = bottom - top
    cx = x0 + (x1 - x0 - tw) / 2 - left
    cy = y0 + (y1 - y0 - th) / 2 - top
    draw.text((cx, cy), text, font=font, fill=fill)


def _cell(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    *,
    value_size: int,
    label_size: int,
) -> None:
    """Draw one labelled value cell: small label top-left, big value centered."""
    x0, y0, x1, y1 = box
    draw.text((x0 + 18, y0 + 12), label, font=_font(label_size), fill=DARK)
    _centered_text(draw, (x0, y0 + label_size // 2, x1, y1), value, _font(value_size))


def render_display(
    instruments: Mapping[str, float | None],
    *,
    now: datetime,
    tz: str = "UTC",
    status: str | None = None,
) -> Image.Image:
    """Render the 960x540 instrument repeater to a greyscale ``"L"`` image.

    *instruments* is a ``latest_instruments()`` snapshot.  *status* overrides
    the header badge; when None it is derived — "NO DATA" if the whole
    snapshot is empty, otherwise "LIVE".
    """
    stw = instruments.get("bsp_kts")
    twa = instruments.get("twa_deg")
    tws = instruments.get("tws_kts")
    awa = instruments.get("awa_deg")
    aws = instruments.get("aws_kts")
    vmg = compute_vmg(stw, twa)

    if status is None:
        any_data = any(instruments.get(k) is not None for k in ("bsp_kts", "twa_deg", "tws_kts"))
        status = "LIVE" if any_data else "NO DATA"

    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)

    # --- Header strip -----------------------------------------------------
    header_h = 66
    draw.rectangle((0, 0, WIDTH, header_h), fill=BLACK)
    draw.text((20, 14), "HELMLOG", font=_font(40), fill=WHITE)

    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 — bad tz string shouldn't break the screen
        local = now
    clock = local.strftime("%H:%M:%S")
    badge = status
    badge_font = _font(30)
    clock_font = _font(30)
    b_w = draw.textbbox((0, 0), badge, font=badge_font)[2]
    c_w = draw.textbbox((0, 0), clock, font=clock_font)[2]
    draw.text((WIDTH - b_w - 20, 18), badge, font=badge_font, fill=WHITE)
    draw.text((WIDTH - b_w - c_w - 54, 18), clock, font=clock_font, fill=WHITE)

    # --- Grid -------------------------------------------------------------
    # Row 1: STW | VMG  (the two headline numbers)
    # Row 2: TWA | TWS | AWA | AWS
    row1_top = header_h
    row1_bot = header_h + 268
    row2_bot = HEIGHT
    mid_x = WIDTH // 2

    _cell(
        draw,
        (0, row1_top, mid_x, row1_bot),
        "STW  kts",
        _fmt(stw, 1),
        value_size=180,
        label_size=34,
    )
    _cell(
        draw,
        (mid_x, row1_top, WIDTH, row1_bot),
        "VMG  kts",
        _fmt(vmg, 1),
        value_size=180,
        label_size=34,
    )

    col_w = WIDTH // 4
    row2_cells = (
        ("TWA", _fmt_angle(twa)),
        ("TWS  kts", _fmt(tws, 1)),
        ("AWA", _fmt_angle(awa)),
        ("AWS  kts", _fmt(aws, 1)),
    )
    for i, (label, value) in enumerate(row2_cells):
        x0 = i * col_w
        _cell(
            draw,
            (x0, row1_bot, x0 + col_w, row2_bot),
            label,
            value,
            value_size=86,
            label_size=30,
        )

    # --- Separators (drawn last so cells sit on top) ----------------------
    draw.line((0, row1_bot, WIDTH, row1_bot), fill=GREY, width=3)
    draw.line((mid_x, row1_top, mid_x, row1_bot), fill=GREY, width=3)
    for i in range(1, 4):
        x = i * col_w
        draw.line((x, row1_bot, x, row2_bot), fill=GREY, width=3)

    return img


def pack_epd_4bit(img: Image.Image, *, invert: bool = False) -> bytes:
    """Pack an ``"L"`` image into the LilyGo EPD47 4-bit framebuffer layout.

    The panel framebuffer is ``WIDTH/2`` bytes per row, 2 pixels per byte, 4
    bits each — the top nibble of the 8-bit grey value.  Per the library's
    ``epd_draw_pixel()``, the even-x pixel occupies the low nibble and the
    odd-x pixel the high nibble; ``0x0`` is black and ``0xF`` white, which
    matches our ``"L"`` image (0 black, 255 white).  The returned buffer is
    exactly ``WIDTH * HEIGHT / 2`` bytes, ready to stream straight into the
    device framebuffer with no decoding.

    *invert* flips black/white before packing — a bring-up escape hatch in
    case a given panel's grayscale polarity is reversed, so it can be fixed
    from the server without reflashing.
    """
    if img.mode != "L":
        img = img.convert("L")
    if img.size != (WIDTH, HEIGHT):
        raise ValueError(f"expected {WIDTH}x{HEIGHT} image, got {img.size[0]}x{img.size[1]}")

    arr = np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(HEIGHT, WIDTH)
    if invert:
        arr = 255 - arr
    nib = (arr >> 4).astype(np.uint8)  # 0..15, one 4-bit level per pixel
    even = nib[:, 0::2]  # even-x pixels → low nibble
    odd = nib[:, 1::2]  # odd-x pixels → high nibble
    packed = ((odd << 4) | even).astype(np.uint8)
    return packed.tobytes()
