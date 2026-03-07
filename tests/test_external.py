"""Tests for external.py — weather and tide fetching."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helmlog.external import ExternalFetcher, TideReading, WeatherReading

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LAT = 41.79
_LON = -71.87
_DT = datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC)

_OPEN_METEO_RESPONSE: dict[str, Any] = {
    "latitude": 41.8,
    "longitude": -71.875,
    "timezone": "UTC",
    "current_units": {
        "time": "iso8601",
        "wind_speed_10m": "kn",
        "wind_direction_10m": "°",
        "temperature_2m": "°C",
        "surface_pressure": "hPa",
    },
    "current": {
        "time": "2025-08-10T14:00",
        "wind_speed_10m": 12.5,
        "wind_direction_10m": 220.0,
        "temperature_2m": 22.3,
        "surface_pressure": 1013.2,
    },
}


def _make_mock_response(
    payload: dict[str, Any] | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else _OPEN_METEO_RESPONSE
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchWeather:
    async def test_returns_weather_reading_on_success(self) -> None:
        """A valid Open-Meteo response yields a WeatherReading with correct values."""
        mock_resp = _make_mock_response()

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is not None
        assert isinstance(reading, WeatherReading)
        assert reading.wind_speed_kts == pytest.approx(12.5)
        assert reading.wind_direction_deg == pytest.approx(220.0)
        assert reading.air_temp_c == pytest.approx(22.3)
        assert reading.pressure_hpa == pytest.approx(1013.2)
        assert reading.lat == _LAT
        assert reading.lon == _LON

    async def test_timestamp_is_utc(self) -> None:
        """The timestamp on the reading should be UTC-aware."""
        mock_resp = _make_mock_response()

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is not None
        assert reading.timestamp.tzinfo is not None
        assert reading.timestamp == datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC)

    async def test_returns_none_on_http_error(self) -> None:
        """Network / HTTP errors return None rather than raising."""
        async with ExternalFetcher() as fetcher:
            with patch.object(
                fetcher._client,  # type: ignore[union-attr]
                "get",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("connection refused"),
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_returns_none_on_http_status_error(self) -> None:
        """4xx/5xx responses return None rather than raising."""
        mock_resp = _make_mock_response(status_code=500)

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_returns_none_on_missing_key(self) -> None:
        """If the response JSON is missing expected fields, return None."""
        bad_payload: dict[str, Any] = {"current": {"time": "2025-08-10T14:00"}}
        mock_resp = _make_mock_response(payload=bad_payload)

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_returns_none_on_empty_response(self) -> None:
        """An empty JSON object returns None."""
        mock_resp = _make_mock_response(payload={})

        async with ExternalFetcher() as fetcher:
            with patch.object(  # type: ignore[union-attr]
                fetcher._client, "get", new_callable=AsyncMock, return_value=mock_resp
            ):
                reading = await fetcher.fetch_weather(_LAT, _LON, _DT)

        assert reading is None

    async def test_lat_lon_rounded_in_request(self) -> None:
        """Lat/lon are rounded to 4 decimal places in the API request."""
        mock_resp = _make_mock_response()
        captured_params: dict[str, Any] = {}

        async def _fake_get(
            url: str, *, params: dict[str, Any] | None = None, **_: object
        ) -> MagicMock:
            if params:
                captured_params.update(params)
            return mock_resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                await fetcher.fetch_weather(41.789999999, -71.870000001, _DT)

        assert captured_params.get("latitude") == round(41.789999999, 4)
        assert captured_params.get("longitude") == round(-71.870000001, 4)


class TestWeatherStorageRoundTrip:
    async def test_write_and_query(self, storage: object) -> None:
        """WeatherReadings persist and are retrievable by time range."""
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)

        reading = WeatherReading(
            timestamp=_DT,
            lat=_LAT,
            lon=_LON,
            wind_speed_kts=12.5,
            wind_direction_deg=220.0,
            air_temp_c=22.3,
            pressure_hpa=1013.2,
        )
        await storage.write_weather(reading)

        rows = await storage.query_weather_range(
            datetime(2025, 8, 10, 13, 0, 0, tzinfo=UTC),
            datetime(2025, 8, 10, 15, 0, 0, tzinfo=UTC),
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["wind_speed_kts"] == pytest.approx(12.5)
        assert row["wind_dir_deg"] == pytest.approx(220.0)
        assert row["air_temp_c"] == pytest.approx(22.3)
        assert row["pressure_hpa"] == pytest.approx(1013.2)

    async def test_query_outside_range_returns_empty(self, storage: object) -> None:
        """Query with a range that doesn't cover the reading returns empty."""
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)

        reading = WeatherReading(
            timestamp=_DT,
            lat=_LAT,
            lon=_LON,
            wind_speed_kts=12.5,
            wind_direction_deg=220.0,
            air_temp_c=22.3,
            pressure_hpa=1013.2,
        )
        await storage.write_weather(reading)

        rows = await storage.query_weather_range(
            datetime(2025, 8, 11, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 8, 11, 2, 0, 0, tzinfo=UTC),
        )
        assert rows == []


class TestLatestPosition:
    async def test_returns_none_when_empty(self, storage: object) -> None:
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)
        result = await storage.latest_position()
        assert result is None

    async def test_returns_most_recent(self, storage: object) -> None:
        from helmlog.nmea2000 import PGN_POSITION_RAPID, PositionRecord
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)
        early = datetime(2025, 8, 10, 13, 0, 0, tzinfo=UTC)
        late = datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC)
        await storage.write(PositionRecord(PGN_POSITION_RAPID, 5, early, 41.0, -71.0))
        await storage.write(PositionRecord(PGN_POSITION_RAPID, 5, late, 42.0, -72.0))

        result = await storage.latest_position()
        assert result is not None
        assert result["latitude_deg"] == pytest.approx(42.0)
        assert result["longitude_deg"] == pytest.approx(-72.0)


# ---------------------------------------------------------------------------
# NOAA tide station helpers
# ---------------------------------------------------------------------------

_STATIONS_RESPONSE: dict[str, Any] = {
    "stations": [
        {"id": "8461490", "name": "New London", "lat": 41.36, "lng": -72.09},
        {"id": "8452660", "name": "Newport", "lat": 41.505, "lng": -71.326},
        {"id": "9410170", "name": "San Diego", "lat": 32.714, "lng": -117.173},
    ]
}

_PREDICTIONS_RESPONSE: dict[str, Any] = {
    "predictions": [
        {"t": "2025-08-10 00:00", "v": "0.123"},
        {"t": "2025-08-10 01:00", "v": "0.456"},
        {"t": "2025-08-10 14:00", "v": "1.234"},
    ]
}

_TIDE_LAT = 41.79  # Narragansett Bay — nearest station should be Newport
_TIDE_LON = -71.87
_TIDE_DATE = date(2025, 8, 10)
_TIDE_DT = datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC)


def _make_tide_mock_responses(
    stations_payload: dict[str, Any] | None = None,
    predictions_payload: dict[str, Any] | None = None,
    stations_status: int = 200,
    predictions_status: int = 200,
) -> list[MagicMock]:
    """Build two sequential mock responses: stations list then predictions."""

    def _resp(payload: dict[str, Any], status: int) -> MagicMock:
        r = MagicMock(spec=httpx.Response)
        r.status_code = status
        r.json.return_value = payload
        if status >= 400:
            r.raise_for_status.side_effect = httpx.HTTPStatusError(
                f"HTTP {status}", request=MagicMock(), response=r
            )
        else:
            r.raise_for_status.return_value = None
        return r

    return [
        _resp(stations_payload or _STATIONS_RESPONSE, stations_status),
        _resp(predictions_payload or _PREDICTIONS_RESPONSE, predictions_status),
    ]


class TestFetchTidePredictions:
    async def test_returns_list_of_readings(self) -> None:
        """A valid NOAA response yields a list of TideReadings."""
        responses = _make_tide_mock_responses()
        call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                readings = await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)

        assert len(readings) == 3
        assert all(isinstance(r, TideReading) for r in readings)

    async def test_heights_parsed_correctly(self) -> None:
        """Height values from the predictions JSON are parsed as floats."""
        responses = _make_tide_mock_responses()
        call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                readings = await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)

        assert readings[0].height_m == pytest.approx(0.123)
        assert readings[1].height_m == pytest.approx(0.456)
        assert readings[2].height_m == pytest.approx(1.234)

    async def test_timestamps_are_utc(self) -> None:
        """Parsed timestamps are UTC-aware."""
        responses = _make_tide_mock_responses()
        call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                readings = await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)

        for r in readings:
            assert r.timestamp.tzinfo is not None
            assert r.type == "prediction"

    async def test_nearest_station_selected(self) -> None:
        """The station nearest to the query point is used."""
        responses = _make_tide_mock_responses()
        captured_params: dict[str, Any] = {}
        call_count = 0

        async def _fake_get(
            url: str, *, params: dict[str, Any] | None = None, **_: object
        ) -> MagicMock:
            nonlocal call_count
            if call_count == 1 and params:  # second call = predictions
                captured_params.update(params)
            resp = responses[call_count]
            call_count += 1
            return resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)

        # New London (41.36, -72.09) is closer to (41.79, -71.87) than Newport (41.505, -71.326)
        # d²(New London) ≈ 0.43²+0.22² = 0.233, d²(Newport) ≈ 0.285²+0.544² = 0.377
        assert captured_params.get("station") == "8461490"

    async def test_stations_cached_on_second_call(self) -> None:
        """Station list is fetched only once across multiple calls."""
        get_call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal get_call_count
            get_call_count += 1
            r = MagicMock(spec=httpx.Response)
            r.raise_for_status.return_value = None
            if "stations" in url:
                r.json.return_value = _STATIONS_RESPONSE
            else:
                r.json.return_value = _PREDICTIONS_RESPONSE
            return r

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)
                await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)

        # 1 stations call + 2 predictions calls (one per fetch)
        assert get_call_count == 3

    async def test_returns_empty_on_predictions_http_error(self) -> None:
        """HTTP error on the predictions call returns an empty list."""
        station_resp = MagicMock(spec=httpx.Response)
        station_resp.raise_for_status.return_value = None
        station_resp.json.return_value = _STATIONS_RESPONSE
        call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return station_resp
            raise httpx.ConnectError("timeout")

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                readings = await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)

        assert readings == []

    async def test_returns_empty_on_noaa_api_error(self) -> None:
        """NOAA API error field (e.g., bad station) returns an empty list."""
        error_payload: dict[str, Any] = {"error": {"message": "No data was found."}}
        responses = _make_tide_mock_responses(predictions_payload=error_payload)
        call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                readings = await fetcher.fetch_tide_predictions(_TIDE_LAT, _TIDE_LON, _TIDE_DATE)

        assert readings == []


class TestFetchTides:
    async def test_returns_reading_for_matching_hour(self) -> None:
        """fetch_tides() returns the prediction for the exact UTC hour."""
        responses = _make_tide_mock_responses()
        call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                reading = await fetcher.fetch_tides(_TIDE_LAT, _TIDE_LON, _TIDE_DT)

        assert reading is not None
        assert reading.timestamp.hour == 14
        assert reading.height_m == pytest.approx(1.234)

    async def test_returns_none_for_missing_hour(self) -> None:
        """fetch_tides() returns None if no prediction exists for that hour."""
        responses = _make_tide_mock_responses()
        call_count = 0

        async def _fake_get(url: str, **_: object) -> MagicMock:
            nonlocal call_count
            resp = responses[call_count]
            call_count += 1
            return resp

        # Hour 5 is not in the mock predictions (only 0, 1, 14)
        dt_no_data = datetime(2025, 8, 10, 5, 0, 0, tzinfo=UTC)
        async with ExternalFetcher() as fetcher:
            with patch.object(fetcher._client, "get", side_effect=_fake_get):  # type: ignore[union-attr]
                reading = await fetcher.fetch_tides(_TIDE_LAT, _TIDE_LON, dt_no_data)

        assert reading is None


class TestTideStorageRoundTrip:
    async def test_write_and_query(self, storage: object) -> None:
        """TideReadings persist and are retrievable by time range."""
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)

        reading = TideReading(
            timestamp=_TIDE_DT,
            height_m=1.234,
            type="prediction",
            station_id="8452660",
            station_name="Newport",
        )
        await storage.write_tide(reading)

        rows = await storage.query_tide_range(
            datetime(2025, 8, 10, 13, 0, 0, tzinfo=UTC),
            datetime(2025, 8, 10, 15, 0, 0, tzinfo=UTC),
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["height_m"] == pytest.approx(1.234)
        assert row["station_id"] == "8452660"
        assert row["station_name"] == "Newport"
        assert row["type"] == "prediction"

    async def test_write_is_idempotent(self, storage: object) -> None:
        """Writing the same reading twice produces only one row (INSERT OR IGNORE)."""
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)

        reading = TideReading(
            timestamp=_TIDE_DT,
            height_m=1.234,
            type="prediction",
            station_id="8452660",
            station_name="Newport",
        )
        await storage.write_tide(reading)
        await storage.write_tide(reading)

        rows = await storage.query_tide_range(
            datetime(2025, 8, 10, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 8, 10, 23, 59, 59, tzinfo=UTC),
        )
        assert len(rows) == 1

    async def test_query_outside_range_returns_empty(self, storage: object) -> None:
        """Query with a range that doesn't cover the reading returns empty."""
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)

        reading = TideReading(
            timestamp=_TIDE_DT,
            height_m=1.234,
            type="prediction",
            station_id="8452660",
            station_name="Newport",
        )
        await storage.write_tide(reading)

        rows = await storage.query_tide_range(
            datetime(2025, 8, 11, 0, 0, 0, tzinfo=UTC),
            datetime(2025, 8, 11, 23, 59, 59, tzinfo=UTC),
        )
        assert rows == []
