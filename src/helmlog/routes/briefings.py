"""Routes for pre-race briefings (#700).

GET /briefings                 Filterable list of recent briefings.
GET /briefings/new             Custom-briefing form (map + date/time).
POST /briefings/run            Generate a one-off briefing (crew+ only).
GET /briefings/{id}            HTML detail page (with OG meta).
GET /briefings/{id}/chart.gif  Animated GIF (5-min frames, 17:00–22:00
                               local) written by the briefing job.

The scheduled job that creates briefings is in
``helmlog.briefings.run_briefing_tick`` and runs from main.py; this
module also exposes the ad-hoc form/endpoint so a sailor can render a
GIF for any bbox + date/time.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from helmlog.auth import require_auth
from helmlog.briefings import (
    VenueConfig,
    force_tick,
    get_venue,
    list_venues,
    register_venue,
    render_animated_gif,
    run_briefing_tick,
)
from helmlog.routes._helpers import get_storage, templates, tpl_ctx

if TYPE_CHECKING:
    from helmlog.briefings import Briefing

router = APIRouter()


@router.get("/briefings", response_class=HTMLResponse, include_in_schema=False)
async def briefings_index(
    request: Request,
    venue: str | None = Query(default=None),
    state: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> Response:
    """Filterable index of recent briefings."""
    storage = get_storage(request)
    df = _parse_date(date_from)
    dt = _parse_date(date_to)
    rows = await storage.list_briefings(
        venue_id=venue or None,
        state=state or None,
        date_from=df,
        date_to=dt,
        limit=200,
    )
    items = [(bid, b, _summary(b)) for bid, b in rows]

    # Venue dropdown: registered venues plus any historic venue_ids that
    # no longer have a config (so old briefings stay filterable).
    historic = await storage.list_briefing_venue_ids()
    venue_options = sorted({*(v.venue_id for v in list_venues()), *historic})

    ctx = tpl_ctx(
        request,
        "/briefings",
        items=items,
        venue_options=venue_options,
        get_venue=get_venue,
        filters={
            "venue": venue or "",
            "state": state or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    )
    return templates.TemplateResponse(request, "briefings_list.html", ctx)


def _parse_date(value: str | None) -> _date | None:
    if not value:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid date {value!r} (expect YYYY-MM-DD)"
        ) from exc


@router.get("/briefings/new", response_class=HTMLResponse, include_in_schema=False)
async def briefings_new_form(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> Response:
    """Render the custom-briefing form (map + date + time)."""
    ctx = tpl_ctx(request, "/briefings", user=user)
    return templates.TemplateResponse(request, "briefings_new.html", ctx)


# Hard limits to keep one-off briefings cheap. A bbox much wider than ~1°
# would explode the Open-Meteo grid request and the GIF render time.
_MAX_BBOX_SPAN_DEG = 1.0
_MIN_BBOX_SPAN_DEG = 0.05


@router.post("/briefings/run", include_in_schema=False)
async def briefings_run(  # noqa: PLR0913
    request: Request,
    lat_min: float = Form(...),
    lon_min: float = Form(...),
    lat_max: float = Form(...),
    lon_max: float = Form(...),
    local_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    venue_name: str = Form("Custom"),
    venue_tz: str = Form("America/Los_Angeles"),
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> Response:
    """Run a one-off briefing for the requested bbox and time window.

    Builds an ephemeral ``VenueConfig`` registered under a content-hashed
    id so the renderer (which looks up venues by id) resolves it. The
    render uses the same fetcher + renderer pipeline as the scheduled job.
    """
    import hashlib
    import os
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from helmlog.external import ExternalFetcher

    # --- Validation ---
    lo_lat, hi_lat = sorted((lat_min, lat_max))
    lo_lon, hi_lon = sorted((lon_min, lon_max))
    span_lat = hi_lat - lo_lat
    span_lon = hi_lon - lo_lon
    if not (_MIN_BBOX_SPAN_DEG <= span_lat <= _MAX_BBOX_SPAN_DEG):
        raise HTTPException(
            status_code=400,
            detail=f"bbox lat span must be {_MIN_BBOX_SPAN_DEG}–{_MAX_BBOX_SPAN_DEG}°",
        )
    if not (_MIN_BBOX_SPAN_DEG <= span_lon <= _MAX_BBOX_SPAN_DEG):
        raise HTTPException(
            status_code=400,
            detail=f"bbox lon span must be {_MIN_BBOX_SPAN_DEG}–{_MAX_BBOX_SPAN_DEG}°",
        )
    try:
        target_date = _date.fromisoformat(local_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date {local_date!r}") from exc
    try:
        start_t = _time.fromisoformat(start_time)
        end_t = _time.fromisoformat(end_time)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid HH:MM time") from exc
    if end_t <= start_t:
        raise HTTPException(status_code=400, detail="end_time must be after start_time")
    try:
        ZoneInfo(venue_tz)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid timezone {venue_tz!r}") from exc

    # --- Build ephemeral venue ---
    center_lat = (lo_lat + hi_lat) / 2
    center_lon = (lo_lon + hi_lon) / 2
    # Aim for ~7 grid points across the longer axis; floor to a sensible step.
    longest = max(span_lat, span_lon)
    step = max(0.02, round(longest / 7, 3))
    digest = hashlib.sha1(
        f"{lo_lat:.4f},{lo_lon:.4f},{hi_lat:.4f},{hi_lon:.4f}:{venue_tz}".encode()
    ).hexdigest()[:10]
    venue_id = f"custom_{digest}"
    venue = VenueConfig(
        venue_id=venue_id,
        venue_name=venue_name or "Custom",
        venue_lat=center_lat,
        venue_lon=center_lon,
        venue_tz=venue_tz,
        days_of_week=tuple(range(7)),  # any day
        racing_window_local=(start_t, end_t),
        lead_hours=(0,),
        map_bbox_deg=(lo_lat, lo_lon, hi_lat, hi_lon),
        grid_step_deg=step,
        coastline_geojson=None,
    )
    register_venue(venue)

    tick = force_tick(venue, target_date, lead_hours=0)
    storage = get_storage(request)
    chart_dir = Path(os.environ.get("BRIEFING_CHART_DIR", "data/briefings"))

    async with ExternalFetcher() as fetcher:
        briefing = await run_briefing_tick(
            storage=storage,
            venue=venue,
            tick=tick,
            fetch_forecast=fetcher.fetch_minutely_15_forecast,
            fetch_tide=fetcher.fetch_tide_predictions,
            fetch_currents=fetcher.fetch_current_predictions,
            fetch_grid=fetcher.fetch_minutely_15_grid,
            chart_renderer=render_animated_gif,
            chart_dir=chart_dir,
        )

    if briefing.state != "Generated":
        raise HTTPException(
            status_code=502,
            detail=f"briefing failed: {briefing.error or 'unknown'}",
        )

    ids = await storage.list_briefing_ids_for_date(venue_id=venue_id, local_date=target_date)
    row_id = ids.get((venue_id, target_date.isoformat(), 0))
    if row_id is None:
        raise HTTPException(status_code=500, detail="briefing not persisted")
    _ = (datetime, UTC, user)  # silence unused locals from the import block / dep
    return RedirectResponse(url=f"/briefings/{row_id}", status_code=303)


@router.get("/briefings/{briefing_id}", response_class=HTMLResponse, include_in_schema=False)
async def briefing_detail(request: Request, briefing_id: int) -> Response:
    """Render the briefing detail page (#700)."""
    storage = get_storage(request)
    briefing = await storage.get_briefing_by_id(briefing_id)
    if briefing is None:
        raise HTTPException(status_code=404, detail="briefing not found")

    series = await storage.list_briefings_for_date(
        venue_id=briefing.venue_id, local_date=briefing.local_date
    )
    series_ids = await storage.list_briefing_ids_for_date(
        venue_id=briefing.venue_id, local_date=briefing.local_date
    )

    # Race summary if linked.
    linked_race = None
    if briefing.race_id is not None:
        linked_race = await storage.get_race(briefing.race_id)

    chart_url = f"/briefings/{briefing_id}/chart.gif" if briefing.chart_path else None
    headline = _headline(briefing)

    ctx = tpl_ctx(
        request,
        "/briefings",
        briefing=briefing,
        briefing_id=briefing_id,
        series=series,
        series_ids=series_ids,
        linked_race=linked_race,
        chart_url=chart_url,
        headline=headline,
    )
    return templates.TemplateResponse(request, "briefing_detail.html", ctx)


@router.get("/briefings/{briefing_id}/chart.gif", include_in_schema=False)
async def briefing_chart(request: Request, briefing_id: int) -> Response:
    """Serve the animated GIF for a briefing, or 404 if it isn't rendered."""
    storage = get_storage(request)
    chart_path = await storage.get_briefing_chart_path(briefing_id)
    if not chart_path:
        raise HTTPException(status_code=404, detail="chart unavailable")
    p = Path(chart_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="chart unavailable")
    return FileResponse(p, media_type="image/gif")


def _headline(briefing: Briefing) -> str:
    """One-line summary used as og:description and the page subtitle."""
    forecast = briefing.hourly_forecast
    if not forecast:
        return "Briefing failed — see source error on detail page"
    speeds = [s.wind_speed_kts for s in forecast]
    gusts = [s.wind_gust_kts for s in forecast]
    lo, hi = min(speeds), max(speeds)
    g_hi = max(gusts)
    return f"Wind {lo:.0f}–{hi:.0f} kts (gust {g_hi:.0f}) · pressure {briefing.pressure_trend}"


def _summary(briefing: Briefing) -> dict[str, object]:
    """Per-row summary fields rendered in the index table."""
    forecast = briefing.hourly_forecast
    if forecast:
        speeds = [s.wind_speed_kts for s in forecast]
        gusts = [s.wind_gust_kts for s in forecast]
        dirs = [s.wind_direction_deg for s in forecast]
        wind_lo, wind_hi = min(speeds), max(speeds)
        gust_hi = max(gusts)
        # Direction range as a tight string ("210–230°") if it varies, else single.
        dir_lo, dir_hi = round(min(dirs)), round(max(dirs))
        dir_str = f"{dir_lo}°" if dir_lo == dir_hi else f"{dir_lo}–{dir_hi}°"
    else:
        wind_lo = wind_hi = gust_hi = 0.0
        dir_str = "—"
    return {
        "headline": _headline(briefing),
        "wind_lo": wind_lo,
        "wind_hi": wind_hi,
        "gust_hi": gust_hi,
        "dir_str": dir_str,
        "has_chart": bool(briefing.chart_path),
        "tide_unavailable": briefing.tide_unavailable_reason is not None,
    }
