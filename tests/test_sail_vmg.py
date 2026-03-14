"""Tests for the sail VMG comparison plugin (#309)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio

from helmlog.analysis.discovery import load_session_data
from helmlog.analysis.plugins.sail_vmg import (
    SailVMGPlugin,
    compute_downwind_vmg,
    compute_upwind_vmg,
    wind_band_for,
    wind_band_label,
)
from helmlog.analysis.protocol import AnalysisContext
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


async def _seed_sails(storage: Storage) -> dict[str, int]:
    """Create test sails. Returns {type: sail_id}."""
    db = storage._conn()
    sails = {}
    for sail_type, name, pos in [
        ("main", "Full Main", "both"),
        ("jib", "J1", "upwind"),
        ("spinnaker", "A2", "downwind"),
    ]:
        cur = await db.execute(
            "INSERT INTO sails (type, name, point_of_sail) VALUES (?, ?, ?)",
            (sail_type, name, pos),
        )
        sails[sail_type] = cur.lastrowid or 0
    await db.commit()
    return sails


async def _seed_session_with_sails(storage: Storage, sails: dict[str, int]) -> int:
    """Create a completed session with instrument data and sail changes."""
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

    # Insert sail change at start
    await db.execute(
        "INSERT INTO sail_changes (race_id, ts, main_id, jib_id, spinnaker_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (race_id, "2024-06-15T12:00:00", sails["main"], sails["jib"], None),
    )

    # Insert upwind data (TWA=45, BSP=5)
    for i in range(10):
        ts = f"2024-06-15T12:00:{i:02d}"
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts, race_id) VALUES (?, 5, ?, ?)",
            (ts, 5.0, race_id),
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

    # Add sail change to spinnaker
    await db.execute(
        "INSERT INTO sail_changes (race_id, ts, main_id, jib_id, spinnaker_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (race_id, "2024-06-15T12:01:00", sails["main"], None, sails["spinnaker"]),
    )

    # Insert downwind data (TWA=150, BSP=6)
    for i in range(10):
        ts = f"2024-06-15T12:01:{i:02d}"
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts, race_id) VALUES (?, 5, ?, ?)",
            (ts, 6.0, race_id),
        )
        await db.execute(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
            " VALUES (?, 5, ?, ?, 0, ?)",
            (ts, 12.0, 150.0, race_id),
        )
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg, race_id) VALUES (?, 5, ?, ?)",
            (ts, 180.0, race_id),
        )

    await db.commit()
    await storage.end_race(race_id, datetime(2024, 6, 15, 12, 5, 0, tzinfo=UTC))
    return race_id


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestVMGFunctions:
    def test_compute_upwind_vmg(self) -> None:
        vmg = compute_upwind_vmg(5.0, 45.0)
        expected = 5.0 * math.cos(math.radians(45.0))
        assert abs(vmg - expected) < 0.001

    def test_compute_upwind_vmg_close_hauled(self) -> None:
        vmg = compute_upwind_vmg(4.0, 30.0)
        expected = 4.0 * math.cos(math.radians(30.0))
        assert abs(vmg - expected) < 0.001

    def test_compute_downwind_vmg(self) -> None:
        vmg = compute_downwind_vmg(6.0, 150.0)
        expected = 6.0 * math.cos(math.radians(30.0))
        assert abs(vmg - expected) < 0.001

    def test_compute_downwind_vmg_dead_run(self) -> None:
        vmg = compute_downwind_vmg(5.0, 170.0)
        expected = 5.0 * math.cos(math.radians(10.0))
        assert abs(vmg - expected) < 0.001

    def test_wind_band_for_low(self) -> None:
        assert wind_band_for(3.0) == (0, 6)

    def test_wind_band_for_mid(self) -> None:
        assert wind_band_for(12.0) == (10, 15)

    def test_wind_band_for_high(self) -> None:
        assert wind_band_for(25.0) == (20, float("inf"))

    def test_wind_band_label(self) -> None:
        assert wind_band_label(0, 6) == "0-6"
        assert wind_band_label(20, float("inf")) == "20+"

    def test_wind_bands_cover_range(self) -> None:
        for tws in [0, 3, 6, 8, 10, 12, 15, 18, 20, 25, 30]:
            assert wind_band_for(float(tws)) is not None


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------


class TestSailVMGPlugin:
    @pytest.mark.asyncio
    async def test_plugin_meta(self) -> None:
        plugin = SailVMGPlugin()
        meta = plugin.meta()
        assert meta.name == "sail_vmg"
        assert meta.version == "1.0.0"

    @pytest.mark.asyncio
    async def test_analyze_with_sail_data(self, storage: Storage) -> None:
        sails = await _seed_sails(storage)
        race_id = await _seed_session_with_sails(storage, sails)
        session_data = await load_session_data(storage, race_id)
        assert session_data is not None

        plugin = SailVMGPlugin()
        ctx = AnalysisContext(user_id=1)
        result = await plugin.analyze(session_data, ctx)

        assert result.plugin_name == "sail_vmg"
        assert result.session_id == race_id
        total_metric = next(m for m in result.metrics if m.name == "total_vmg_points")
        assert total_metric.value > 0

    @pytest.mark.asyncio
    async def test_analyze_empty_session(self, storage: Storage) -> None:
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

        plugin = SailVMGPlugin()
        ctx = AnalysisContext(user_id=1)
        result = await plugin.analyze(session_data, ctx)

        total_metric = next(m for m in result.metrics if m.name == "total_vmg_points")
        assert total_metric.value == 0

    @pytest.mark.asyncio
    async def test_discovered_by_framework(self) -> None:
        from helmlog.analysis.discovery import discover_plugins

        plugins = discover_plugins(force_rescan=True)
        assert "sail_vmg" in plugins


# ---------------------------------------------------------------------------
# Storage method tests
# ---------------------------------------------------------------------------


class TestSailActiveRanges:
    @pytest.mark.asyncio
    async def test_get_sail_active_ranges(self, storage: Storage) -> None:
        sails = await _seed_sails(storage)
        await _seed_session_with_sails(storage, sails)
        ranges = await storage.get_sail_active_ranges()
        assert len(ranges) > 0

    @pytest.mark.asyncio
    async def test_filter_by_sail_type(self, storage: Storage) -> None:
        sails = await _seed_sails(storage)
        await _seed_session_with_sails(storage, sails)
        ranges = await storage.get_sail_active_ranges(sail_type="main")
        for r in ranges:
            assert r["sail_type"] == "main"

    @pytest.mark.asyncio
    async def test_filter_by_sail_id(self, storage: Storage) -> None:
        sails = await _seed_sails(storage)
        await _seed_session_with_sails(storage, sails)
        main_id = sails["main"]
        ranges = await storage.get_sail_active_ranges(sail_id=main_id)
        for r in ranges:
            assert r["sail_id"] == main_id


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestSailPerformanceAPI:
    @pytest.mark.asyncio
    async def test_performance_endpoint(self, storage: Storage) -> None:
        sails = await _seed_sails(storage)
        await _seed_session_with_sails(storage, sails)
        await storage.create_user("test@example.com", "Test", "crew")

        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/sails/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert "sails" in data

    @pytest.mark.asyncio
    async def test_performance_filter_by_type(self, storage: Storage) -> None:
        sails = await _seed_sails(storage)
        await _seed_session_with_sails(storage, sails)
        await storage.create_user("test@example.com", "Test", "crew")

        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/sails/performance?sail_type=main")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_performance_empty(self, storage: Storage) -> None:
        await storage.create_user("test@example.com", "Test", "crew")
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/sails/performance")
        assert resp.status_code == 200
        assert resp.json()["sails"] == []
