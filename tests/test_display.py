"""Tests for the remote e-paper display render + endpoint (#820)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
from PIL import Image

from helmlog.display_render import (
    HEIGHT,
    WIDTH,
    compute_vmg,
    pack_epd_4bit,
    render_display,
)
from helmlog.nmea2000 import PGN_SPEED_THROUGH_WATER, PGN_WIND_DATA, SpeedRecord, WindRecord
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_NOW = datetime(2026, 7, 7, 15, 30, 0, tzinfo=UTC)


def test_compute_vmg_upwind_positive() -> None:
    # 6 kts at 40° TWA → 6*cos(40°) ≈ 4.6, moving toward the wind.
    assert compute_vmg(6.0, 40.0) == pytest.approx(4.6, abs=0.05)


def test_compute_vmg_downwind_negative() -> None:
    # 6 kts at 150° TWA → negative (component is away from the wind).
    assert compute_vmg(6.0, 150.0) < 0


def test_compute_vmg_missing_inputs() -> None:
    assert compute_vmg(None, 40.0) is None
    assert compute_vmg(6.0, None) is None


def test_render_returns_panel_sized_greyscale() -> None:
    img = render_display({"bsp_kts": 6.4, "twa_deg": 42.0, "tws_kts": 12.1}, now=_NOW)
    assert img.size == (WIDTH, HEIGHT)
    assert img.mode == "L"


def test_render_empty_snapshot_is_no_data() -> None:
    # All-None snapshot must still render a full-size image (status "NO DATA").
    img = render_display(dict.fromkeys(("bsp_kts", "twa_deg", "tws_kts"), None), now=_NOW)
    assert img.size == (WIDTH, HEIGHT)


def test_pack_epd_size() -> None:
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    packed = pack_epd_4bit(img)
    assert len(packed) == WIDTH * HEIGHT // 2


def test_pack_epd_white_and_black() -> None:
    white = pack_epd_4bit(Image.new("L", (WIDTH, HEIGHT), 255))
    black = pack_epd_4bit(Image.new("L", (WIDTH, HEIGHT), 0))
    assert set(white) == {0xFF}  # 0xF is white in both nibbles
    assert set(black) == {0x00}


def test_pack_epd_nibble_layout() -> None:
    # Column 0 black, column 1 white, rest white. The first byte of row 0
    # holds (even=col0=black low nibble, odd=col1=white high nibble) = 0xF0.
    img = Image.new("L", (WIDTH, HEIGHT), 255)
    for y in range(HEIGHT):
        img.putpixel((0, y), 0)
    packed = pack_epd_4bit(img)
    assert packed[0] == 0xF0


def test_pack_epd_invert() -> None:
    # Inverting an all-white image yields all-black packing.
    assert set(pack_epd_4bit(Image.new("L", (WIDTH, HEIGHT), 255), invert=True)) == {0x00}


def test_pack_epd_rejects_wrong_size() -> None:
    with pytest.raises(ValueError, match="expected"):
        pack_epd_4bit(Image.new("L", (100, 100), 255))


@pytest.mark.asyncio
async def test_display_raw_endpoint(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/display.raw")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert len(resp.content) == WIDTH * HEIGHT // 2
    # Server-controlled refresh cadence travels with the frame.
    assert int(resp.headers["x-refresh-seconds"]) == 5  # default
    assert int(resp.headers["x-full-refresh-seconds"]) == 300  # default


@pytest.mark.asyncio
async def test_display_raw_refresh_header_follows_setting(storage: Storage) -> None:
    await storage.set_setting("DISPLAY_REFRESH_SECONDS", "12")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/display.raw")
    assert int(resp.headers["x-refresh-seconds"]) == 12


@pytest.mark.asyncio
async def test_display_png_endpoint(storage: Storage) -> None:
    ts = datetime(2026, 7, 7, 15, 0, 0, tzinfo=UTC)
    await storage.write(
        SpeedRecord(pgn=PGN_SPEED_THROUGH_WATER, source_addr=5, timestamp=ts, speed_kts=6.4)
    )
    await storage.write(
        WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=ts,
            wind_speed_kts=12.1,
            wind_angle_deg=42.0,
            reference=0,
        )
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/display.png")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (WIDTH, HEIGHT)
