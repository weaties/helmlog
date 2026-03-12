"""Tests for wind field visualization web endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _synthesize_session(client: httpx.AsyncClient) -> int:
    """Helper: synthesize a session and return its id."""
    resp = await client.post(
        "/api/sessions/synthesize",
        json={
            "course_type": "windward_leeward",
            "wind_direction": 180,
            "wind_speed_low": 10,
            "wind_speed_high": 12,
            "laps": 1,
            "start_lat": 47.63,
            "start_lon": -122.40,
            "seed": 42,
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_synthesize_persists_wind_params(storage: Storage) -> None:
    """Synthesizing a session persists wind field parameters."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _synthesize_session(client)

    params = await storage.get_synth_wind_params(sid)
    assert params is not None
    assert params["seed"] == 42
    assert params["base_twd"] == 180
    assert params["tws_low"] == 10
    assert params["tws_high"] == 12
    assert params["ref_lat"] == 47.63
    assert params["ref_lon"] == -122.40
    assert params["duration_s"] > 0


@pytest.mark.asyncio
async def test_synthesize_persists_course_marks(storage: Storage) -> None:
    """Synthesizing a session persists course mark positions."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _synthesize_session(client)

    marks = await storage.get_synth_course_marks(sid)
    assert len(marks) >= 3  # at least S, A, F
    keys = {m["mark_key"] for m in marks}
    assert "S" in keys
    assert "A" in keys
    assert "F" in keys
    for m in marks:
        assert m["lat"] != 0
        assert m["lon"] != 0


@pytest.mark.asyncio
async def test_session_detail_has_wind_field_flag(storage: Storage) -> None:
    """Session detail response includes has_wind_field for synthesized sessions."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _synthesize_session(client)
        resp = await client.get(f"/api/sessions/{sid}/detail")

    assert resp.status_code == 200
    assert resp.json()["has_wind_field"] is True


@pytest.mark.asyncio
async def test_wind_field_grid_endpoint(storage: Storage) -> None:
    """GET /api/sessions/{id}/wind-field returns a spatial grid."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _synthesize_session(client)
        resp = await client.get(f"/api/sessions/{sid}/wind-field?elapsed_s=60&grid_size=10")

    assert resp.status_code == 200
    data = resp.json()
    assert data["elapsed_s"] == 60.0
    assert data["duration_s"] > 0
    assert data["base_twd"] == 180
    grid = data["grid"]
    assert grid["rows"] == 10
    assert grid["cols"] == 10
    assert len(grid["cells"]) == 100
    cell = grid["cells"][0]
    assert "twd" in cell
    assert "tws" in cell
    assert "lat" in cell
    assert "lon" in cell
    # Marks are included
    assert len(data["marks"]) >= 3


@pytest.mark.asyncio
async def test_wind_field_grid_404_for_non_synth(storage: Storage) -> None:
    """Wind field endpoint returns 404 for non-synthesized sessions."""
    # Create a regular session
    from datetime import UTC, datetime

    race = await storage.start_race(
        "Test", datetime(2024, 1, 1, tzinfo=UTC), "2024-01-01", 1, "Race 1"
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race.id}/wind-field")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_wind_timeseries_endpoint(storage: Storage) -> None:
    """GET /api/sessions/{id}/wind-timeseries returns comparative data."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _synthesize_session(client)
        resp = await client.get(f"/api/sessions/{sid}/wind-timeseries?step_s=30")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["positions"]) == 3
    assert data["positions"][0]["label"] == "Port side"
    assert data["positions"][2]["label"] == "Starboard side"
    assert len(data["series"]) > 1
    # Each series entry has twd and tws arrays of length 3
    entry = data["series"][0]
    assert len(entry["twd"]) == 3
    assert len(entry["tws"]) == 3


@pytest.mark.asyncio
async def test_wind_field_spatial_variation(storage: Storage) -> None:
    """Wind field grid shows spatial variation across the course."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _synthesize_session(client)
        resp = await client.get(f"/api/sessions/{sid}/wind-field?elapsed_s=300&grid_size=10")

    data = resp.json()
    cells = data["grid"]["cells"]
    # TWD and TWS should not be identical across all cells
    twds = {c["twd"] for c in cells}
    twss = {c["tws"] for c in cells}
    assert len(twds) > 1, "Wind direction should vary spatially"
    assert len(twss) > 1, "Wind speed should vary spatially"


@pytest.mark.asyncio
async def test_wind_timeseries_divergence(storage: Storage) -> None:
    """Port and starboard time series show different wind conditions."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sid = await _synthesize_session(client)
        resp = await client.get(f"/api/sessions/{sid}/wind-timeseries?step_s=10")

    data = resp.json()
    # Check that port and starboard see different conditions
    twd_diffs = [abs(s["twd"][0] - s["twd"][2]) for s in data["series"]]
    max_diff = max(twd_diffs)
    assert max_diff > 0.5, "Port and starboard should see different TWD"
