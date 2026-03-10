"""Tests for the synthesize race web endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest.mark.asyncio
async def test_synthesize_creates_session(storage: Storage) -> None:
    """POST /api/sessions/synthesize creates a synthesized session."""
    # Set up an event so naming works
    await storage.set_daily_event("2026-03-10", "TestRegatta")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
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
    data = resp.json()
    assert data["points"] > 100
    assert data["duration_s"] > 0
    assert "S" in data["name"]  # synthesized prefix
    assert data["id"] > 0


@pytest.mark.asyncio
async def test_synthesize_custom_course(storage: Storage) -> None:
    """POST /api/sessions/synthesize with custom mark sequence."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/synthesize",
            json={
                "course_type": "custom",
                "mark_sequence": "S-K-D-F",
                "wind_direction": 0,
                "start_lat": 47.63,
                "start_lon": -122.40,
                "seed": 99,
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["points"] > 50


@pytest.mark.asyncio
async def test_synthesize_missing_mark_sequence(storage: Storage) -> None:
    """Custom course without mark_sequence returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/synthesize",
            json={"course_type": "custom"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_synthesize_unknown_course_type(storage: Storage) -> None:
    """Unknown course_type returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/synthesize",
            json={"course_type": "slalom"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_course_marks_endpoint(storage: Storage) -> None:
    """GET /api/courses/marks returns buoy and CYC marks."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/courses/marks?wind_dir=180&start_lat=47.63&start_lon=-122.4")

    assert resp.status_code == 200
    data = resp.json()
    assert "buoy_marks" in data
    assert "cyc_marks" in data
    assert set(data["buoy_marks"].keys()) == {"S", "A", "O", "G", "X", "F"}
    assert "K" in data["cyc_marks"]


@pytest.mark.asyncio
async def test_synthesize_returns_mark_warnings_for_bad_position(storage: Storage) -> None:
    """Synthesize with RC on land should return mark_warnings in response."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/synthesize",
            json={
                "course_type": "windward_leeward",
                "wind_direction": 90,
                "start_lat": 47.61,
                "start_lon": -122.34,  # downtown Seattle — on land
                "laps": 1,
                "seed": 42,
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert "mark_warnings" in data
    assert len(data["mark_warnings"]) > 0


@pytest.mark.asyncio
async def test_synthesize_no_warnings_for_valid_position(storage: Storage) -> None:
    """Synthesize with RC in open water should have no mark_warnings."""
    await storage.set_daily_event("2026-03-10", "TestRegatta")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/synthesize",
            json={
                "course_type": "windward_leeward",
                "wind_direction": 180,
                "start_lat": 47.70,
                "start_lon": -122.44,
                "laps": 1,
                "seed": 42,
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert "mark_warnings" not in data


@pytest.mark.asyncio
async def test_sessions_filter_synthesized(storage: Storage) -> None:
    """GET /api/sessions?type=synthesized is accepted."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions?type=synthesized")

    assert resp.status_code == 200
    data = resp.json()
    assert "sessions" in data
