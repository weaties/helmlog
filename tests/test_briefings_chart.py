"""Smoke tests for the briefing animated-GIF renderer (#700).

The renderer is matplotlib + PillowWriter and writes a GIF to disk. We
assert the file exists, has non-trivial size, and starts with the
``GIF89a`` (or ``GIF87a``) magic header — without parsing the frames.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from helmlog.briefings import (
    Briefing,
    HourlyForecastSample,
    HourlyTideSample,
    VenueConfig,
    compose_briefing,
    register_venue,
    render_animated_gif,
)

if TYPE_CHECKING:
    from pathlib import Path

SHILSHOLE = VenueConfig(
    venue_id="shilshole",
    venue_name="Shilshole Bay",
    venue_lat=47.6800,
    venue_lon=-122.4067,
    venue_tz="America/Los_Angeles",
    days_of_week=(0, 2),
    racing_window_local=(time(18, 0), time(21, 0)),
    lead_hours=(12, 8, 6, 4, 2, 0),
)
register_venue(SHILSHOLE)

# GIF magic numbers — Pillow writes GIF89a for animations.
GIF_MAGIC = (b"GIF87a", b"GIF89a")


def _briefing(*, with_tide: bool, with_currents: bool) -> Briefing:
    # Animation window for Shilshole on 2026-04-29 (Wed, raceday) is
    # 17:00–22:00 PT = 2026-04-30 00:00–05:00 UTC. Generate 15-min
    # forecast samples and 6-min current samples spanning the window.
    anim_start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    forecast = [
        HourlyForecastSample(
            timestamp_utc=anim_start + timedelta(minutes=15 * i),
            wind_speed_kts=8.0 + 0.2 * i,
            wind_gust_kts=12.0 + 0.3 * i,
            wind_direction_deg=(200.0 + 2 * i) % 360,
            air_temp_c=15.0,
            pressure_hpa=1015.0 + 0.05 * i,
            precip_probability_pct=10.0,
            cloud_cover_pct=50.0,
        )
        for i in range(21)  # 21 × 15 min = 5 h covers 17:00–22:00
    ]
    tide: list[HourlyTideSample] = []
    if with_tide and with_currents:
        # 6-min current predictions, height carried at the same cadence.
        tide = [
            HourlyTideSample(
                timestamp_utc=anim_start + timedelta(minutes=6 * i),
                tide_height_m=2.0 + 0.05 * i,
                current_speed_kts=0.4 + 0.02 * i,
                current_set_deg=(315.0 + i) % 360,
            )
            for i in range(50)  # 50 × 6 min = 5 h
        ]
    elif with_tide:
        tide = [
            HourlyTideSample(
                timestamp_utc=anim_start + timedelta(hours=h),
                tide_height_m=2.0 + 0.1 * h,
                current_speed_kts=None,
                current_set_deg=None,
            )
            for h in range(6)
        ]
    return compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=12,
        forecast_samples=forecast,
        tide_samples=tide,
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        tide_error="" if with_tide else "no NOAA station",
    )


def test_render_gif_writes_an_animated_gif_with_full_data(tmp_path: Path) -> None:
    out = tmp_path / "shilshole.gif"
    ok = render_animated_gif(_briefing(with_tide=True, with_currents=True), out)
    assert ok is True
    assert out.exists()
    assert out.stat().st_size > 1000
    assert out.read_bytes()[:6] in GIF_MAGIC


def test_render_gif_works_without_currents(tmp_path: Path) -> None:
    """Tide heights present, currents absent — GIF still renders."""
    out = tmp_path / "no-currents.gif"
    ok = render_animated_gif(_briefing(with_tide=True, with_currents=False), out)
    assert ok is True
    assert out.exists()
    assert out.read_bytes()[:6] in GIF_MAGIC


def test_render_gif_works_with_wind_only(tmp_path: Path) -> None:
    """Tide unavailable — wind-only GIF still renders."""
    out = tmp_path / "wind-only.gif"
    ok = render_animated_gif(_briefing(with_tide=False, with_currents=False), out)
    assert ok is True
    assert out.exists()
    assert out.read_bytes()[:6] in GIF_MAGIC
