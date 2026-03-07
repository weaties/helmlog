"""Tests for Gaia GPS import into SQLite (Layer 3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from logger.storage import Storage, StorageConfig


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


class TestImportRace:
    @pytest.mark.asyncio
    async def test_import_race(self, storage: Storage) -> None:
        race_id = await storage.import_race(
            name="2025-09-04-gaigps-7d1bd70e",
            event="Wednesday Evening Boating",
            race_num=1,
            date_str="2025-09-04",
            start_utc=datetime(2025, 9, 4, 1, 17, 33, tzinfo=UTC),
            end_utc=datetime(2025, 9, 4, 2, 1, 32, tzinfo=UTC),
            session_type="race",
            source="gaiagps",
            source_id="7d1bd70eeb99ade0641acba897d32f8f",
        )
        assert race_id is not None
        assert race_id > 0

    @pytest.mark.asyncio
    async def test_dedup_check(self, storage: Storage) -> None:
        assert not await storage.has_source_id("gaiagps", "abc123")
        await storage.import_race(
            name="test-race",
            event="Test",
            race_num=1,
            date_str="2025-01-01",
            start_utc=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            end_utc=datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC),
            session_type="race",
            source="gaiagps",
            source_id="abc123",
        )
        assert await storage.has_source_id("gaiagps", "abc123")
        assert not await storage.has_source_id("gaiagps", "xyz789")


class TestImportTrackPoints:
    @pytest.mark.asyncio
    async def test_import_points(self, storage: Storage) -> None:
        points = [
            ("2025-09-04T01:17:33", 47.688, -122.417, None, None),
            ("2025-09-04T01:17:39", 47.689, -122.418, 45.0, 5.5),
            ("2025-09-04T01:17:45", 47.690, -122.419, 90.0, 6.0),
        ]
        n = await storage.import_track_points(points)
        assert n == 3

        # Verify positions were written
        start = datetime(2025, 9, 4, 1, 0, 0, tzinfo=UTC)
        end = datetime(2025, 9, 4, 2, 0, 0, tzinfo=UTC)
        positions = await storage.query_range("positions", start, end)
        assert len(positions) == 3

        # Verify cogsog (only 2 points had non-None values)
        cogsog = await storage.query_range("cogsog", start, end)
        assert len(cogsog) == 2

    @pytest.mark.asyncio
    async def test_import_empty_points(self, storage: Storage) -> None:
        n = await storage.import_track_points([])
        assert n == 0
