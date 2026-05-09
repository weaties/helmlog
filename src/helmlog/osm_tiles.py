"""OSM XYZ tile fetcher + disk cache for the pre-race briefing basemap.

Used by ``helmlog.briefings.render_animated_gif`` to draw the GIF on top
of the same OpenStreetMap tiles the selection map shows. Tiles are
cached on disk so subsequent renders for the same bbox/zoom are free.

OSM TOS:
- Identify ourselves with a descriptive User-Agent (see ``USER_AGENT``).
- The disk cache means we don't re-fetch every render.
- Heavy users should switch to a self-hosted tile provider; that's a
  drop-in URL replacement (set ``HELMLOG_TILE_URL_TEMPLATE``).
"""

from __future__ import annotations

import asyncio
import io
import math
import os
from typing import TYPE_CHECKING

import httpx
from loguru import logger

if TYPE_CHECKING:
    from pathlib import Path

    from PIL.Image import Image as _PILImage

_DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_SIZE = 256
USER_AGENT = "HelmLog/1.0 (https://github.com/weaties/helmlog)"


def _tile_url_template() -> str:
    return os.environ.get("HELMLOG_TILE_URL_TEMPLATE", _DEFAULT_TILE_URL)


def _deg_to_tile(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2**z
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(max(-85.05, min(85.05, lat)))
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _tile_to_deg(x: float, y: float, z: int) -> tuple[float, float]:
    n = 2**z
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    return math.degrees(lat_rad), lon


def _zoom_for_bbox(lon_span: float) -> int:
    """Pick a zoom that gives ~4 tiles across the longer axis."""
    if lon_span <= 0:
        return 12
    target_per_tile = max(lon_span / 4.0, 0.01)
    z = int(round(math.log2(360.0 / target_per_tile)))
    return max(8, min(15, z))


async def _fetch_tile_bytes(
    z: int,
    x: int,
    y: int,
    cache_dir: Path,
    client: httpx.AsyncClient,
) -> bytes | None:
    cache_path = cache_dir / str(z) / str(x) / f"{y}.png"
    if cache_path.exists():
        try:
            return cache_path.read_bytes()
        except OSError as exc:
            logger.debug("OSM tile cache read failed {}: {}", cache_path, exc)
    url = _tile_url_template().format(z=z, x=x, y=y)
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(5.0),
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("OSM tile fetch failed z={} x={} y={}: {}", z, x, y, exc)
        return None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(resp.content)
    except OSError as exc:
        logger.debug("OSM tile cache write failed {}: {}", cache_path, exc)
    return resp.content


async def fetch_basemap(
    bbox: tuple[float, float, float, float],
    cache_dir: Path,
    *,
    zoom: int | None = None,
) -> tuple[_PILImage | None, tuple[float, float, float, float] | None]:
    """Fetch + stitch OSM tiles covering ``bbox`` (lat_min, lon_min, lat_max, lon_max).

    Returns ``(image, extent)`` where ``extent = (left, right, bottom, top)``
    in lon/lat suitable for ``ax.imshow``. Returns ``(None, None)`` if no
    tiles could be fetched (offline, network blip, etc.) — the caller falls
    back to a plain background.
    """
    from PIL import Image

    lat_min, lon_min, lat_max, lon_max = bbox
    if zoom is None:
        zoom = _zoom_for_bbox(max(lon_max - lon_min, lat_max - lat_min))

    # Tile y is north-up: lat_min (south) → larger y; lat_max (north) → smaller y.
    x_a, y_a = _deg_to_tile(lat_min, lon_min, zoom)
    x_b, y_b = _deg_to_tile(lat_max, lon_max, zoom)
    x_min = int(math.floor(min(x_a, x_b)))
    x_max = int(math.floor(max(x_a, x_b))) + 1
    y_min = int(math.floor(min(y_a, y_b)))
    y_max = int(math.floor(max(y_a, y_b))) + 1

    width_px = (x_max - x_min) * TILE_SIZE
    height_px = (y_max - y_min) * TILE_SIZE
    if width_px <= 0 or height_px <= 0:
        return None, None

    composite = Image.new("RGB", (width_px, height_px), color=(204, 224, 236))

    async with httpx.AsyncClient() as client:
        coros = []
        positions: list[tuple[int, int]] = []
        for x in range(x_min, x_max):
            for y in range(y_min, y_max):
                coros.append(_fetch_tile_bytes(zoom, x, y, cache_dir, client))
                positions.append((x - x_min, y - y_min))
        results = await asyncio.gather(*coros, return_exceptions=False)

    fetched = 0
    for (px, py), data in zip(positions, results, strict=True):
        if data is None:
            continue
        try:
            tile_img = Image.open(io.BytesIO(data)).convert("RGB")
            composite.paste(tile_img, (px * TILE_SIZE, py * TILE_SIZE))
            fetched += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("OSM tile decode failed: {}", exc)

    if fetched == 0:
        return None, None

    nw_lat, nw_lon = _tile_to_deg(x_min, y_min, zoom)
    se_lat, se_lon = _tile_to_deg(x_max, y_max, zoom)
    extent = (nw_lon, se_lon, se_lat, nw_lat)  # left, right, bottom, top
    return composite, extent
