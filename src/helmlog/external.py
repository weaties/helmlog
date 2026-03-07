"""External data sources — tides, weather, etc.

Uses httpx.AsyncClient for all HTTP requests. ExternalFetcher must be used as
an async context manager to manage the shared HTTP client lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

if TYPE_CHECKING:
    from datetime import date, datetime

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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_NOAA_STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
_NOAA_PREDICTIONS_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class ExternalFetcher:
    """Fetches external environmental data from web APIs."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # NOAA tide station list — fetched once and reused for the session
        self._stations_cache: list[dict[str, Any]] | None = None

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
        logger.debug("fetch_weather: lat={:.4f} lon={:.4f} dt={}", lat, lon, dt)

        params: dict[str, Any] = {
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "current": "wind_speed_10m,wind_direction_10m,temperature_2m,surface_pressure",
            "wind_speed_unit": "kn",
            "timezone": "UTC",
        }
        try:
            resp = await self._http().get(_OPEN_METEO_URL, params=params)
            resp.raise_for_status()
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
        return reading
