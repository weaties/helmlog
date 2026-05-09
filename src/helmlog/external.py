"""External data sources — tides, weather, etc.

Uses httpx.AsyncClient for all HTTP requests. ExternalFetcher must be used as
an async context manager to manage the shared HTTP client lifetime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

if TYPE_CHECKING:
    from datetime import date, datetime

    from helmlog.cache import WebCache

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TideReading:
    """A single hourly tide height prediction from NOAA CO-OPS."""

    timestamp: datetime  # UTC time of the reading (truncated to hour)
    height_m: float  # metres above MLLW chart datum
    type: str  # "prediction" | "observation"
    station_id: str  # NOAA station ID, e.g. "8461490"
    station_name: str  # Human-readable station name


@dataclass(frozen=True)
class WeatherReading:
    """A single weather observation from Open-Meteo (hourly resolution)."""

    timestamp: datetime  # UTC time of the reading (truncated to hour)
    lat: float  # latitude used for the query
    lon: float  # longitude used for the query
    wind_speed_kts: float  # 10 m wind speed in knots
    wind_direction_deg: float  # 10 m wind direction (degrees true, 0 = N)
    air_temp_c: float  # 2 m air temperature (°C)
    pressure_hpa: float  # surface pressure (hPa)


@dataclass(frozen=True)
class CurrentReading:
    """A single 6-min tidal-current prediction from NOAA CO-OPS."""

    timestamp: datetime  # UTC time of the prediction
    speed_kts: float  # current speed (knots, always >= 0)
    set_deg: float  # direction current is flowing toward (degrees true)
    station_id: str
    station_name: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_NOAA_STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
_NOAA_PREDICTIONS_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


def external_data_enabled() -> bool:
    """Check if external data fetching is enabled (#209).

    Defaults to True. Set EXTERNAL_DATA_ENABLED=false to disable.
    When disabled, GPS position is not sent to external APIs.
    """
    import os

    return os.environ.get("EXTERNAL_DATA_ENABLED", "true").lower() != "false"


def external_data_should_fetch() -> bool:
    """Check if external data fetching should proceed (#403).

    Returns False if EXTERNAL_DATA_ENABLED=false OR METERED=true.
    Use this instead of external_data_enabled() when deciding whether
    to launch weather/tide background tasks.
    """
    import os

    if not external_data_enabled():
        return False
    return os.environ.get("METERED", "false").lower() != "true"


def _safe_float(val: object) -> float | None:
    """Coerce to float; return None if val is None / empty / non-numeric."""
    if val is None or val == "":
        return None
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _reduce_precision(val: float, decimals: int = 2) -> float:
    """Reduce GPS coordinate precision for external API calls (#209).

    0.01° ≈ 1.1 km — sufficient for weather/tide lookups.
    """
    return round(val, decimals)


def _track_response(component: str, resp: httpx.Response) -> None:
    """Record bandwidth for an httpx response (best-effort)."""
    try:
        from helmlog.bandwidth import track_httpx_response

        track_httpx_response(component, resp)
    except Exception:  # noqa: BLE001
        pass


class ExternalFetcher:
    """Fetches external environmental data from web APIs."""

    def __init__(self, cache: WebCache | None = None) -> None:
        self._client: httpx.AsyncClient | None = None
        # NOAA tide station list — fetched once and reused for the session
        self._stations_cache: list[dict[str, Any]] | None = None
        # NOAA currents prediction station list — separate API endpoint.
        self._current_stations_cache: list[dict[str, Any]] | None = None
        # Optional T2 cache for successful API responses (#594 / #610).
        # Weather entries expire after 1h; tides never expire (predictions
        # for a given date are immutable once published).
        self._cache: WebCache | None = cache

    async def __aenter__(self) -> ExternalFetcher:
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ExternalFetcher must be used as an async context manager")
        return self._client

    # ------------------------------------------------------------------
    # Tides — NOAA CO-OPS
    # ------------------------------------------------------------------

    async def _get_tide_stations(self) -> list[dict[str, Any]]:
        """Fetch (and cache for the session) the NOAA tide prediction station list."""
        if self._stations_cache is not None:
            return self._stations_cache

        try:
            resp = await self._http().get(_NOAA_STATIONS_URL, params={"type": "tidepredictions"})
            resp.raise_for_status()
            _track_response("tides", resp)
            data: dict[str, Any] = resp.json()
            self._stations_cache = data["stations"]
            logger.debug("Fetched {} NOAA tide stations", len(self._stations_cache))
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("Failed to fetch NOAA tide station list: {}", exc)
            self._stations_cache = []

        return self._stations_cache or []

    @staticmethod
    def _nearest_station(
        stations: list[dict[str, Any]], lat: float, lon: float
    ) -> dict[str, Any] | None:
        """Return the nearest station by Euclidean distance in degrees."""
        if not stations:
            return None
        return min(
            stations,
            key=lambda s: (float(s["lat"]) - lat) ** 2 + (float(s["lng"]) - lon) ** 2,
        )

    async def fetch_tide_predictions(
        self,
        lat: float,
        lon: float,
        for_date: date,
    ) -> list[TideReading]:
        """Fetch all hourly tide predictions for a given UTC date from NOAA CO-OPS.

        Finds the nearest tide prediction station to (lat, lon) and requests
        hourly predictions in GMT for the whole day. Heights are in metres
        above MLLW datum.

        This covers US coastal waters. Returns an empty list if the fetch
        fails or no station data is available.

        Args:
            lat:      Latitude in decimal degrees.
            lon:      Longitude in decimal degrees.
            for_date: The UTC date to fetch predictions for.

        Returns:
            A list of TideReadings (up to 24), or an empty list on failure.
        """
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        # T2 cache lookup (#610). Predictions for a station+date are
        # immutable once published, so there's no TTL — cache-forever is
        # correct. Key by (lat, lon, date) rather than station id because
        # the nearest-station lookup is the slow-moving input.
        cache_hash = f"{_reduce_precision(lat)},{_reduce_precision(lon)}:{for_date.isoformat()}"
        if self._cache is not None:
            cached = await self._cache.t2_get_global("tides", data_hash=cache_hash)
            if isinstance(cached, list):
                try:
                    return [
                        TideReading(
                            timestamp=_datetime.fromisoformat(r["timestamp"]),
                            height_m=float(r["height_m"]),
                            type=str(r["type"]),
                            station_id=str(r["station_id"]),
                            station_name=str(r["station_name"]),
                        )
                        for r in cached
                    ]
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Tide cache decode failed, refetching: {}", exc)

        stations = await self._get_tide_stations()
        station = self._nearest_station(stations, lat, lon)
        if station is None:
            logger.warning("No NOAA tide stations available")
            return []

        station_id: str = str(station["id"])
        station_name: str = str(station["name"])
        date_str = for_date.strftime("%Y%m%d")

        logger.debug(
            "Fetching tide predictions: station={!r} ({}) date={}",
            station_name,
            station_id,
            date_str,
        )

        try:
            resp = await self._http().get(
                _NOAA_PREDICTIONS_URL,
                params={
                    "product": "predictions",
                    "application": "helmlog",
                    "begin_date": date_str,
                    "end_date": date_str,
                    "datum": "MLLW",
                    "station": station_id,
                    "time_zone": "gmt",
                    "interval": "h",
                    "units": "metric",
                    "format": "json",
                },
            )
            resp.raise_for_status()
            _track_response("tides", resp)
            data: dict[str, Any] = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("Tide predictions fetch failed: {}", exc)
            return []

        if "error" in data:
            logger.warning(
                "NOAA API error for station {}: {}",
                station_id,
                data["error"].get("message", "unknown"),
            )
            return []

        readings: list[TideReading] = []
        try:
            for p in data["predictions"]:
                ts = _datetime.fromisoformat(p["t"].replace(" ", "T")).replace(tzinfo=_UTC)
                readings.append(
                    TideReading(
                        timestamp=ts,
                        height_m=float(p["v"]),
                        type="prediction",
                        station_id=station_id,
                        station_name=station_name,
                    )
                )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Tide prediction parse error: {}", exc)
            return []

        logger.info(
            "Tide predictions: {} hourly readings from {!r} ({})",
            len(readings),
            station_name,
            station_id,
        )

        if self._cache is not None and readings:
            blob = [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "height_m": r.height_m,
                    "type": r.type,
                    "station_id": r.station_id,
                    "station_name": r.station_name,
                }
                for r in readings
            ]
            await self._cache.t2_put_global(
                "tides", data_hash=cache_hash, value=blob, ttl_seconds=None
            )

        return readings

    async def fetch_tides(
        self,
        lat: float,
        lon: float,
        dt: datetime,
    ) -> TideReading | None:
        """Return the tide prediction for the given UTC datetime (hour precision).

        Fetches the full day of predictions and returns the one matching the
        hour of dt. Returns None if the fetch fails or no matching hour is found.

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.
            dt:  UTC datetime for the tide reading.

        Returns:
            A TideReading for the matching hour, or None.
        """
        readings = await self.fetch_tide_predictions(lat, lon, dt.date())
        for r in readings:
            if r.timestamp.hour == dt.hour:
                return r
        return None

    # ------------------------------------------------------------------
    # Tidal currents — NOAA CO-OPS (#700 GIF expansion)
    # ------------------------------------------------------------------

    async def _get_current_stations(self) -> list[dict[str, Any]]:
        if self._current_stations_cache is not None:
            return self._current_stations_cache
        try:
            resp = await self._http().get(_NOAA_STATIONS_URL, params={"type": "currentpredictions"})
            resp.raise_for_status()
            _track_response("tides", resp)
            data: dict[str, Any] = resp.json()
            self._current_stations_cache = data["stations"]
            logger.debug("Fetched {} NOAA currents stations", len(self._current_stations_cache))
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("Failed to fetch NOAA currents station list: {}", exc)
            self._current_stations_cache = []
        return self._current_stations_cache or []

    async def fetch_current_predictions(
        self,
        *,
        lat: float,
        lon: float,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[CurrentReading]:
        """Fetch 6-minute NOAA tidal-current predictions for ``[start_utc, end_utc]``.

        Picks the nearest currentpredictions station to (lat, lon). Returns
        an empty list on any failure — callers treat as "currents
        unavailable" without raising. Speeds are always >= 0; the set
        direction is the direction the water is flowing toward (degrees
        true). NOAA returns negative ``Velocity_Major`` for ebb; we
        normalize to (speed, set) using the station's flood/ebb directions.
        """
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        lat = _reduce_precision(lat)
        lon = _reduce_precision(lon)

        stations = await self._get_current_stations()
        station = self._nearest_station(stations, lat, lon)
        if station is None:
            return []

        station_id = str(station["id"])
        station_name = str(station.get("name", ""))
        # NOAA's stations.json doesn't always carry meanFloodDir/meanEbbDir
        # at top level for currents; we'll fall back to abs(velocity) and
        # use Velocity-derived direction from the per-prediction payload.
        flood_dir = _safe_float(station.get("meanFloodDir"))
        ebb_dir = _safe_float(station.get("meanEbbDir"))

        params: dict[str, Any] = {
            "product": "currents_predictions",
            "application": "helmlog",
            "begin_date": start_utc.strftime("%Y%m%d %H:%M"),
            "end_date": end_utc.strftime("%Y%m%d %H:%M"),
            "station": station_id,
            "time_zone": "gmt",
            "interval": "6",
            "units": "english",
            "format": "json",
        }
        try:
            resp = await self._http().get(_NOAA_PREDICTIONS_URL, params=params)
            resp.raise_for_status()
            _track_response("tides", resp)
            data: dict[str, Any] = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("Currents predictions fetch failed: {}", exc)
            return []

        if "error" in data:
            logger.warning(
                "NOAA currents API error for station {}: {}",
                station_id,
                data["error"].get("message", "unknown"),
            )
            return []

        cps = data.get("current_predictions", {}).get("cp") or []
        out: list[CurrentReading] = []
        for cp in cps:
            try:
                ts_raw = str(cp["Time"]).replace(" ", "T")
                ts = _datetime.fromisoformat(ts_raw).replace(tzinfo=_UTC)
                vel = float(cp["Velocity_Major"])
            except (KeyError, ValueError, TypeError):
                continue
            if not (start_utc <= ts <= end_utc):
                continue
            speed = abs(vel)
            # Direction the current flows toward. Use station-level flood/
            # ebb directions when available; otherwise fall back to whatever
            # Direction the per-prediction payload carries.
            if vel >= 0 and flood_dir is not None:
                set_deg: float = flood_dir
            elif vel < 0 and ebb_dir is not None:
                set_deg = ebb_dir
            else:
                d = _safe_float(cp.get("Direction"))
                if d is None:
                    continue
                set_deg = d
            out.append(
                CurrentReading(
                    timestamp=ts,
                    speed_kts=speed,
                    set_deg=set_deg % 360.0,
                    station_id=station_id,
                    station_name=station_name,
                )
            )
        logger.info(
            "Currents predictions: {} samples from {!r} ({})",
            len(out),
            station_name,
            station_id,
        )
        return out

    # ------------------------------------------------------------------
    # 15-minute forecast — Open-Meteo (#700 GIF expansion)
    # ------------------------------------------------------------------

    async def fetch_minutely_15_forecast(
        self,
        *,
        lat: float,
        lon: float,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[Any]:
        """Fetch a 15-minute resolution forecast slice covering the window.

        Returns a list of ``helmlog.briefings.HourlyForecastSample``
        (the dataclass name predates this denser resolution; the shape is
        identical). Empty list on failure — callers treat as "forecast
        unavailable" without raising.
        """
        from helmlog.briefings import HourlyForecastSample

        lat = _reduce_precision(lat)
        lon = _reduce_precision(lon)
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "minutely_15": (
                "wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
                "temperature_2m,precipitation_probability,cloud_cover,surface_pressure"
            ),
            "wind_speed_unit": "kn",
            "timezone": "UTC",
            "start_date": start_utc.date().isoformat(),
            "end_date": end_utc.date().isoformat(),
        }
        try:
            resp = await self._http().get(_OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            _track_response("weather", resp)
            data: dict[str, Any] = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("Minutely-15 forecast fetch failed: {}", exc)
            return []

        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        try:
            block = data["minutely_15"]
            times = block["time"]
            speeds = block["wind_speed_10m"]
            dirs = block["wind_direction_10m"]
            gusts = block["wind_gusts_10m"]
            temps = block["temperature_2m"]
            precips = block["precipitation_probability"]
            clouds = block["cloud_cover"]
            pressures = block["surface_pressure"]
        except (KeyError, TypeError) as exc:
            logger.warning("Minutely-15 forecast parse error: {}", exc)
            return []

        out: list[HourlyForecastSample] = []
        for i, t in enumerate(times):
            try:
                ts = _datetime.fromisoformat(t).replace(tzinfo=_UTC)
            except (TypeError, ValueError):
                continue
            if not (start_utc <= ts <= end_utc):
                continue
            try:
                out.append(
                    HourlyForecastSample(
                        timestamp_utc=ts,
                        wind_speed_kts=float(speeds[i]),
                        wind_gust_kts=float(gusts[i]) if gusts[i] is not None else float(speeds[i]),
                        wind_direction_deg=float(dirs[i]),
                        air_temp_c=float(temps[i]) if temps[i] is not None else 0.0,
                        pressure_hpa=float(pressures[i]) if pressures[i] is not None else 0.0,
                        precip_probability_pct=float(precips[i]) if precips[i] is not None else 0.0,
                        cloud_cover_pct=float(clouds[i]) if clouds[i] is not None else 0.0,
                    )
                )
            except (TypeError, ValueError, IndexError) as exc:
                logger.debug("Minutely-15 forecast skipped row {}: {}", i, exc)
                continue
        return out

    # ------------------------------------------------------------------
    # Hourly forecast — Open-Meteo (#700)
    # ------------------------------------------------------------------

    async def fetch_hourly_forecast(
        self,
        *,
        lat: float,
        lon: float,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[Any]:
        """Fetch an hourly forecast slice covering ``[start_utc, end_utc]``.

        Returns a list of ``helmlog.briefings.HourlyForecastSample``. Empty
        list on failure (callers treat as "forecast unavailable" — never
        raises). Used by the pre-race briefing job (#700).
        """
        from helmlog.briefings import HourlyForecastSample

        lat = _reduce_precision(lat)
        lon = _reduce_precision(lon)
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": (
                "wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
                "temperature_2m,precipitation_probability,cloud_cover,surface_pressure"
            ),
            "wind_speed_unit": "kn",
            "timezone": "UTC",
            "start_date": start_utc.date().isoformat(),
            "end_date": end_utc.date().isoformat(),
        }
        try:
            resp = await self._http().get(_OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            _track_response("weather", resp)
            data: dict[str, Any] = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("Hourly forecast fetch failed: {}", exc)
            return []

        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        try:
            hourly = data["hourly"]
            times = hourly["time"]
            speeds = hourly["wind_speed_10m"]
            dirs = hourly["wind_direction_10m"]
            gusts = hourly["wind_gusts_10m"]
            temps = hourly["temperature_2m"]
            precips = hourly["precipitation_probability"]
            clouds = hourly["cloud_cover"]
            pressures = hourly["surface_pressure"]
        except (KeyError, TypeError) as exc:
            logger.warning("Hourly forecast parse error: {}", exc)
            return []

        out: list[HourlyForecastSample] = []
        for i, t in enumerate(times):
            try:
                ts = _datetime.fromisoformat(t).replace(tzinfo=_UTC)
            except (TypeError, ValueError):
                continue
            if not (start_utc <= ts <= end_utc):
                continue
            try:
                out.append(
                    HourlyForecastSample(
                        timestamp_utc=ts,
                        wind_speed_kts=float(speeds[i]),
                        wind_gust_kts=float(gusts[i]) if gusts[i] is not None else float(speeds[i]),
                        wind_direction_deg=float(dirs[i]),
                        air_temp_c=float(temps[i]),
                        pressure_hpa=float(pressures[i]),
                        precip_probability_pct=float(precips[i]) if precips[i] is not None else 0.0,
                        cloud_cover_pct=float(clouds[i]) if clouds[i] is not None else 0.0,
                    )
                )
            except (TypeError, ValueError, IndexError) as exc:
                logger.debug("Hourly forecast skipped row {}: {}", i, exc)
                continue
        return out

    # ------------------------------------------------------------------
    # Weather — Open-Meteo
    # ------------------------------------------------------------------

    async def fetch_weather(
        self,
        lat: float,
        lon: float,
        dt: datetime,
    ) -> WeatherReading | None:
        """Fetch current weather from Open-Meteo for the given location.

        Calls the /v1/forecast endpoint with the `current` parameter to get
        wind speed (kts), wind direction, air temperature, and surface pressure
        for the current UTC hour.

        Args:
            lat: Latitude in decimal degrees.
            lon: Longitude in decimal degrees.
            dt:  UTC datetime (used only for logging; Open-Meteo returns the
                 current hour regardless).

        Returns:
            A WeatherReading, or None if the request fails or the response
            cannot be parsed.
        """
        # Reduce GPS precision for external API calls (#209)
        lat = _reduce_precision(lat)
        lon = _reduce_precision(lon)
        logger.debug("fetch_weather: lat={:.2f} lon={:.2f} dt={}", lat, lon, dt)

        # T2 cache lookup (#610). The data_hash encodes the location + hour
        # so the cache naturally invalidates when the hour boundary is
        # crossed (Open-Meteo's ``current`` block advances hourly). TTL 1h
        # is a belt-and-suspenders safety net against clock skew or
        # missed hour rollovers.
        hour_key = dt.strftime("%Y-%m-%dT%H")
        cache_hash = f"{lat:.2f},{lon:.2f}:{hour_key}"
        if self._cache is not None:
            cached = await self._cache.t2_get_global("weather", data_hash=cache_hash)
            if cached is not None:
                try:
                    from datetime import datetime as _dt

                    ts_iso = cached["timestamp"] if isinstance(cached, dict) else None
                    if isinstance(cached, dict) and isinstance(ts_iso, str):
                        return WeatherReading(
                            timestamp=_dt.fromisoformat(ts_iso),
                            lat=float(cached["lat"]),
                            lon=float(cached["lon"]),
                            wind_speed_kts=float(cached["wind_speed_kts"]),
                            wind_direction_deg=float(cached["wind_direction_deg"]),
                            air_temp_c=float(cached["air_temp_c"]),
                            pressure_hpa=float(cached["pressure_hpa"]),
                        )
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Weather cache decode failed, refetching: {}", exc)

        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "current": "wind_speed_10m,wind_direction_10m,temperature_2m,surface_pressure",
            "wind_speed_unit": "kn",
            "timezone": "UTC",
        }
        try:
            resp = await self._http().get(_OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            _track_response("weather", resp)
        except httpx.HTTPError as exc:
            logger.warning("Weather fetch failed: {}", exc)
            return None

        try:
            data: dict[str, Any] = resp.json()
            current = data["current"]
            from datetime import UTC as _UTC
            from datetime import datetime as _datetime

            ts = _datetime.fromisoformat(current["time"]).replace(tzinfo=_UTC)
            reading = WeatherReading(
                timestamp=ts,
                lat=lat,
                lon=lon,
                wind_speed_kts=float(current["wind_speed_10m"]),
                wind_direction_deg=float(current["wind_direction_10m"]),
                air_temp_c=float(current["temperature_2m"]),
                pressure_hpa=float(current["surface_pressure"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Weather response parse error: {}", exc)
            return None

        logger.info(
            "Weather: {:.1f} kts from {:.0f}°, {:.1f}°C, {:.0f} hPa",
            reading.wind_speed_kts,
            reading.wind_direction_deg,
            reading.air_temp_c,
            reading.pressure_hpa,
        )

        if self._cache is not None:
            blob = asdict(reading)
            blob["timestamp"] = reading.timestamp.isoformat()
            await self._cache.t2_put_global(
                "weather", data_hash=cache_hash, value=blob, ttl_seconds=3600.0
            )

        return reading
