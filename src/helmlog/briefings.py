"""Pre-race weather briefings (#700).

A briefing is a snapshot of forecast wind, weather, tide, and current for a
configured venue's racing window. The job runs at multiple lead times
(default 12, 8, 6, 4, 2, 0 hours before window-start) and persists each run
so the trend across forecasts is preserved for debrief.

This module owns:

- ``VenueConfig`` and the venue registry (Shilshole seeded; others added by
  config).
- ``Briefing`` dataclass and the pure ``compose_briefing`` function that
  turns hourly forecast + tide samples into a stored record.
- Scheduler tick computation: given a venue and "now" in venue-local time,
  what is the next ``(local_date, lead_hours)`` triple to run?

Storage is in ``storage.py`` and the chart renderer / web routes live in
their own modules. Hardware and HTTP I/O are kept out of this file so the
logic is testable without a network or a Pi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    from helmlog.storage import Storage


@dataclass(frozen=True)
class VenueConfig:
    """Per-venue configuration for the pre-race briefing job.

    All scheduling fields are interpreted in ``venue_tz``. ``days_of_week``
    uses Python's ``date.weekday()`` convention (Monday=0). ``lead_hours``
    is the list of hours before ``racing_window_local[0]`` at which a
    briefing should be generated; the largest lead fires first.
    """

    venue_id: str
    venue_name: str
    venue_lat: float
    venue_lon: float
    venue_tz: str
    days_of_week: tuple[int, ...]
    racing_window_local: tuple[time, time]
    lead_hours: tuple[int, ...]
    map_bbox_deg: tuple[float, float, float, float]  # (lat_min, lon_min, lat_max, lon_max)
    grid_step_deg: float  # spacing of the GIF wind grid (LuckGrib-style)
    coastline_geojson: str | None  # filename in helmlog.data.coastlines

    def __init__(
        self,
        *,
        venue_id: str,
        venue_name: str,
        venue_lat: float,
        venue_lon: float,
        venue_tz: str,
        days_of_week: Iterable[int],
        racing_window_local: tuple[time, time],
        lead_hours: Iterable[int],
        map_bbox_deg: tuple[float, float, float, float] | None = None,
        grid_step_deg: float = 0.05,
        coastline_geojson: str | None = None,
    ) -> None:
        # Custom __init__ so callers can pass lists; the stored values are
        # tuples (frozen dataclass equality + hashability).
        object.__setattr__(self, "venue_id", venue_id)
        object.__setattr__(self, "venue_name", venue_name)
        object.__setattr__(self, "venue_lat", venue_lat)
        object.__setattr__(self, "venue_lon", venue_lon)
        object.__setattr__(self, "venue_tz", venue_tz)
        object.__setattr__(self, "days_of_week", tuple(days_of_week))
        object.__setattr__(self, "racing_window_local", racing_window_local)
        # Sort descending so the earliest lead (e.g. 12 h) runs first.
        object.__setattr__(self, "lead_hours", tuple(sorted(lead_hours, reverse=True)))
        if map_bbox_deg is None:
            # Default ±0.15° (~16 km) around the venue.
            map_bbox_deg = (
                venue_lat - 0.15,
                venue_lon - 0.20,
                venue_lat + 0.15,
                venue_lon + 0.20,
            )
        object.__setattr__(self, "map_bbox_deg", map_bbox_deg)
        object.__setattr__(self, "grid_step_deg", grid_step_deg)
        object.__setattr__(self, "coastline_geojson", coastline_geojson)


# ---------------------------------------------------------------------------
# Venue registry
# ---------------------------------------------------------------------------

_SHILSHOLE = VenueConfig(
    venue_id="shilshole",
    venue_name="Shilshole Bay",
    venue_lat=47.6800,
    venue_lon=-122.4067,
    venue_tz="America/Los_Angeles",
    days_of_week=(0, 2),  # Monday, Wednesday
    racing_window_local=(time(18, 0), time(21, 0)),
    lead_hours=(12, 8, 6, 4, 2, 0),
    # Bbox spans south Magnolia to north Edmonds, Bainbridge to Capitol Hill.
    map_bbox_deg=(47.55, -122.55, 47.85, -122.25),
    grid_step_deg=0.05,
    coastline_geojson="puget_sound.json",
)

_REGISTRY: dict[str, VenueConfig] = {
    _SHILSHOLE.venue_id: _SHILSHOLE,
}


def get_venue(venue_id: str) -> VenueConfig | None:
    """Look up a venue by id. Returns None if not registered."""
    return _REGISTRY.get(venue_id)


def list_venues() -> list[VenueConfig]:
    """Return all registered venues."""
    return list(_REGISTRY.values())


def register_venue(venue: VenueConfig) -> None:
    """Register a venue (used by config loaders / tests).

    Replaces any prior registration with the same ``venue_id``.
    """
    _REGISTRY[venue.venue_id] = venue


# ---------------------------------------------------------------------------
# Scheduler tick computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefingTick:
    """A single (venue, local_date, lead_hours) trigger that should run.

    ``trigger_utc`` is the UTC moment the tick fires. The composer uses
    ``window_start_utc``/``window_end_utc`` for the forecast slice it pulls.
    """

    venue_id: str
    local_date: date
    lead_hours: int
    trigger_utc: datetime
    window_start_utc: datetime
    window_end_utc: datetime


def _local_window_to_utc(venue: VenueConfig, local_date: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(venue.venue_tz)
    start_local = datetime.combine(local_date, venue.racing_window_local[0], tzinfo=tz)
    end_local = datetime.combine(local_date, venue.racing_window_local[1], tzinfo=tz)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


# The animated GIF spans the racing window padded by one hour on each side
# (e.g. for Shilshole's 18:00–21:00 window, frames cover 17:00–22:00 local).
# The forecast/current fetchers pull this wider window so every frame has
# data behind it.
ANIMATION_PAD = timedelta(hours=1)


def _animation_window_utc(venue: VenueConfig, local_date: date) -> tuple[datetime, datetime]:
    start, end = _local_window_to_utc(venue, local_date)
    return start - ANIMATION_PAD, end + ANIMATION_PAD


def ticks_for_date(venue: VenueConfig, local_date: date) -> list[BriefingTick]:
    """Return all ticks for a specific venue-local date.

    If ``local_date`` is not one of the venue's ``days_of_week``, returns
    ``[]``. Otherwise returns one tick per entry in ``lead_hours``, in
    descending lead order (so the earliest fire is first).
    """
    if local_date.weekday() not in venue.days_of_week:
        return []
    start_utc, end_utc = _local_window_to_utc(venue, local_date)
    return [
        BriefingTick(
            venue_id=venue.venue_id,
            local_date=local_date,
            lead_hours=lh,
            trigger_utc=start_utc - timedelta(hours=lh),
            window_start_utc=start_utc,
            window_end_utc=end_utc,
        )
        for lh in venue.lead_hours
    ]


def grid_points(venue: VenueConfig) -> tuple[list[float], list[float]]:
    """Return (lats, lons) lists for the venue's GIF wind grid.

    Uses the venue's ``map_bbox_deg`` and ``grid_step_deg``. The returned
    lists are paired by index — element ``i`` is one grid cell at
    ``(lats[i], lons[i])``. Suitable for ``ExternalFetcher.fetch_minutely_15_grid``.
    """
    lat_min, lon_min, lat_max, lon_max = venue.map_bbox_deg
    step = venue.grid_step_deg
    if step <= 0:
        return [venue.venue_lat], [venue.venue_lon]
    lat_steps = max(1, int(round((lat_max - lat_min) / step)))
    lon_steps = max(1, int(round((lon_max - lon_min) / step)))
    lats: list[float] = []
    lons: list[float] = []
    for i in range(lat_steps + 1):
        for j in range(lon_steps + 1):
            lats.append(round(lat_min + i * step, 4))
            lons.append(round(lon_min + j * step, 4))
    return lats, lons


def force_tick(venue: VenueConfig, local_date: date, lead_hours: int = 0) -> BriefingTick:
    """Build a tick for any local_date, bypassing the day-of-week filter.

    Used by the ``helmlog briefing run`` CLI to fire an ad-hoc briefing
    on demand (e.g. for a non-race-day venue test). Production scheduling
    still goes through ``ticks_for_date`` / ``next_tick``, which respect
    the venue's configured race days.
    """
    start_utc, end_utc = _local_window_to_utc(venue, local_date)
    return BriefingTick(
        venue_id=venue.venue_id,
        local_date=local_date,
        lead_hours=lead_hours,
        trigger_utc=datetime.now(UTC),
        window_start_utc=start_utc,
        window_end_utc=end_utc,
    )


def next_tick(venue: VenueConfig, now_utc: datetime) -> BriefingTick | None:
    """Return the next tick at or after ``now_utc`` for this venue.

    Walks forward up to 8 days to find the next race day; returns None if
    the venue has no configured days (defensive).
    """
    if not venue.days_of_week:
        return None
    tz = ZoneInfo(venue.venue_tz)
    today_local = now_utc.astimezone(tz).date()
    for offset in range(8):
        candidate = today_local + timedelta(days=offset)
        for tick in ticks_for_date(venue, candidate):
            if tick.trigger_utc >= now_utc:
                return tick
    return None


# ---------------------------------------------------------------------------
# Briefing dataclass + composer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HourlyForecastSample:
    """One hour of weather forecast at the venue."""

    timestamp_utc: datetime
    wind_speed_kts: float
    wind_gust_kts: float
    wind_direction_deg: float
    air_temp_c: float
    pressure_hpa: float
    precip_probability_pct: float
    cloud_cover_pct: float


@dataclass(frozen=True)
class HourlyTideSample:
    """One hour of tide height + current at the venue."""

    timestamp_utc: datetime
    tide_height_m: float | None
    current_speed_kts: float | None
    current_set_deg: float | None  # direction current is flowing toward


@dataclass(frozen=True)
class Briefing:
    """A composed pre-race briefing for one (venue, local_date, lead_hours)."""

    venue_id: str
    local_date: date
    lead_hours: int
    state: str  # "Generated" | "Failed"
    hourly_forecast: tuple[HourlyForecastSample, ...]
    hourly_tide: tuple[HourlyTideSample, ...]
    pressure_trend: str  # "rising" | "steady" | "falling" | "unknown"
    source_urls: dict[str, str] = field(default_factory=dict)
    forecast_issued_at: datetime | None = None
    fetched_at: datetime | None = None
    error: str | None = None
    tide_unavailable_reason: str | None = None
    tide_station_id: str | None = None
    tide_station_name: str | None = None
    chart_path: str | None = None
    race_id: int | None = None


_PRESSURE_STEADY_HPA = 1.0  # |Δ| ≤ 1 hPa across the racing window → "steady"


def _pressure_trend(samples: Sequence[HourlyForecastSample]) -> str:
    if len(samples) < 2:
        return "unknown"
    delta = samples[-1].pressure_hpa - samples[0].pressure_hpa
    if abs(delta) <= _PRESSURE_STEADY_HPA:
        return "steady"
    return "rising" if delta > 0 else "falling"


def compose_briefing(
    *,
    venue: VenueConfig,
    local_date: date,
    lead_hours: int,
    forecast_samples: Sequence[HourlyForecastSample],
    tide_samples: Sequence[HourlyTideSample],
    source_urls: dict[str, str],
    forecast_issued_at: datetime | None,
    fetched_at: datetime,
    forecast_error: str | None = None,
    tide_error: str | None = None,
    tide_station_id: str | None = None,
    tide_station_name: str | None = None,
) -> Briefing:
    """Compose a Briefing from already-fetched forecast and tide data.

    Pure function — no I/O. Fail-safes:

    - If ``forecast_samples`` is empty (or ``forecast_error`` is set with
      no samples), the briefing is returned in ``Failed`` state with the
      error message attached. Tide data is dropped.
    - If ``tide_samples`` is empty but forecast samples are present, the
      briefing is returned in ``Generated`` state with an empty tide
      block and ``tide_unavailable_reason`` populated.
    - The samples are filtered to the animation window (racing window
      padded by one hour on each side, so the GIF can show pre/post-race
      context) and sorted by timestamp before storage.
    """
    window_start_utc, window_end_utc = _animation_window_utc(venue, local_date)

    if not forecast_samples:
        return Briefing(
            venue_id=venue.venue_id,
            local_date=local_date,
            lead_hours=lead_hours,
            state="Failed",
            hourly_forecast=(),
            hourly_tide=(),
            pressure_trend="unknown",
            source_urls=dict(source_urls),
            forecast_issued_at=forecast_issued_at,
            fetched_at=fetched_at,
            error=forecast_error or "no forecast samples",
        )

    forecast_in_window = tuple(
        sorted(
            (s for s in forecast_samples if window_start_utc <= s.timestamp_utc <= window_end_utc),
            key=lambda s: s.timestamp_utc,
        )
    )

    if not forecast_in_window:
        return Briefing(
            venue_id=venue.venue_id,
            local_date=local_date,
            lead_hours=lead_hours,
            state="Failed",
            hourly_forecast=(),
            hourly_tide=(),
            pressure_trend="unknown",
            source_urls=dict(source_urls),
            forecast_issued_at=forecast_issued_at,
            fetched_at=fetched_at,
            error="no forecast samples covered the racing window",
        )

    tide_in_window = tuple(
        sorted(
            (s for s in tide_samples if window_start_utc <= s.timestamp_utc <= window_end_utc),
            key=lambda s: s.timestamp_utc,
        )
    )

    return Briefing(
        venue_id=venue.venue_id,
        local_date=local_date,
        lead_hours=lead_hours,
        state="Generated",
        hourly_forecast=forecast_in_window,
        hourly_tide=tide_in_window,
        pressure_trend=_pressure_trend(forecast_in_window),
        source_urls=dict(source_urls),
        forecast_issued_at=forecast_issued_at,
        fetched_at=fetched_at,
        error=None,
        tide_unavailable_reason=tide_error if not tide_in_window else None,
        tide_station_id=tide_station_id,
        tide_station_name=tide_station_name,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_NOAA_STATION_BASE_URL = "https://tidesandcurrents.noaa.gov/stationhome.html?id="


class _ForecastFetcher(Protocol):
    async def __call__(
        self,
        *,
        lat: float,
        lon: float,
        start_utc: datetime,
        end_utc: datetime,
    ) -> Sequence[HourlyForecastSample]: ...


class _TideFetcher(Protocol):
    async def __call__(
        self,
        *,
        lat: float,
        lon: float,
        for_date: date,
    ) -> Sequence[Any]: ...


class _CurrentFetcher(Protocol):
    async def __call__(
        self,
        *,
        lat: float,
        lon: float,
        start_utc: datetime,
        end_utc: datetime,
    ) -> Sequence[Any]: ...


class _GridFetcher(Protocol):
    async def __call__(
        self,
        *,
        lats: list[float],
        lons: list[float],
        start_utc: datetime,
        end_utc: datetime,
    ) -> Sequence[Any]: ...


def _forecast_url(venue: VenueConfig) -> str:
    return (
        f"{_OPEN_METEO_BASE_URL}?latitude={venue.venue_lat:.2f}"
        f"&longitude={venue.venue_lon:.2f}&hourly=wind_speed_10m,wind_direction_10m,"
        "wind_gusts_10m,temperature_2m,precipitation_probability,cloud_cover,surface_pressure"
    )


def _tide_to_hourly(tide_readings: Sequence[Any]) -> tuple[list[HourlyTideSample], str | None]:
    """Adapt TideReading-like objects to HourlyTideSample (height-only)."""
    samples: list[HourlyTideSample] = []
    station_id: str | None = None
    for r in tide_readings:
        ts = getattr(r, "timestamp", None)
        height = getattr(r, "height_m", None)
        if ts is None or height is None:
            continue
        if station_id is None:
            station_id = getattr(r, "station_id", None)
        samples.append(
            HourlyTideSample(
                timestamp_utc=ts,
                tide_height_m=float(height),
                current_speed_kts=None,
                current_set_deg=None,
            )
        )
    return samples, station_id


def _merge_currents(
    tide_samples: list[HourlyTideSample], current_readings: Sequence[Any]
) -> list[HourlyTideSample]:
    """Merge 6-min current predictions into the tide-sample timeline.

    Currents are densest, so we emit one combined sample per current
    reading. Tide height is carried forward from the most recent earlier
    tide sample (NOAA tide predictions are hourly; currents predictions
    are 6-min). If no current readings exist, returns ``tide_samples``
    unchanged.
    """
    if not current_readings:
        return tide_samples

    tides_sorted = sorted(tide_samples, key=lambda s: s.timestamp_utc)
    currents_sorted = sorted(
        current_readings,
        key=lambda r: getattr(r, "timestamp"),  # noqa: B009
    )

    out: list[HourlyTideSample] = []
    tide_idx = 0
    last_height: float | None = None
    for cr in currents_sorted:
        cr_ts = getattr(cr, "timestamp", None)
        cr_speed = getattr(cr, "speed_kts", None)
        cr_set = getattr(cr, "set_deg", None)
        if cr_ts is None or cr_speed is None or cr_set is None:
            continue
        # Advance through tide samples whose timestamp is <= cr_ts to
        # carry the most recent height forward.
        while tide_idx < len(tides_sorted) and tides_sorted[tide_idx].timestamp_utc <= cr_ts:
            last_height = tides_sorted[tide_idx].tide_height_m
            tide_idx += 1
        out.append(
            HourlyTideSample(
                timestamp_utc=cr_ts,
                tide_height_m=last_height,
                current_speed_kts=float(cr_speed),
                current_set_deg=float(cr_set),
            )
        )
    return out


async def run_briefing_tick(
    *,
    storage: Storage,
    venue: VenueConfig,
    tick: BriefingTick,
    fetch_forecast: _ForecastFetcher,
    fetch_tide: _TideFetcher,
    fetch_currents: _CurrentFetcher | None = None,
    fetch_grid: _GridFetcher | None = None,
    chart_renderer: Callable[..., bool] | None = None,
    chart_dir: Path | None = None,
    now_utc: datetime | None = None,
) -> Briefing:
    """Execute a single scheduler tick: fetch, compose, persist, link Race.

    Pure-domain composition is delegated to ``compose_briefing``. This
    function owns the side effects: network fetches via the injected
    callables, DB writes via ``storage``, optional chart rendering.

    The tide and currents source errors are captured (never raised) so
    an outage doesn't fail the whole briefing — matches the spec's
    fail-safe rules. A forecast outage produces a ``Failed`` briefing
    and skips Race auto-creation.
    """
    fetched_at = now_utc or datetime.now(UTC)
    anim_start, anim_end = _animation_window_utc(venue, tick.local_date)

    forecast_samples: list[HourlyForecastSample] = []
    forecast_error: str | None = None
    try:
        forecast_samples = list(
            await fetch_forecast(
                lat=venue.venue_lat,
                lon=venue.venue_lon,
                start_utc=anim_start,
                end_utc=anim_end,
            )
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe: capture and continue
        forecast_error = str(exc)
        logger.warning(
            "briefing forecast fetch failed venue={} date={} lead={}h err={}",
            venue.venue_id,
            tick.local_date,
            tick.lead_hours,
            exc,
        )

    tide_samples: list[HourlyTideSample] = []
    tide_error: str | None = None
    tide_station_id: str | None = None
    tide_station_name: str | None = None
    try:
        tide_readings = await fetch_tide(
            lat=venue.venue_lat,
            lon=venue.venue_lon,
            for_date=tick.window_start_utc.date(),
        )
        tide_samples, tide_station_id = _tide_to_hourly(tide_readings)
        if tide_readings:
            tide_station_name = getattr(tide_readings[0], "station_name", None)
    except Exception as exc:  # noqa: BLE001
        tide_error = str(exc)
        logger.warning(
            "briefing tide fetch failed venue={} date={} err={}",
            venue.venue_id,
            tick.local_date,
            exc,
        )

    if fetch_currents is not None:
        try:
            current_readings = list(
                await fetch_currents(
                    lat=venue.venue_lat,
                    lon=venue.venue_lon,
                    start_utc=anim_start,
                    end_utc=anim_end,
                )
            )
            if current_readings:
                tide_samples = _merge_currents(tide_samples, current_readings)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "briefing currents fetch failed venue={} date={} err={}",
                venue.venue_id,
                tick.local_date,
                exc,
            )

    # LuckGrib-style wind grid: fetch a 2D mesh of forecast samples for
    # the renderer. Failure is non-fatal — the renderer falls back to a
    # uniform wash from the venue's single point.
    grid_points_data: list[Any] = []
    if fetch_grid is not None:
        try:
            lats, lons = grid_points(venue)
            grid_points_data = list(
                await fetch_grid(
                    lats=lats,
                    lons=lons,
                    start_utc=anim_start,
                    end_utc=anim_end,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "briefing grid fetch failed venue={} date={} err={}",
                venue.venue_id,
                tick.local_date,
                exc,
            )

    source_urls: dict[str, str] = {"forecast": _forecast_url(venue)}
    if tide_station_id:
        source_urls["tide"] = f"{_NOAA_STATION_BASE_URL}{tide_station_id}"

    briefing = compose_briefing(
        venue=venue,
        local_date=tick.local_date,
        lead_hours=tick.lead_hours,
        forecast_samples=forecast_samples,
        tide_samples=tide_samples,
        source_urls=source_urls,
        forecast_issued_at=None,
        fetched_at=fetched_at,
        forecast_error=forecast_error,
        tide_error=tide_error,
        tide_station_id=tide_station_id,
        tide_station_name=tide_station_name,
    )

    # Race linking — only when the forecast succeeded. A Failed briefing
    # never auto-creates a Race row.
    race_id: int | None = None
    if briefing.state == "Generated":
        race_id = await _link_or_create_race(storage, venue, tick)
        briefing = _briefing_with_race_id(briefing, race_id)

    # Best-effort chart render. Path is set on the briefing only on success.
    if chart_renderer is not None and chart_dir is not None and briefing.state == "Generated":
        chart_dir.mkdir(parents=True, exist_ok=True)
        chart_filename = (
            f"{venue.venue_id}_{tick.local_date.isoformat()}_l{tick.lead_hours:02d}.gif"
        )
        chart_path = chart_dir / chart_filename

        # Pre-fetch OSM basemap tiles for the venue bbox. The cache lives
        # next to the chart_dir and survives across runs, so once a bbox
        # is rendered the tiles are free thereafter. A failure here is
        # non-fatal — the renderer falls back to a plain water background.
        basemap: tuple[Any, tuple[float, float, float, float]] | None = None
        try:
            from helmlog.osm_tiles import fetch_basemap

            tile_cache_dir = chart_dir.parent / "osm-cache"
            img, ext = await fetch_basemap(venue.map_bbox_deg, tile_cache_dir)
            if img is not None and ext is not None:
                basemap = (img, ext)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "briefing basemap fetch failed venue={} err={}",
                venue.venue_id,
                exc,
            )

        try:
            ok = chart_renderer(
                briefing,
                chart_path,
                grid=grid_points_data or None,
                basemap=basemap,
            )
            if ok and chart_path.exists():
                briefing = _briefing_with_chart_path(briefing, str(chart_path))
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "briefing chart render failed venue={} lead={}h err={}",
                venue.venue_id,
                tick.lead_hours,
                exc,
            )

    briefing_id = await storage.write_briefing(briefing)
    if race_id is not None:
        await storage.link_briefing_to_race(briefing_id=briefing_id, race_id=race_id)

    logger.info(
        "briefing {} venue={} date={} lead={}h state={} race={}",
        briefing_id,
        venue.venue_id,
        tick.local_date.isoformat(),
        tick.lead_hours,
        briefing.state,
        race_id,
    )
    return briefing


async def _link_or_create_race(storage: Storage, venue: VenueConfig, tick: BriefingTick) -> int:
    """Find an existing Race covering the racing window, or create a forecast one."""
    from helmlog.races import build_race_name

    existing = await storage.list_races_in_range(tick.window_start_utc, tick.window_end_utc)
    if existing:
        # Pick the first race whose start time matches the window most closely.
        return existing[0].id

    date_str = tick.window_start_utc.date().isoformat()
    same_day = await storage.list_races_for_date(date_str)
    race_num = sum(1 for r in same_day if r.session_type == "forecast") + 1
    name = build_race_name(
        event=venue.venue_name.replace(" ", ""),
        d=tick.local_date,
        race_num=race_num,
        session_type="forecast",
    )
    race = await storage.start_race(
        event=venue.venue_name.replace(" ", ""),
        start_utc=tick.window_start_utc,
        date_str=date_str,
        race_num=race_num,
        name=name,
        session_type="forecast",
    )
    # The forecast race is a placeholder — close it immediately so the
    # session_active flag doesn't trip elsewhere.
    await storage.end_race(race.id, tick.window_end_utc)
    return race.id


def _briefing_with_race_id(b: Briefing, race_id: int) -> Briefing:
    return Briefing(
        venue_id=b.venue_id,
        local_date=b.local_date,
        lead_hours=b.lead_hours,
        state=b.state,
        hourly_forecast=b.hourly_forecast,
        hourly_tide=b.hourly_tide,
        pressure_trend=b.pressure_trend,
        source_urls=dict(b.source_urls),
        forecast_issued_at=b.forecast_issued_at,
        fetched_at=b.fetched_at,
        error=b.error,
        tide_unavailable_reason=b.tide_unavailable_reason,
        tide_station_id=b.tide_station_id,
        tide_station_name=b.tide_station_name,
        chart_path=b.chart_path,
        race_id=race_id,
    )


# ---------------------------------------------------------------------------
# Animated GIF renderer (matplotlib + PillowWriter)
# ---------------------------------------------------------------------------


# GIF render resolution. With photo-like OSM tile content, the GIF file
# size scales near-linearly with pixel count (each frame quantizes its
# own palette). 900×500 @ 80 DPI gives a tight ~3 MB while still being
# legible — bumping to 1200×630 quadruples the file size.
_CHART_WIDTH_PX = 900
_CHART_HEIGHT_PX = 500
_CHART_DPI = 80
_GIF_FRAME_INTERVAL = timedelta(minutes=5)
_GIF_FRAME_FPS = 4  # ~250 ms per frame in the resulting GIF


def _frame_times(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Inclusive 5-minute frame timestamps from ``start_utc`` to ``end_utc``."""
    out: list[datetime] = []
    t = start_utc
    while t <= end_utc:
        out.append(t)
        t = t + _GIF_FRAME_INTERVAL
    return out


def _interp_at(samples: Sequence[HourlyForecastSample], at: datetime) -> tuple[float, float, float]:
    """Linearly interpolate (speed, gust, direction) at ``at`` from samples.

    Direction is unwrapped before interpolation so a 359°→2° step crosses
    the discontinuity smoothly. Returns (0, 0, direction-of-bracket-start)
    if ``at`` falls outside the sample range — those frames will simply
    show steady values rather than failing the render.
    """
    if not samples:
        return 0.0, 0.0, 0.0
    if at <= samples[0].timestamp_utc:
        s = samples[0]
        return s.wind_speed_kts, s.wind_gust_kts, s.wind_direction_deg
    if at >= samples[-1].timestamp_utc:
        s = samples[-1]
        return s.wind_speed_kts, s.wind_gust_kts, s.wind_direction_deg
    for i in range(len(samples) - 1):
        a, b = samples[i], samples[i + 1]
        if a.timestamp_utc <= at <= b.timestamp_utc:
            span = (b.timestamp_utc - a.timestamp_utc).total_seconds()
            if span <= 0:
                return a.wind_speed_kts, a.wind_gust_kts, a.wind_direction_deg
            f = (at - a.timestamp_utc).total_seconds() / span
            speed = a.wind_speed_kts + (b.wind_speed_kts - a.wind_speed_kts) * f
            gust = a.wind_gust_kts + (b.wind_gust_kts - a.wind_gust_kts) * f
            # Unwrap direction across the 0/360 discontinuity.
            d_a = a.wind_direction_deg
            d_b = b.wind_direction_deg
            delta = d_b - d_a
            if delta > 180:
                d_b -= 360
            elif delta < -180:
                d_b += 360
            direction = (d_a + (d_b - d_a) * f) % 360
            return speed, gust, direction
    s = samples[-1]
    return s.wind_speed_kts, s.wind_gust_kts, s.wind_direction_deg


def _interp_current(
    samples: Sequence[HourlyTideSample], at: datetime
) -> tuple[float | None, float | None]:
    """Linearly interpolate (speed_kts, set_deg) from current-bearing samples."""
    valid = [
        s for s in samples if s.current_speed_kts is not None and s.current_set_deg is not None
    ]
    if not valid:
        return None, None
    if at <= valid[0].timestamp_utc:
        return valid[0].current_speed_kts, valid[0].current_set_deg
    if at >= valid[-1].timestamp_utc:
        return valid[-1].current_speed_kts, valid[-1].current_set_deg
    for i in range(len(valid) - 1):
        a, b = valid[i], valid[i + 1]
        if a.timestamp_utc <= at <= b.timestamp_utc:
            span = (b.timestamp_utc - a.timestamp_utc).total_seconds()
            if span <= 0:
                return a.current_speed_kts, a.current_set_deg
            f = (at - a.timestamp_utc).total_seconds() / span
            assert a.current_speed_kts is not None
            assert b.current_speed_kts is not None
            assert a.current_set_deg is not None
            assert b.current_set_deg is not None
            speed = a.current_speed_kts + (b.current_speed_kts - a.current_speed_kts) * f
            d_a = a.current_set_deg
            d_b = b.current_set_deg
            delta = d_b - d_a
            if delta > 180:
                d_b -= 360
            elif delta < -180:
                d_b += 360
            set_deg = (d_a + (d_b - d_a) * f) % 360
            return speed, set_deg
    return None, None


# TWS speed→color bands. Mirrors `_twsColor` in src/helmlog/static/session.js
# (with the project's CSS theme variables resolved to literal hex). Keeping
# the briefing GIF consistent with the live session-page wind overlay so
# crews see the same color story in both places.
_TWS_BANDS: tuple[tuple[float, str], ...] = (
    (0.0, "#6b7280"),  # < 6 kt — text-muted
    (6.0, "#7eb8f7"),  # 6–7 — accent
    (8.0, "#2563eb"),  # 8–11 — accent-strong
    (12.0, "#7c3aed"),  # 12–13 — violet
    (14.0, "#fbbf24"),  # 14–15 — warning
    (16.0, "#f87171"),  # 16–19 — danger
    (20.0, "#991b1b"),  # 20+ — dark red
)


def _tws_color(tws: float) -> str:
    color = _TWS_BANDS[0][1]
    for threshold, hexc in _TWS_BANDS:
        if tws >= threshold:
            color = hexc
    return color


def _barb_features(tws_kts: float) -> tuple[int, int, int]:
    """Return (pennants, fulls, halves) for a wind speed.

    Speeds round to the nearest 5 kt before counting (standard met
    convention; matches ``_renderWindBarbSvg`` in session.js).
    """
    knots = int(round(tws_kts / 5) * 5)
    pennants, knots = divmod(knots, 50)
    fulls, knots = divmod(knots, 10)
    halves = knots // 5
    return pennants, fulls, halves


def _barb_local_geometry(
    tws_kts: float,
) -> tuple[list[tuple[float, float, float, float]], list[list[tuple[float, float]]], bool]:
    """Build a wind barb in local (X_along_shaft, Y_to_right) units.

    Returns (line_segments, pennant_polygons, is_calm). Units are
    "feature-steps" — the renderer scales these into world degrees so the
    visual size scales with the venue's grid step. Local layout mirrors
    session.js's ``_renderWindBarbSvg``: shaft from station outward,
    pennants at the tip, then full barbs, then half barbs, walking inward.
    """
    if tws_kts < 3:
        return [], [], True

    pennants, fulls, halves = _barb_features(tws_kts)
    shaft_len = 6.4  # 32 / step(5) in the SVG units
    segments: list[tuple[float, float, float, float]] = [(0.0, 0.0, shaft_len, 0.0)]
    polys: list[list[tuple[float, float]]] = []
    y = shaft_len  # walk inward from the tip
    for _ in range(pennants):
        polys.append([(y, 0.0), (y, 2.0), (y - 1.0, 0.0)])
        y -= 1.0
    if pennants > 0:
        y -= 0.2  # gap between pennants and full barbs
    for _ in range(fulls):
        segments.append((y, 0.0, y - 0.8, 2.0))
        y -= 1.0
    # A lone half barb sits one step in from the tip (matches session.js).
    if halves > 0 and fulls == 0 and pennants == 0:
        y -= 1.0
    for _ in range(halves):
        segments.append((y, 0.0, y - 0.4, 1.0))
        y -= 1.0
    return segments, polys, False


def _local_to_world(
    x_local: float,
    y_local: float,
    twd_deg: float,
    station_lon: float,
    station_lat: float,
    step_lat: float,
    cos_lat: float,
) -> tuple[float, float]:
    """Rotate a local barb point by TWD and place it in (lon, lat) space."""
    import math

    theta = math.radians(twd_deg)
    sin_t = math.sin(theta)
    cos_t = math.cos(theta)
    # forward (along shaft) = (sin θ, cos θ); right = (cos θ, -sin θ).
    dx = x_local * sin_t + y_local * cos_t  # uncompensated lon offset
    dy = x_local * cos_t - y_local * sin_t  # lat offset
    # set_aspect(1/cos_lat) means 1 lon-deg displays as cos_lat lat-degs;
    # divide lon offset by cos_lat to keep the barb visually rigid.
    cos_lat_safe = max(cos_lat, 0.1)
    return (
        station_lon + dx * step_lat / cos_lat_safe,
        station_lat + dy * step_lat,
    )


def _load_coastline(filename: str) -> list[list[tuple[float, float]]]:
    """Load coastline polygons as list of (lat, lon) ring lists.

    Returns ``[]`` if the file is missing or unparseable; the renderer
    falls back to a coastline-free map without erroring.
    """
    import importlib.resources as _r
    import json as _json

    try:
        text = (_r.files("helmlog") / "data" / "coastlines" / filename).read_text()
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        return []
    try:
        gj = _json.loads(text)
    except _json.JSONDecodeError:
        return []
    rings: list[list[tuple[float, float]]] = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        if gtype == "Polygon":
            for ring in coords:
                rings.append([(pt[1], pt[0]) for pt in ring])
        elif gtype == "MultiPolygon":
            for poly in coords:
                for ring in poly:
                    rings.append([(pt[1], pt[0]) for pt in ring])
    return rings


def render_animated_gif(
    briefing: Briefing,
    output_path: Path,
    *,
    grid: Sequence[Any] | None = None,
    basemap: tuple[Any, tuple[float, float, float, float]] | None = None,
) -> bool:
    """Render a LuckGrib-style animated GIF of the briefing window.

    Each frame is a 2D map of the venue area showing:

    - **OSM tile basemap** (when ``basemap`` is provided — matches the
      tiles the selection map shows). Falls back to a Natural Earth
      coastline polygon (or plain water-blue) if not.
    - **Wind heatmap** — per-grid-cell wind speed coloured (blue→red,
      0–25 kt). Falls back to a uniform wash from the venue's single
      forecast point if no ``grid`` is provided.
    - **Wind barbs** at each grid cell (matching session-page style),
      colour banded by TWS, shaft pointing toward the wind source.
    - **Currents arrow** at the venue location (NOAA 6-min interpolated).
    - **Time/condition badge** with frame UTC + venue local time, speed,
      direction, gust.

    Frames are emitted at 5-minute intervals. ``grid`` is a sequence of
    ``helmlog.external.GridForecastPoint`` (typed loosely to avoid the
    import). ``basemap`` is ``(PIL.Image, (left, right, bottom, top))``
    as returned by ``helmlog.osm_tiles.fetch_basemap``. Best-effort: a
    False return / exception is caught upstream and the briefing still
    persists.
    """
    import math

    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Polygon as MplPolygon
    from PIL import Image

    if not briefing.hourly_forecast:
        return False

    venue = get_venue(briefing.venue_id)
    if venue is None:
        return False

    anim_start, anim_end = _animation_window_utc(venue, briefing.local_date)
    frames = _frame_times(anim_start, anim_end)
    if not frames:
        return False

    venue_forecast = sorted(briefing.hourly_forecast, key=lambda s: s.timestamp_utc)
    tide = sorted(briefing.hourly_tide, key=lambda s: s.timestamp_utc)

    # Bbox + map geometry.
    lat_min, lon_min, lat_max, lon_max = venue.map_bbox_deg
    lat_center = (lat_min + lat_max) / 2

    # Grid samples: prefer the explicit grid, else synthesize a 1-cell grid
    # from the venue's hourly_forecast (uniform wind across the map).
    grid_cells: list[tuple[float, float, list[Any]]] = []
    if grid:
        for gp in grid:
            samples = list(getattr(gp, "samples", ()))
            if not samples:
                continue
            grid_cells.append((float(gp.lat), float(gp.lon), samples))
    if not grid_cells:
        grid_cells = [(venue.venue_lat, venue.venue_lon, list(venue_forecast))]

    fig, ax = plt.subplots(
        figsize=(_CHART_WIDTH_PX / _CHART_DPI, _CHART_HEIGHT_PX / _CHART_DPI),
        dpi=_CHART_DPI,
    )
    fig.patch.set_facecolor("#e8f0f5")
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    # Equirectangular: 1° lat ≠ 1° lon at this latitude. Stretch x so the
    # map isn't squished horizontally.
    cos_lat = math.cos(math.radians(lat_center))
    ax.set_aspect(1.0 / cos_lat if cos_lat > 0 else 1.0)
    ax.set_xlabel("lon")
    ax.set_ylabel("lat")
    ax.set_title(
        f"{_venue_display_name(briefing.venue_id)} — "
        f"{briefing.local_date.isoformat()} (lead {briefing.lead_hours} h)"
    )

    # Basemap. Prefer OSM tiles (matches the selection map); fall back to
    # the Natural Earth coastline polygon (Shilshole) or a plain water
    # wash. Web Mercator vs equirectangular distortion is negligible at
    # the venue scale we render (a few tens of km, mid-latitudes).
    if basemap is not None and basemap[0] is not None:
        bm_img, bm_extent = basemap
        # Soften the basemap so the wind data pops AND the GIF's per-frame
        # palette quantizer has fewer unique colours to chase (file size
        # roughly halves vs the raw tile imagery).
        try:
            from PIL import ImageEnhance

            bm_soft = ImageEnhance.Color(bm_img).enhance(0.35)  # ~mute saturation
            bm_soft = ImageEnhance.Contrast(bm_soft).enhance(0.85)  # flatten
            bm_soft = ImageEnhance.Brightness(bm_soft).enhance(1.08)  # lift slightly
        except Exception:  # noqa: BLE001
            bm_soft = bm_img
        ax.imshow(
            np.asarray(bm_soft),
            extent=bm_extent,
            origin="upper",
            aspect="auto",
            zorder=0,
            interpolation="bilinear",
        )
    else:
        ax.set_facecolor("#cfe1ec")  # water
        if venue.coastline_geojson:
            for ring in _load_coastline(venue.coastline_geojson):
                xy = [(lon, lat) for lat, lon in ring]
                ax.add_patch(
                    MplPolygon(xy, closed=True, facecolor="#dccfa6", edgecolor="#8a7d4a", lw=0.6)
                )

    # Heatmap setup. The colour scale is locked to the actual data range
    # for this briefing (computed once, applied to every frame) so the
    # same wind speed always maps to the same colour across the animation.
    # We use a single imshow with bilinear interpolation so the gradient
    # between adjacent grid cells is smooth, not stepped per cell.
    cmap = plt.get_cmap("RdYlBu_r")
    half = max(0.001, venue.grid_step_deg / 2)

    # Range derived from observed cell samples. Pad +1 kt so colours don't
    # saturate at the brightest cell, and floor the span so a glassy day
    # still has visible gradient.
    all_speeds = [s.wind_speed_kts for _lat, _lon, samples in grid_cells for s in samples]
    speed_min = 0.0
    observed_max = max(all_speeds) if all_speeds else 12.0
    speed_max = max(observed_max + 1.0, 8.0)

    unique_lats = sorted({lat for lat, _lon, _s in grid_cells})
    unique_lons = sorted({lon for _lat, lon, _s in grid_cells})
    n_lat = len(unique_lats)
    n_lon = len(unique_lons)
    lat_index = {lat: i for i, lat in enumerate(unique_lats)}
    lon_index = {lon: j for j, lon in enumerate(unique_lons)}
    cell_lookup: list[list[list[Any] | None]] = [[None] * n_lon for _ in range(n_lat)]
    for lat, lon, samples in grid_cells:
        cell_lookup[lat_index[lat]][lon_index[lon]] = samples
    heatmap_extent = (
        min(unique_lons) - half,
        max(unique_lons) + half,
        min(unique_lats) - half,
        max(unique_lats) + half,
    )
    heatmap_arr = np.zeros((n_lat, n_lon), dtype=float)
    heatmap_im = ax.imshow(
        heatmap_arr,
        extent=heatmap_extent,
        origin="lower",
        aspect="auto",
        cmap=cmap,
        vmin=speed_min,
        vmax=speed_max,
        alpha=0.45,
        interpolation="bilinear",
        zorder=0.5,
    )

    # Static colorbar legend.
    from matplotlib.colors import Normalize as _Normalize

    sm = plt.cm.ScalarMappable(
        cmap=cmap,
        norm=_Normalize(vmin=speed_min, vmax=speed_max),
    )
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, fraction=0.025, pad=0.02)
    cbar.set_label(f"wind (kt) — scale 0–{speed_max:.0f}", fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    # Per-frame wind barbs (matching the session-page track overlay style
    # in src/helmlog/static/session.js#_renderWindBarbSvg). Each cell gets
    # an SVG-style barb whose shaft points to the wind source, with
    # pennants/fulls/halves stacked from the tip inward and the line color
    # banded by TWS to mirror `_twsColor`. We rebuild the segment lists
    # each frame and update LineCollection / PolyCollection in place —
    # cheaper than tearing down and re-adding artists.
    from matplotlib.collections import LineCollection, PolyCollection

    barb_lines = LineCollection([], linewidths=1.4, capstyle="round", zorder=2)
    ax.add_collection(barb_lines)
    barb_pennants = PolyCollection(
        [], facecolors=[], edgecolors=[], linewidths=0.6, joinstyle="round", zorder=2
    )
    ax.add_collection(barb_pennants)
    # Static station dots (one per grid cell).
    ax.plot(
        [c[1] for c in grid_cells],
        [c[0] for c in grid_cells],
        ".",
        color="#1f2937",
        ms=2.5,
        zorder=2.5,
    )
    # Half-step in lat-degrees so the longest barb (~6.4 step) still fits
    # comfortably within a grid cell at any rotation.
    barb_step_lat = (venue.grid_step_deg * 0.45) / 6.4

    # Venue marker + currents arrow.
    ax.plot(
        venue.venue_lon,
        venue.venue_lat,
        "o",
        color="#d62728",
        ms=7,
        zorder=4,
        label=venue.venue_name,
    )
    current_arrow = ax.annotate(
        "",
        xy=(venue.venue_lon, venue.venue_lat),
        xytext=(venue.venue_lon, venue.venue_lat),
        arrowprops={"arrowstyle": "->", "color": "#005a9e", "lw": 2.5},
        zorder=3,
    )

    # Time/condition badge (top-left of map).
    badge = ax.text(
        0.02,
        0.97,
        "",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=10,
        family="monospace",
        bbox={"boxstyle": "round", "fc": "white", "ec": "#888", "alpha": 0.9},
        zorder=5,
    )

    # Currents arrow scales 1 kt ≈ ~half a grid step.
    cur_deg_per_kt = (venue.grid_step_deg * 0.45) / max(2.0, 1.0)

    def update(frame_idx: int) -> tuple[Any, ...]:
        t = frames[frame_idx]

        # Heatmap — fill the (n_lat, n_lon) array with this frame's wind
        # speeds. imshow's bilinear interpolation gives a smooth gradient
        # between cell centres rather than a hard step.
        for i in range(n_lat):
            row = cell_lookup[i]
            for j in range(n_lon):
                samples = row[j]
                if samples is None:
                    heatmap_arr[i, j] = 0.0
                    continue
                speed, _g, _d = _interp_at(samples, t)
                heatmap_arr[i, j] = speed
        heatmap_im.set_data(heatmap_arr)

        # Wind barbs — rebuild segment + pennant lists for this frame.
        all_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        all_seg_colors: list[str] = []
        all_polys: list[list[tuple[float, float]]] = []
        all_poly_colors: list[str] = []
        for lat, lon, samples in grid_cells:
            speed, _gust, direction = _interp_at(samples, t)
            color = _tws_color(speed)
            local_segments, local_polys, is_calm = _barb_local_geometry(speed)
            if is_calm:
                # Skip — the static station dot still marks the cell.
                continue
            for x0, y0, x1, y1 in local_segments:
                p0 = _local_to_world(x0, y0, direction, lon, lat, barb_step_lat, cos_lat)
                p1 = _local_to_world(x1, y1, direction, lon, lat, barb_step_lat, cos_lat)
                all_segments.append((p0, p1))
                all_seg_colors.append(color)
            for poly in local_polys:
                pts = [
                    _local_to_world(px, py, direction, lon, lat, barb_step_lat, cos_lat)
                    for px, py in poly
                ]
                all_polys.append(pts)
                all_poly_colors.append(color)
        barb_lines.set_segments(all_segments)
        barb_lines.set_color(all_seg_colors)
        barb_pennants.set_verts(all_polys)
        barb_pennants.set_facecolor(all_poly_colors)
        barb_pennants.set_edgecolor(all_poly_colors)

        # Currents arrow at the venue.
        cur_speed, cur_set = _interp_current(tide, t)
        if cur_speed is not None and cur_set is not None and cur_speed > 0:
            dlon = math.sin(math.radians(cur_set)) * cur_speed * cur_deg_per_kt / max(cos_lat, 0.1)
            dlat = math.cos(math.radians(cur_set)) * cur_speed * cur_deg_per_kt
            current_arrow.xy = (venue.venue_lon + dlon, venue.venue_lat + dlat)
        else:
            current_arrow.xy = (venue.venue_lon, venue.venue_lat)
        current_arrow.set_position((venue.venue_lon, venue.venue_lat))

        # Badge text — venue point conditions.
        v_speed, v_gust, v_dir = _interp_at(venue_forecast, t)
        local = t.astimezone(ZoneInfo(venue.venue_tz))
        cur_str = (
            f"{cur_speed:.2f} kt @ {int(round(cur_set))}°"
            if cur_speed is not None and cur_set is not None
            else "—"
        )
        badge.set_text(
            f"{local.strftime('%a %H:%M')} local  ({t.strftime('%H:%MZ')})\n"
            f"wind  {v_speed:.1f} kt @ {int(round(v_dir))}°  gust {v_gust:.0f}\n"
            f"curr  {cur_str}"
        )
        return (heatmap_im, barb_lines, barb_pennants, current_arrow, badge)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Render frames manually so we control the GIF palette. matplotlib's
    # default PillowWriter quantizes each frame independently, which
    # makes the static colorbar (and other unchanged pixels) shimmer
    # frame-to-frame as similar colours round to different palette
    # entries. We render every frame to RGB, build a single shared
    # palette from a sampling of those frames, then quantize each frame
    # against that master palette so static elements stay rock-stable.
    rendered: list[Image.Image] = []
    for i in range(len(frames)):
        update(i)
        fig.canvas.draw()
        w, h = fig.canvas.get_width_height()
        buf = np.asarray(fig.canvas.buffer_rgba())  # type: ignore[attr-defined]
        rendered.append(Image.fromarray(buf, "RGBA").convert("RGB"))
        _ = w, h
    plt.close(fig)

    if not rendered:
        return False

    # Build the master palette from ~8 evenly-spaced frames so it covers
    # the full wind range, not just the start-of-window state.
    n_samples = min(8, len(rendered))
    step = max(1, len(rendered) // n_samples)
    sample_frames = rendered[::step]
    sw, sh = sample_frames[0].size
    sheet = Image.new("RGB", (sw, sh * len(sample_frames)))
    for i, f in enumerate(sample_frames):
        sheet.paste(f, (0, i * sh))
    master = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT)

    quantized = [
        f.quantize(palette=master, dither=Image.Dither.FLOYDSTEINBERG) for f in rendered
    ]
    quantized[0].save(
        str(output_path),
        save_all=True,
        append_images=quantized[1:],
        duration=int(1000 / _GIF_FRAME_FPS),
        loop=0,
        optimize=True,
        disposal=2,
    )
    return output_path.exists() and output_path.stat().st_size > 0


def _venue_display_name(venue_id: str) -> str:
    venue = get_venue(venue_id)
    return venue.venue_name if venue is not None else venue_id


def _briefing_with_chart_path(b: Briefing, chart_path: str) -> Briefing:
    return Briefing(
        venue_id=b.venue_id,
        local_date=b.local_date,
        lead_hours=b.lead_hours,
        state=b.state,
        hourly_forecast=b.hourly_forecast,
        hourly_tide=b.hourly_tide,
        pressure_trend=b.pressure_trend,
        source_urls=dict(b.source_urls),
        forecast_issued_at=b.forecast_issued_at,
        fetched_at=b.fetched_at,
        error=b.error,
        tide_unavailable_reason=b.tide_unavailable_reason,
        tide_station_id=b.tide_station_id,
        tide_station_name=b.tide_station_name,
        chart_path=chart_path,
        race_id=b.race_id,
    )
