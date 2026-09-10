"""Tests for the sun-based yaw calibration (scripts/analysis/video/suncheck.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageDraw
from scripts.analysis.video import common, suncheck

if TYPE_CHECKING:
    from pathlib import Path


def test_sun_position_seattle_evening() -> None:
    # 2026-09-10 01:25 UTC (18:25 PDT) in Seattle: sun low in the west.
    az, el = suncheck.sun_position(datetime(2026, 9, 10, 1, 25, 1, tzinfo=UTC), 47.6858, -122.4144)
    assert 260 < az < 272 and 8 < el < 13
    # Local solar noon is near 20:00 UTC: azimuth ~180, high elevation.
    az, el = suncheck.sun_position(datetime(2026, 6, 21, 20, 5, tzinfo=UTC), 47.6858, -122.4144)
    assert 170 < az < 190 and el > 60


def test_find_sun_x_compact_blob_only(tmp_path: Path) -> None:
    im = Image.new("L", (1440, 720), 90)
    dr = ImageDraw.Draw(im)
    x = int(1440 * 0.3)
    dr.ellipse((x - 12, 200, x + 12, 224), fill=255)
    p = tmp_path / "sun.jpg"
    im.save(p, quality=95)
    fx = suncheck.find_sun_x(p)
    assert fx is not None and abs(fx - 0.3) < 0.01
    # Broad glare across the sky is rejected; a dark frame has no sun.
    dr.rectangle((0, 150, 900, 260), fill=255)
    im.save(p, quality=95)
    assert suncheck.find_sun_x(p) is None
    Image.new("L", (1440, 720), 40).save(p)
    assert suncheck.find_sun_x(p) is None
    # Bright deck below the horizon does not count.
    im2 = Image.new("L", (1440, 720), 90)
    ImageDraw.Draw(im2).rectangle((600, 500, 640, 700), fill=255)
    im2.save(p, quality=95)
    assert suncheck.find_sun_x(p) is None


def test_yaw_error_geometry() -> None:
    # Sun at azimuth 266, heading 292 → sun should appear at rel -26. Seen at -30 → yaw error +4.
    assert common.angle_diff(266.0 - 292.0, -30.0) == pytest.approx(4.0)
