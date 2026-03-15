"""Tests for the pluggable visualization framework (#286)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

from helmlog.storage import Storage, StorageConfig
from helmlog.visualization.discovery import discover_viz_plugins, get_viz_plugin
from helmlog.visualization.preferences import resolve_viz_preference, set_viz_preference
from helmlog.visualization.protocol import VizContext, VizPluginMeta
from helmlog.web import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


async def _seed_session(storage: Storage) -> int:
    """Create a completed session with instrument data. Returns race_id."""
    race = await storage.start_race(
        "Test",
        datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
        "2024-06-15",
        1,
        "Test Race 1",
        "race",
    )
    race_id = race.id
    db = storage._conn()
    for i in range(5):
        ts = f"2024-06-15T12:00:{i:02d}"
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts, race_id) VALUES (?, 5, ?, ?)",
            (ts, 5.0 + i * 0.1, race_id),
        )
        await db.execute(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
            " VALUES (?, 5, ?, ?, 0, ?)",
            (ts, 12.0, 45.0, race_id),
        )
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg, race_id) VALUES (?, 5, ?, ?)",
            (ts, 180.0, race_id),
        )
        await db.execute(
            "INSERT INTO positions"
            " (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, 5, ?, ?, ?)",
            (ts, 41.0 + i * 0.001, -71.0 + i * 0.001, race_id),
        )
    await db.commit()
    await storage.end_race(race_id, datetime(2024, 6, 15, 12, 5, 0, tzinfo=UTC))
    return race_id


async def _seed_user(storage: Storage) -> int:
    return await storage.create_user("test@example.com", "Test User", "crew")


# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_viz_plugin_meta_frozen(self) -> None:
        m = VizPluginMeta(name="test", display_name="Test", description="desc", version="1.0")
        assert m.name == "test"
        assert m.required_analysis == []
        with pytest.raises(AttributeError):
            m.name = "other"  # type: ignore[misc]

    def test_viz_plugin_meta_with_required_analysis(self) -> None:
        m = VizPluginMeta(
            name="test",
            display_name="Test",
            description="desc",
            version="1.0",
            required_analysis=["polar_baseline"],
        )
        assert m.required_analysis == ["polar_baseline"]

    def test_viz_context_defaults(self) -> None:
        ctx = VizContext(user_id=1)
        assert ctx.co_op_id is None
        assert ctx.is_co_op_data is False

    def test_viz_context_co_op(self) -> None:
        ctx = VizContext(user_id=1, co_op_id="abc", is_co_op_data=True)
        assert ctx.is_co_op_data is True


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_viz_plugins_returns_dict(self) -> None:
        plugins = discover_viz_plugins(force_rescan=True)
        assert isinstance(plugins, dict)
        # Should find the 3 built-in plugins
        assert "polar_scatter" in plugins
        assert "speed_vmg_timeseries" in plugins
        assert "track_performance_map" in plugins

    def test_get_viz_plugin_found(self) -> None:
        p = get_viz_plugin("polar_scatter")
        assert p is not None
        assert p.meta().name == "polar_scatter"

    def test_get_viz_plugin_not_found(self) -> None:
        assert get_viz_plugin("nonexistent") is None

    def test_rescan_finds_same_plugins(self) -> None:
        p1 = discover_viz_plugins(force_rescan=True)
        p2 = discover_viz_plugins(force_rescan=True)
        assert set(p1.keys()) == set(p2.keys())


# ---------------------------------------------------------------------------
# Preference tests
# ---------------------------------------------------------------------------


class TestPreferences:
    @pytest.mark.asyncio
    async def test_no_preference(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        result = await resolve_viz_preference(storage, user_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_platform_preference(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        await set_viz_preference(storage, "platform", None, ["polar_scatter"])
        result = await resolve_viz_preference(storage, user_id)
        assert result == ["polar_scatter"]

    @pytest.mark.asyncio
    async def test_user_overrides_platform(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        await set_viz_preference(storage, "platform", None, ["polar_scatter"])
        await set_viz_preference(storage, "user", str(user_id), ["speed_vmg_timeseries"])
        result = await resolve_viz_preference(storage, user_id)
        assert result == ["speed_vmg_timeseries"]

    @pytest.mark.asyncio
    async def test_boat_overrides_co_op(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        await set_viz_preference(storage, "co_op", "coop1", ["polar_scatter"])
        await set_viz_preference(storage, "boat", None, ["track_performance_map"])
        result = await resolve_viz_preference(storage, user_id, co_op_id="coop1")
        assert result == ["track_performance_map"]

    @pytest.mark.asyncio
    async def test_co_op_preference(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        await set_viz_preference(
            storage, "co_op", "coop1", ["polar_scatter", "speed_vmg_timeseries"]
        )
        result = await resolve_viz_preference(storage, user_id, co_op_id="coop1")
        assert result == ["polar_scatter", "speed_vmg_timeseries"]

    @pytest.mark.asyncio
    async def test_invalid_scope_raises(self, storage: Storage) -> None:
        with pytest.raises(ValueError, match="Invalid scope"):
            await set_viz_preference(storage, "invalid", None, ["test"])


# ---------------------------------------------------------------------------
# Plugin render tests
# ---------------------------------------------------------------------------


class TestPolarScatterPlugin:
    @pytest.mark.asyncio
    async def test_render_with_analysis_data(self) -> None:
        plugin = get_viz_plugin("polar_scatter")
        assert plugin is not None

        analysis_results: dict[str, Any] = {
            "raw": {
                "cells": [
                    {"twa_bin": 45, "session_mean_bsp": 5.5, "sample_count": 10},
                    {"twa_bin": 90, "session_mean_bsp": 6.2, "sample_count": 8},
                ]
            }
        }
        ctx = VizContext(user_id=1)
        result = await plugin.render({}, analysis_results, ctx)

        assert "data" in result
        assert "layout" in result
        assert result["data"][0]["type"] == "scatterpolar"
        assert len(result["data"][0]["r"]) == 2
        assert len(result["data"][0]["theta"]) == 2

    @pytest.mark.asyncio
    async def test_render_empty_analysis(self) -> None:
        plugin = get_viz_plugin("polar_scatter")
        assert plugin is not None

        ctx = VizContext(user_id=1)
        result = await plugin.render({}, {}, ctx)

        assert "data" in result
        assert "layout" in result
        assert result["data"][0]["r"] == []

    @pytest.mark.asyncio
    async def test_render_from_viz_data(self) -> None:
        """Falls back to viz data when raw is missing."""
        plugin = get_viz_plugin("polar_scatter")
        assert plugin is not None

        analysis_results: dict[str, Any] = {
            "viz": [
                {
                    "chart_type": "polar",
                    "title": "Session Polar",
                    "data": {
                        "cells": [
                            {
                                "twa_bin": 30,
                                "session_mean_bsp": 4.0,
                                "sample_count": 5,
                            }
                        ]
                    },
                }
            ]
        }
        ctx = VizContext(user_id=1)
        result = await plugin.render({}, analysis_results, ctx)
        assert len(result["data"][0]["r"]) == 1


class TestSpeedVMGTimeseriesPlugin:
    @pytest.mark.asyncio
    async def test_render_with_session_data(self) -> None:
        plugin = get_viz_plugin("speed_vmg_timeseries")
        assert plugin is not None

        session_data: dict[str, Any] = {
            "speeds": [
                {"ts": "2024-06-15T12:00:00", "speed_kts": 5.0},
                {"ts": "2024-06-15T12:00:01", "speed_kts": 5.5},
            ],
            "winds": [
                {
                    "ts": "2024-06-15T12:00:00",
                    "wind_angle_deg": 45.0,
                    "wind_speed_kts": 12.0,
                },
            ],
        }
        ctx = VizContext(user_id=1)
        result = await plugin.render(session_data, {}, ctx)

        assert "data" in result
        assert "layout" in result
        # Should have BSP trace
        assert result["data"][0]["name"] == "BSP (kts)"
        assert len(result["data"][0]["y"]) == 2
        # Should have VMG trace (at least one wind match)
        assert len(result["data"]) == 2
        assert result["data"][1]["name"] == "VMG (kts)"

    @pytest.mark.asyncio
    async def test_render_empty_data(self) -> None:
        plugin = get_viz_plugin("speed_vmg_timeseries")
        assert plugin is not None

        ctx = VizContext(user_id=1)
        result = await plugin.render({"speeds": [], "winds": []}, {}, ctx)

        assert "data" in result
        assert "layout" in result
        assert len(result["data"]) == 1  # BSP trace only (no VMG data)
        assert result["data"][0]["y"] == []


class TestTrackPerformanceMapPlugin:
    @pytest.mark.asyncio
    async def test_render_with_positions(self) -> None:
        plugin = get_viz_plugin("track_performance_map")
        assert plugin is not None

        session_data: dict[str, Any] = {
            "positions": [
                {"ts": "2024-06-15T12:00:00", "lat": 41.0, "lon": -71.0},
                {"ts": "2024-06-15T12:00:01", "lat": 41.001, "lon": -71.001},
            ],
            "speeds": [
                {"ts": "2024-06-15T12:00:00", "speed_kts": 5.0},
            ],
        }
        ctx = VizContext(user_id=1)
        result = await plugin.render(session_data, {}, ctx)

        assert "data" in result
        assert "layout" in result
        assert result["data"][0]["type"] == "scattermapbox"
        assert len(result["data"][0]["lat"]) == 2
        assert result["layout"]["mapbox"]["style"] == "open-street-map"

    @pytest.mark.asyncio
    async def test_render_empty_positions(self) -> None:
        plugin = get_viz_plugin("track_performance_map")
        assert plugin is not None

        ctx = VizContext(user_id=1)
        result = await plugin.render({"positions": [], "speeds": []}, {}, ctx)

        assert result["data"][0]["lat"] == []


# ---------------------------------------------------------------------------
# Link sharing fallback tests
# ---------------------------------------------------------------------------


class TestLinkSharing:
    @pytest.mark.asyncio
    async def test_shared_valid_viz(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/visualizations/shared?viz=polar_scatter&model=polar_baseline"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is False
        assert data["viz"]["name"] == "polar_scatter"
        assert data["model"] == "polar_baseline"

    @pytest.mark.asyncio
    async def test_shared_unknown_viz_fallback(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/visualizations/shared?viz=unknown_plugin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is True
        assert "available" in data
        assert data["requested_viz"] == "unknown_plugin"

    @pytest.mark.asyncio
    async def test_shared_no_viz_param(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/visualizations/shared")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_shared_valid_viz_no_model(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/visualizations/shared?viz=speed_vmg_timeseries")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is False
        assert data["model"] is None

    @pytest.mark.asyncio
    async def test_shared_valid_viz_with_unknown_model(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/visualizations/shared?viz=polar_scatter&model=nonexistent"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is False
        # Model resolution is deferred to render time

    @pytest.mark.asyncio
    async def test_shared_both_unknown(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/visualizations/shared?viz=nope&model=nope")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is True


# ---------------------------------------------------------------------------
# Co-op data licensing tests
# ---------------------------------------------------------------------------


class TestCoopDataLicensing:
    @pytest.mark.asyncio
    async def test_own_session_allows_render(self, storage: Storage) -> None:
        """Own boat session: render allowed."""
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/visualizations/render/{race_id}?viz=speed_vmg_timeseries"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert "layout" in data

    @pytest.mark.asyncio
    async def test_render_nonexistent_session(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/visualizations/render/9999?viz=polar_scatter")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_render_unknown_viz(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/visualizations/render/{race_id}?viz=nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_render_missing_viz_param(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/visualizations/render/{race_id}")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_co_op_session_view_only(self, storage: Storage) -> None:
        """Co-op session: render allowed but audit logged."""
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        # Mark as co-op data
        db = storage._conn()
        await db.execute(
            "UPDATE races SET peer_fingerprint = ? WHERE id = ?",
            ("abc123fingerprint", race_id),
        )
        await db.commit()

        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/visualizations/render/{race_id}?viz=speed_vmg_timeseries"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestVisualizationAPI:
    @pytest.mark.asyncio
    async def test_catalog(self, storage: Storage) -> None:
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/visualizations/catalog")
        assert resp.status_code == 200
        catalog = resp.json()
        assert isinstance(catalog, list)
        names = [p["name"] for p in catalog]
        assert "polar_scatter" in names
        assert "speed_vmg_timeseries" in names
        assert "track_performance_map" in names
        # Check metadata fields present
        for p in catalog:
            assert "display_name" in p
            assert "description" in p
            assert "version" in p
            assert "required_analysis" in p

    @pytest.mark.asyncio
    async def test_render_polar_scatter(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/visualizations/render/{race_id}?viz=polar_scatter&model=polar_baseline"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert "layout" in data
        assert data["data"][0]["type"] == "scatterpolar"

    @pytest.mark.asyncio
    async def test_render_track_map(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/visualizations/render/{race_id}?viz=track_performance_map"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"][0]["type"] == "scattermapbox"

    @pytest.mark.asyncio
    async def test_preferences_get(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/visualizations/preferences")
        assert resp.status_code == 200
        assert resp.json()["plugin_names"] is None

    @pytest.mark.asyncio
    async def test_preferences_set_and_get(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/visualizations/preferences",
                json={
                    "scope": "user",
                    "plugin_names": ["polar_scatter", "speed_vmg_timeseries"],
                },
            )
            assert resp.status_code == 200

            resp = await client.get("/api/visualizations/preferences")
            assert resp.json()["plugin_names"] == [
                "polar_scatter",
                "speed_vmg_timeseries",
            ]

    @pytest.mark.asyncio
    async def test_preferences_empty_raises(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/visualizations/preferences",
                json={"scope": "user", "plugin_names": []},
            )
        assert resp.status_code == 422
