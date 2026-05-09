"""Tests for the OSM tile fetcher used by the briefing GIF basemap."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from PIL import Image

from helmlog.osm_tiles import (
    TILE_SIZE,
    _deg_to_tile,
    _tile_to_deg,
    _zoom_for_bbox,
    fetch_basemap,
)

if TYPE_CHECKING:
    from pathlib import Path


def _png_bytes(color: tuple[int, int, int] = (180, 200, 220)) -> bytes:
    img = Image.new("RGB", (TILE_SIZE, TILE_SIZE), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_deg_tile_round_trip() -> None:
    lat, lon = 47.68, -122.41
    z = 12
    x, y = _deg_to_tile(lat, lon, z)
    lat2, lon2 = _tile_to_deg(x, y, z)
    assert abs(lat - lat2) < 1e-6
    assert abs(lon - lon2) < 1e-6


def test_zoom_for_bbox_picks_a_sensible_level() -> None:
    # 0.30° span (Shilshole) → ~4 tiles wide → roughly z=12.
    z = _zoom_for_bbox(0.30)
    assert 11 <= z <= 13


@pytest.mark.asyncio
async def test_fetch_basemap_stitches_tiles_and_caches(tmp_path: Path) -> None:
    """A successful tile fetch returns a stitched image and the extent box."""
    tile_png = _png_bytes()
    call_count = 0

    async def _get(url: str, **_: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.content = tile_png
        r.raise_for_status.return_value = None
        return r

    bbox = (47.65, -122.50, 47.70, -122.40)
    cache = tmp_path / "cache"

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        img, extent = await fetch_basemap(bbox, cache)

    assert img is not None
    assert extent is not None
    left, right, bottom, top = extent
    # extent should bracket the bbox.
    assert left <= bbox[1] and right >= bbox[3]
    assert bottom <= bbox[0] and top >= bbox[2]
    # Tiles cached on disk so a subsequent call doesn't re-fetch.
    cached_files = list(cache.rglob("*.png"))
    assert len(cached_files) >= 1
    first_call_count = call_count

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        img2, extent2 = await fetch_basemap(bbox, cache)
    assert img2 is not None
    assert extent2 == extent
    # Cache hit — no additional network calls.
    assert call_count == first_call_count


@pytest.mark.asyncio
async def test_fetch_basemap_returns_none_when_all_fetches_fail(tmp_path: Path) -> None:
    """If every tile fetch fails the function returns (None, None)."""

    async def _get(url: str, **_: object) -> MagicMock:
        r = MagicMock(spec=httpx.Response)
        r.raise_for_status.side_effect = httpx.HTTPError("offline")
        return r

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=_get)):
        img, extent = await fetch_basemap((47.65, -122.50, 47.70, -122.40), tmp_path / "cache")

    assert img is None
    assert extent is None
