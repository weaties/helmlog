"""Routes for pre-race briefings (#700).

GET /briefings                 Filterable list of recent briefings.
GET /briefings/{id}            HTML detail page (with OG meta).
GET /briefings/{id}/chart.gif  Animated GIF (5-min frames, 17:00–22:00
                               local) written by the briefing job.

The job that creates briefings is in helmlog.briefings.run_briefing_tick
and runs from main.py at scheduler ticks; the routes here are read-only.
"""

from __future__ import annotations

from datetime import date as _date
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from helmlog.briefings import get_venue, list_venues
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
