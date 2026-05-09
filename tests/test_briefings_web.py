"""Web route tests for the pre-race briefing detail page (#700)."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.briefings import (
    Briefing,
    HourlyForecastSample,
    HourlyTideSample,
    VenueConfig,
    compose_briefing,
    register_venue,
    render_animated_gif,
)
from helmlog.web import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage

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


def _briefing(*, lead_hours: int = 12) -> Briefing:
    start = datetime(2026, 4, 30, 1, 0, tzinfo=UTC)
    forecast = [
        HourlyForecastSample(
            timestamp_utc=start + timedelta(hours=h),
            wind_speed_kts=10.0 + h,
            wind_gust_kts=14.0 + h,
            wind_direction_deg=200.0 + h,
            air_temp_c=15.0,
            pressure_hpa=1015.0,
            precip_probability_pct=10.0,
            cloud_cover_pct=50.0,
        )
        for h in range(4)
    ]
    tide = [
        HourlyTideSample(
            timestamp_utc=start + timedelta(hours=h),
            tide_height_m=2.0 + 0.1 * h,
            current_speed_kts=None,
            current_set_deg=None,
        )
        for h in range(4)
    ]
    return compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=lead_hours,
        forecast_samples=forecast,
        tide_samples=tide,
        source_urls={
            "forecast": "https://api.open-meteo.com/v1/forecast?latitude=47.68",
            "tide": "https://tidesandcurrents.noaa.gov/stationhome.html?id=9447130",
        },
        forecast_issued_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        fetched_at=datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
        tide_station_id="9447130",
        tide_station_name="Seattle, WA",
    )


@pytest.mark.asyncio
async def test_briefing_detail_renders_with_og_meta(storage: Storage, tmp_path: Path) -> None:
    b = _briefing()
    bid = await storage.write_briefing(b)
    # Render a real chart so the GET /chart.gif test below has something
    # to serve.
    chart_path = tmp_path / "chart.gif"
    render_animated_gif(b, chart_path)
    await storage.update_briefing_chart_path(briefing_id=bid, chart_path=str(chart_path))

    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/briefings/{bid}")
            assert resp.status_code == 200
            html = resp.text
            # Page identifies the venue and lead time.
            assert "Shilshole Bay" in html
            assert "12 h" in html or "lead 12" in html
            # Source links surface for the crew (per spec).
            assert "api.open-meteo.com" in html
            assert "tidesandcurrents.noaa.gov" in html
            # OG meta is present so a WhatsApp paste previews the chart.
            assert "og:image" in html
            assert "og:title" in html
            assert f"/briefings/{bid}/chart.gif" in html


@pytest.mark.asyncio
async def test_chart_gif_route_serves_image(storage: Storage, tmp_path: Path) -> None:
    b = _briefing()
    bid = await storage.write_briefing(b)
    chart_path = tmp_path / "chart.gif"
    render_animated_gif(b, chart_path)
    await storage.update_briefing_chart_path(briefing_id=bid, chart_path=str(chart_path))

    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/briefings/{bid}/chart.gif")
            assert resp.status_code == 200
            assert resp.headers.get("content-type", "").startswith("image/gif")
            assert resp.content[:6] in (b"GIF87a", b"GIF89a")


@pytest.mark.asyncio
async def test_chart_gif_404_when_chart_unavailable(storage: Storage) -> None:
    """Missing chart file returns 404 — the textual page is still the source of truth."""
    b = _briefing()
    bid = await storage.write_briefing(b)
    # No chart_path set.

    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/briefings/{bid}/chart.gif")
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_briefings_index_lists_recent_with_summary(storage: Storage) -> None:
    """Index lists every briefing sorted by date desc, with headline summary."""
    for lh in (12, 8):
        await storage.write_briefing(_briefing(lead_hours=lh))

    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/briefings")
            assert resp.status_code == 200
            html = resp.text
            assert "Pre-race briefings" in html
            assert "Shilshole Bay" in html
            # Both briefings present with their lead-hours.
            assert "12 h" in html
            assert "8 h" in html
            # Summary columns: wind range, pressure trend.
            assert "Wind (kts)" in html
            assert "Pressure" in html


@pytest.mark.asyncio
async def test_briefings_index_filters_by_venue_and_state(storage: Storage) -> None:
    """Filter form narrows results; non-matching briefings are excluded."""
    # One Generated Shilshole briefing.
    await storage.write_briefing(_briefing(lead_hours=12))
    # One Failed briefing for the same venue/date — different lead.
    failed = compose_briefing(
        venue=SHILSHOLE,
        local_date=date(2026, 4, 29),
        lead_hours=8,
        forecast_samples=[],
        tide_samples=[],
        source_urls={},
        forecast_issued_at=None,
        fetched_at=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
        forecast_error="open-meteo timeout",
    )
    await storage.write_briefing(failed)

    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # No filter — both rows show.
            resp = await client.get("/briefings")
            assert "12 h" in resp.text
            assert "8 h" in resp.text
            # state=Failed — only the failed one.
            resp = await client.get("/briefings?state=Failed")
            assert "8 h" in resp.text
            assert "12 h" not in resp.text
            # venue=shilshole — both still show.
            resp = await client.get("/briefings?venue=shilshole")
            assert "12 h" in resp.text and "8 h" in resp.text
            # venue=bellingham (no rows) — no leads visible, empty-state message shown.
            resp = await client.get("/briefings?venue=bellingham")
            assert "No briefings match" in resp.text


@pytest.mark.asyncio
async def test_briefings_index_rejects_invalid_date(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/briefings?date_from=not-a-date")
            assert resp.status_code == 400


@pytest.mark.asyncio
async def test_briefings_nav_link_present(storage: Storage) -> None:
    """The site nav surfaces the Briefings page."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/briefings")
            assert resp.status_code == 200
            assert 'href="/briefings"' in resp.text


@pytest.mark.asyncio
async def test_briefing_detail_lists_prior_briefings_in_series(
    storage: Storage, tmp_path: Path
) -> None:
    """The series link list per spec — prior leads for the same (venue, date)."""
    for lh in (12, 8, 6):
        await storage.write_briefing(_briefing(lead_hours=lh))

    # Get the lead-6 briefing's id (latest in series).
    rows = await storage.list_briefings_for_date(venue_id="shilshole", local_date=date(2026, 4, 29))
    lead_6 = next(b for b in rows if b.lead_hours == 6)

    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # Find the briefing id by venue/date/lead.
            rows = await storage.list_briefings_for_date(
                venue_id="shilshole", local_date=date(2026, 4, 29)
            )
            target = next(r for r in rows if r.lead_hours == 6)
            # IDs aren't on the dataclass — fetch from a separate helper.
            ids = await storage.list_briefing_ids_for_date(
                venue_id="shilshole", local_date=date(2026, 4, 29)
            )
            target_id = ids[(target.venue_id, target.local_date.isoformat(), target.lead_hours)]
            resp = await client.get(f"/briefings/{target_id}")
            assert resp.status_code == 200
            html = resp.text
            # The two earlier briefings in the series are linked.
            for other_lh in (12, 8):
                assert f"lead {other_lh}" in html or f"{other_lh} h" in html
    _ = lead_6  # just keeps the helpful local for diagnostics


# ---------------------------------------------------------------------------
# /briefings/new + POST /briefings/run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefings_new_form_renders_for_authed_user(storage: Storage) -> None:
    """The custom-briefing form page renders when auth is bypassed (admin)."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/briefings/new")
            assert resp.status_code == 200
            assert "briefing-map" in resp.text
            assert 'name="local_date"' in resp.text
            assert 'name="start_time"' in resp.text


@pytest.mark.asyncio
async def test_briefings_run_rejects_invalid_bbox(storage: Storage) -> None:
    """A bbox span outside the [0.05, 1.0]° guardrails returns 400."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/briefings/run",
                data={
                    # Way too small (< 0.05° span).
                    "lat_min": "47.68",
                    "lon_min": "-122.41",
                    "lat_max": "47.681",
                    "lon_max": "-122.409",
                    "local_date": "2026-05-09",
                    "start_time": "18:00",
                    "end_time": "21:00",
                    "venue_name": "Tiny",
                    "venue_tz": "America/Los_Angeles",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "bbox" in resp.text.lower()


@pytest.mark.asyncio
async def test_briefings_run_rejects_invalid_time_window(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/briefings/run",
                data={
                    "lat_min": "47.55",
                    "lon_min": "-122.55",
                    "lat_max": "47.85",
                    "lon_max": "-122.25",
                    "local_date": "2026-05-09",
                    "start_time": "21:00",
                    "end_time": "18:00",  # before start
                    "venue_name": "Custom",
                    "venue_tz": "America/Los_Angeles",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 400


@pytest.mark.asyncio
async def test_briefings_index_links_to_new_form(storage: Storage) -> None:
    """The index surfaces a 'New briefing' link to /briefings/new."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/briefings")
            assert resp.status_code == 200
            assert 'href="/briefings/new"' in resp.text
