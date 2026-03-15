"""Tests for the pluggable analysis framework (#283)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio

from helmlog.analysis.cache import AnalysisCache, _compute_data_hash
from helmlog.analysis.discovery import discover_plugins, get_plugin, load_session_data
from helmlog.analysis.preferences import resolve_preference, set_preference
from helmlog.analysis.protocol import (
    AnalysisContext,
    AnalysisResult,
    Insight,
    Metric,
    PluginMeta,
    SessionData,
    VizData,
)
from helmlog.storage import Storage, StorageConfig
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
    # Insert some speed/wind/heading data
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
    await db.commit()
    await storage.end_race(race_id, datetime(2024, 6, 15, 12, 5, 0, tzinfo=UTC))
    return race_id


async def _seed_user(storage: Storage) -> int:
    return await storage.create_user("test@example.com", "Test User", "crew")


# ---------------------------------------------------------------------------
# Protocol dataclass tests
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_plugin_meta_frozen(self) -> None:
        m = PluginMeta(name="test", display_name="Test", description="desc", version="1.0")
        assert m.name == "test"
        with pytest.raises(AttributeError):
            m.name = "other"  # type: ignore[misc]

    def test_session_data_defaults(self) -> None:
        sd = SessionData(
            session_id=1, start_utc="2024-01-01T00:00:00", end_utc="2024-01-01T01:00:00"
        )
        assert sd.speeds == []
        assert sd.sail_changes == []

    def test_metric_frozen(self) -> None:
        m = Metric(name="x", value=1.0, unit="kts")
        assert m.label == ""

    def test_insight_defaults(self) -> None:
        i = Insight(category="test", message="hello")
        assert i.severity == "info"

    def test_viz_data_defaults(self) -> None:
        v = VizData(chart_type="bar", title="Test")
        assert v.data == {}

    def test_analysis_result_to_dict(self) -> None:
        r = AnalysisResult(
            plugin_name="test",
            plugin_version="1.0",
            session_id=1,
            metrics=[Metric(name="x", value=1.0, unit="kts", label="Speed")],
            insights=[Insight(category="c", message="m", severity="warning")],
            viz=[VizData(chart_type="bar", title="T", data={"k": "v"})],
            raw={"secret": True},
        )
        d = r.to_dict()
        assert d["metrics"][0]["name"] == "x"
        assert d["insights"][0]["severity"] == "warning"
        assert d["viz"][0]["data"] == {"k": "v"}
        assert d["raw"]["secret"] is True

    def test_analysis_result_to_dict_no_raw(self) -> None:
        r = AnalysisResult(plugin_name="t", plugin_version="1", session_id=1, raw={"s": 1})
        d = r.to_dict(include_raw=False)
        assert "raw" not in d

    def test_analysis_context_defaults(self) -> None:
        ctx = AnalysisContext(user_id=1)
        assert ctx.co_op_id is None
        assert ctx.is_co_op_data is False


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_plugins_returns_dict(self) -> None:
        plugins = discover_plugins(force_rescan=True)
        assert isinstance(plugins, dict)
        # Should find at least the polar_baseline plugin
        assert "polar_baseline" in plugins

    def test_get_plugin_found(self) -> None:
        p = get_plugin("polar_baseline")
        assert p is not None
        assert p.meta().name == "polar_baseline"

    def test_get_plugin_not_found(self) -> None:
        assert get_plugin("nonexistent") is None

    @pytest.mark.asyncio
    async def test_load_session_data_no_session(self, storage: Storage) -> None:
        result = await load_session_data(storage, 9999)
        assert result is None

    @pytest.mark.asyncio
    async def test_load_session_data_open_session(self, storage: Storage) -> None:
        race = await storage.start_race(
            "Test",
            datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
            "2024-06-15",
            1,
            "Test Race",
            "race",
        )
        result = await load_session_data(storage, race.id)
        assert result is None  # not ended

    @pytest.mark.asyncio
    async def test_load_session_data_completed(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        result = await load_session_data(storage, race_id)
        assert result is not None
        assert result.session_id == race_id
        assert len(result.speeds) >= 4
        assert len(result.winds) >= 4


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------


class TestCache:
    def test_compute_data_hash_deterministic(self) -> None:
        h1 = _compute_data_hash({"a": 1, "b": 2})
        h2 = _compute_data_hash({"b": 2, "a": 1})
        assert h1 == h2

    @pytest.mark.asyncio
    async def test_cache_miss(self, storage: Storage) -> None:
        cache = AnalysisCache(storage)
        result = await cache.get(1, "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_put_and_get(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        cache = AnalysisCache(storage)
        data = {"metrics": [], "session_id": race_id}
        await cache.put(race_id, "test_plugin", "1.0", "hash123", data)
        result = await cache.get(race_id, "test_plugin")
        assert result is not None
        assert result["session_id"] == race_id

    @pytest.mark.asyncio
    async def test_cache_stale_hash(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        cache = AnalysisCache(storage)
        await cache.put(race_id, "test_plugin", "1.0", "hash_old", {"x": 1})
        result = await cache.get(race_id, "test_plugin", data_hash="hash_new")
        assert result is None  # stale

    @pytest.mark.asyncio
    async def test_cache_invalidate(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        cache = AnalysisCache(storage)
        await cache.put(race_id, "test_plugin", "1.0", "h", {"x": 1})
        await cache.invalidate(race_id)
        assert await cache.get(race_id, "test_plugin") is None


# ---------------------------------------------------------------------------
# Preference tests
# ---------------------------------------------------------------------------


class TestPreferences:
    @pytest.mark.asyncio
    async def test_no_preference(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        result = await resolve_preference(storage, user_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_platform_preference(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        await set_preference(storage, "platform", None, "polar_baseline")
        result = await resolve_preference(storage, user_id)
        assert result == "polar_baseline"

    @pytest.mark.asyncio
    async def test_user_overrides_platform(self, storage: Storage) -> None:
        user_id = await _seed_user(storage)
        await set_preference(storage, "platform", None, "polar_baseline")
        await set_preference(storage, "user", str(user_id), "sail_vmg")
        result = await resolve_preference(storage, user_id)
        assert result == "sail_vmg"

    @pytest.mark.asyncio
    async def test_invalid_scope_raises(self, storage: Storage) -> None:
        with pytest.raises(ValueError, match="Invalid scope"):
            await set_preference(storage, "invalid", None, "test")


# ---------------------------------------------------------------------------
# Polar baseline plugin tests
# ---------------------------------------------------------------------------


class TestPolarBaselinePlugin:
    @pytest.mark.asyncio
    async def test_analyze_with_data(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        session_data = await load_session_data(storage, race_id)
        assert session_data is not None

        plugin = get_plugin("polar_baseline")
        assert plugin is not None

        ctx = AnalysisContext(user_id=1)
        result = await plugin.analyze(session_data, ctx)

        assert result.plugin_name == "polar_baseline"
        assert result.session_id == race_id
        assert len(result.metrics) > 0
        # Should have total_samples metric
        samples_metric = next(m for m in result.metrics if m.name == "total_samples")
        assert samples_metric.value >= 0

    @pytest.mark.asyncio
    async def test_analyze_empty_session(self, storage: Storage) -> None:
        # Create session with no instrument data
        race = await storage.start_race(
            "Test",
            datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
            "2024-06-15",
            1,
            "Empty Race",
            "race",
        )
        race_id = race.id
        await storage.end_race(race_id, datetime(2024, 6, 15, 12, 5, 0, tzinfo=UTC))
        session_data = await load_session_data(storage, race_id)
        assert session_data is not None

        plugin = get_plugin("polar_baseline")
        assert plugin is not None
        ctx = AnalysisContext(user_id=1)
        result = await plugin.analyze(session_data, ctx)

        samples_metric = next(m for m in result.metrics if m.name == "total_samples")
        assert samples_metric.value == 0


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestAnalysisAPI:
    @pytest.mark.asyncio
    async def test_list_models(self, storage: Storage) -> None:
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/analysis/models")
        assert resp.status_code == 200
        models = resp.json()
        assert isinstance(models, list)
        names = [m["name"] for m in models]
        assert "polar_baseline" in names

    @pytest.mark.asyncio
    async def test_run_analysis(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/analysis/run/{race_id}?model=polar_baseline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plugin_name"] == "polar_baseline"
        assert "metrics" in data

    @pytest.mark.asyncio
    async def test_run_analysis_not_found(self, storage: Storage) -> None:
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/analysis/run/9999?model=polar_baseline")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cached_result(self, storage: Storage) -> None:
        race_id = await _seed_session(storage)
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Run first to populate cache
            await client.post(f"/api/analysis/run/{race_id}?model=polar_baseline")
            # Get cached result
            resp = await client.get(f"/api/analysis/results/{race_id}?model=polar_baseline")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_preferences_api(self, storage: Storage) -> None:
        await _seed_user(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Get preference (none set)
            resp = await client.get("/api/analysis/preferences")
            assert resp.status_code == 200
            assert resp.json()["model_name"] is None

            # Set preference
            resp = await client.put(
                "/api/analysis/preferences",
                json={"scope": "user", "model_name": "polar_baseline"},
            )
            assert resp.status_code == 200

            # Verify it's set
            resp = await client.get("/api/analysis/preferences")
            assert resp.json()["model_name"] == "polar_baseline"
