"""Render the remote e-paper display image (#820, #822).

HelmLog drives a LilyGo T5-S3 4.7" e-paper panel (960x540, 16 grey levels)
as a cockpit instrument repeater.  Rather than lay out fonts on the ESP32,
the panel is a thin frame-buffer client: it fetches a pre-rendered image
from HelmLog and blits it.  This module does the rendering with Pillow.

#820 shipped one fixed layout.  #822 makes a *screen* data-driven — a
``template`` (grid geometry) plus ``slots`` bound to metrics, defined in
``display_screens.py``.  This module turns a resolved :class:`Screen` into
pixels via :func:`render_screen`, renders the touch :func:`render_menu`, and
packs frames into the device's 4-bit framebuffer.

Rendering is a pure function of the screen config + instrument snapshot so it
is trivially testable, and the same code path serves both the human-preview
``.png`` and the device ``.raw`` endpoints in ``routes/display.py``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from helmlog.display_screens import (
    HEADER_H,
    HEIGHT,
    METRICS,
    TEMPLATES,
    WIDTH,
    Screen,
    Slot,
    SlotBinding,
    compute_vmg,
    default_screens,
    enabled_screens,
    metric_value,
    resolve_format,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

# Re-exported so existing importers (tests, #820 callers) keep working after
# the geometry constants and VMG math moved into display_screens.
__all__ = [
    "HEIGHT",
    "WIDTH",
    "compute_vmg",
    "menu_regions",
    "pack_epd_4bit",
    "render_display",
    "render_menu",
    "render_screen",
]

# Pillow returns a FreeType face for a real .ttf and a bitmap face from
# load_default(); draw.text/textbbox accept either.
type AnyFont = ImageFont.FreeTypeFont | ImageFont.ImageFont

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


def _local_clock(now: datetime, tz: str) -> str:
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 — bad tz string shouldn't break the screen
        local = now
    return local.strftime("%H:%M:%S")


def _format_metric_value(fmt: str, value: float | None, *, now: datetime, tz: str) -> str:
    """Render a metric value string per its concrete format token."""
    if fmt == "clock":
        return _local_clock(now, tz)
    if fmt == "angle":
        return _fmt_angle(value)
    decimals = {"num0": 0, "num1": 1, "num2": 2}.get(fmt, 1)
    return _fmt(value, decimals)


def _slot_label(binding: SlotBinding, fmt: str) -> str:
    """The cell label: override or the metric's name, plus its unit."""
    metric = METRICS[binding.metric]
    label = binding.label if binding.label is not None else metric.label
    # Only numeric readings carry a unit; angles/clock read cleaner without one.
    if metric.unit and fmt in ("num0", "num1", "num2"):
        return f"{label}  {metric.unit}"
    return label


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
    # Labels in black: mid-grey renders too faint on the e-paper panel.
    if label:
        draw.text((x0 + 18, y0 + 12), label, font=_font(label_size), fill=BLACK)
    _centered_text(draw, (x0, y0 + label_size // 2, x1, y1), value, _font(value_size))


def _draw_header(
    draw: ImageDraw.ImageDraw,
    *,
    title: str,
    now: datetime,
    tz: str,
    badge: str | None,
) -> None:
    """Draw the shared black header strip: title left, clock + badge right."""
    draw.rectangle((0, 0, WIDTH, HEADER_H), fill=BLACK)
    draw.text((20, 14), title, font=_font(40), fill=WHITE)

    clock = _local_clock(now, tz)
    clock_font = _font(30)
    c_w = draw.textbbox((0, 0), clock, font=clock_font)[2]
    if badge:
        badge_font = _font(30)
        b_w = draw.textbbox((0, 0), badge, font=badge_font)[2]
        draw.text((WIDTH - b_w - 20, 18), badge, font=badge_font, fill=WHITE)
        draw.text((WIDTH - b_w - c_w - 54, 18), clock, font=clock_font, fill=WHITE)
    else:
        draw.text((WIDTH - c_w - 20, 18), clock, font=clock_font, fill=WHITE)


def _draw_compass(
    draw: ImageDraw.ImageDraw,
    slot: Slot,
    binding: SlotBinding | None,
    instruments: Mapping[str, float | None],
) -> None:
    """Draw a compass dial in *slot*: a ring, cardinal marks, a needle at the
    bound heading metric's bearing, and the value at the centre.
    """
    x0, y0, x1, y1 = slot.box
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    radius = min(x1 - x0, y1 - y0) / 2 - 40

    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=GREY, width=3)

    # Cardinal ticks + letters (N up, clockwise).
    for label, bearing_deg in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        rad = math.radians(bearing_deg)
        ox = cx + radius * math.sin(rad)
        oy = cy - radius * math.cos(rad)
        ix = cx + (radius - 18) * math.sin(rad)
        iy = cy - (radius - 18) * math.cos(rad)
        draw.line((ix, iy, ox, oy), fill=GREY, width=3)
        _centered_text(
            draw,
            (int(ox - 20), int(oy - 20), int(ox + 20), int(oy + 20)),
            label,
            _font(26),
            fill=BLACK,
        )

    bearing = metric_value(binding.metric, instruments) if binding is not None else None
    if bearing is not None:
        rad = math.radians(bearing)
        tip = (cx + (radius - 6) * math.sin(rad), cy - (radius - 6) * math.cos(rad))
        left = math.radians(bearing + 150)
        right = math.radians(bearing - 150)
        base_r = 26
        draw.polygon(
            [
                tip,
                (cx + base_r * math.sin(left), cy - base_r * math.cos(left)),
                (cx, cy),
                (cx + base_r * math.sin(right), cy - base_r * math.cos(right)),
            ],
            fill=BLACK,
        )

    metric = METRICS[binding.metric] if binding is not None else None
    label = binding.label if binding and binding.label else (metric.label if metric else "")
    if label:
        _centered_text(
            draw,
            (int(cx - 100), int(cy - radius / 2 - 6), int(cx + 100), int(cy - radius / 2 + 26)),
            label,
            _font(slot.label_size),
            fill=BLACK,
        )
    _centered_text(
        draw,
        (
            int(cx - 130),
            int(cy - slot.value_size / 2),
            int(cx + 130),
            int(cy + slot.value_size / 2),
        ),
        _fmt_angle(bearing),
        _font(slot.value_size),
    )


def render_screen(
    screen: Screen,
    instruments: Mapping[str, float | None],
    *,
    now: datetime,
    tz: str = "UTC",
    status: str | None = None,
) -> Image.Image:
    """Render *screen* to a 960x540 greyscale ``"L"`` image.

    *instruments* is a ``latest_instruments()`` snapshot.  *status* overrides
    the header badge; when None it is derived — "NO DATA" if the snapshot has
    no headline value, otherwise "LIVE".
    """
    if status is None:
        any_data = any(instruments.get(k) is not None for k in ("bsp_kts", "twa_deg", "tws_kts"))
        status = "LIVE" if any_data else "NO DATA"

    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, title="HELMLOG", now=now, tz=tz, badge=status)

    template = TEMPLATES.get(screen.template)
    if template is None:  # defensive: parse_screens validates this
        template = TEMPLATES["single"]

    for slot in template.slots:
        binding = screen.slots.get(slot.id)
        if slot.kind == "compass":
            _draw_compass(draw, slot, binding, instruments)
            continue
        if binding is None:
            _cell(draw, slot.box, "", "—", value_size=slot.value_size, label_size=slot.label_size)
            continue
        fmt = resolve_format(binding.metric, binding.fmt)
        value = metric_value(binding.metric, instruments)
        text = _format_metric_value(fmt, value, now=now, tz=tz)
        _cell(
            draw,
            slot.box,
            _slot_label(binding, fmt),
            text,
            value_size=slot.value_size,
            label_size=slot.label_size,
        )

    # Separators drawn last so cells sit on top.
    for line in template.separators:
        draw.line(line, fill=GREY, width=3)

    return img


def render_display(
    instruments: Mapping[str, float | None],
    *,
    now: datetime,
    tz: str = "UTC",
    status: str | None = None,
) -> Image.Image:
    """Render the default #820 layout (kept for back-compat).

    Equivalent to rendering the first seeded screen (``Wind & Speed``); the
    ``?screen=``-less endpoints use this so behaviour is unchanged from #820.
    """
    return render_screen(default_screens()[0], instruments, now=now, tz=tz, status=status)


# ---------------------------------------------------------------------------
# Touch menu (#822 milestone 2)
# ---------------------------------------------------------------------------


def _menu_tiles(screens: list[Screen]) -> list[tuple[Screen, tuple[int, int, int, int]]]:
    """Lay enabled screens out as full-width rows, top to bottom.

    A single column (rather than a grid) makes tap hit-testing one-dimensional:
    with the panel mounted in any rotation the firmware only has to get one axis
    right, which removes the class of touch/orientation mismatch flagged in
    eink-screen#1.  Tiles are also larger and easier to hit.
    """
    items = enabled_screens(screens)
    if not items:
        return []
    tile_h = (HEIGHT - HEADER_H) // len(items)
    return [
        (screen, (0, HEADER_H + i * tile_h, WIDTH, HEADER_H + (i + 1) * tile_h))
        for i, screen in enumerate(items)
    ]


def menu_regions(screens: list[Screen]) -> list[dict[str, object]]:
    """Tap regions for the touch menu: ``[{index, x, y, w, h, id, name}, …]``.

    Shares geometry with :func:`render_menu` so a tap maps to exactly the tile
    the device drew.  Only enabled screens appear, ordered like the menu.
    ``index`` is the 1-based number drawn on the tile — the firmware can hit-test
    by rectangle *or*, as a fallback, select by the tile number it drew.
    """
    return [
        {
            "index": i,
            "x": x0,
            "y": y0,
            "w": x1 - x0,
            "h": y1 - y0,
            "id": s.id,
            "name": s.name,
        }
        for i, (s, (x0, y0, x1, y1)) in enumerate(_menu_tiles(screens), start=1)
    ]


def render_menu(
    screens: list[Screen],
    *,
    now: datetime,
    tz: str = "UTC",
) -> Image.Image:
    """Render the screen-picker menu: a numbered tile per enabled screen."""
    img = Image.new("L", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, title="SELECT SCREEN", now=now, tz=tz, badge=None)

    tiles = _menu_tiles(screens)
    if not tiles:
        _centered_text(draw, (0, HEADER_H, WIDTH, HEIGHT), "No screens", _font(60))
        return img

    for i, (screen, box) in enumerate(tiles, start=1):
        x0, y0, x1, y1 = box
        draw.rectangle((x0 + 6, y0 + 6, x1 - 6, y1 - 6), outline=GREY, width=3)
        draw.text((x0 + 22, y0 + 18), str(i), font=_font(40), fill=BLACK)
        _centered_text(draw, (x0 + 60, y0, x1, y1), screen.name, _font(48))
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
