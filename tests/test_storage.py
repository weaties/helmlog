"""Tests for storage.py — SQLite persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.nmea2000 import (
    PGN_COG_SOG_RAPID,
    PGN_ENVIRONMENTAL,
    PGN_POSITION_RAPID,
    PGN_SPEED_THROUGH_WATER,
    PGN_VESSEL_HEADING,
    PGN_WATER_DEPTH,
    PGN_WIND_DATA,
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PositionRecord,
    SpeedRecord,
    WindRecord,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage

_TS = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


class TestMigration:
    async def test_migration_creates_tables(self, storage: Storage) -> None:
        """All expected tables exist after migration."""
        db = storage._conn()
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        rows = await cur.fetchall()
        names = {row[0] for row in rows}
        for expected in {
            "schema_version",
            "headings",
            "speeds",
            "depths",
            "positions",
            "cogsog",
            "winds",
            "environmental",
            "video_sessions",
            "weather",
            "tides",
            "session_notes",
            "race_videos",
        }:
            assert expected in names, f"Table {expected!r} not found"

    async def test_migration_version_recorded(self, storage: Storage) -> None:
        from helmlog.storage import _CURRENT_VERSION

        db = storage._conn()
        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == _CURRENT_VERSION

    async def test_migration_idempotent(self, storage: Storage) -> None:
        """Running migrate() a second time must not error or add duplicate rows."""
        from helmlog.storage import _MIGRATIONS

        await storage.migrate()
        await storage.migrate()
        db = storage._conn()
        cur = await db.execute("SELECT COUNT(*) FROM schema_version")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == len(_MIGRATIONS)  # one row per migration version

    async def test_repair_recreates_missing_table(self, storage: Storage) -> None:
        """If a table is missing despite its migration being recorded, repair recreates it."""
        db = storage._conn()
        # Simulate the old executescript() bug: drop a table but keep the version
        await db.execute("DROP TABLE IF EXISTS boat_settings")
        await db.commit()
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='boat_settings'"
        )
        assert await cur.fetchone() is None

        # Re-run migrate — the repair pass should recreate the table
        await storage.migrate()
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='boat_settings'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "boat_settings"


class TestSplitMigrationSql:
    """Verify the helper that splits multi-statement migration strings."""

    def test_splits_multiple_statements(self) -> None:
        from helmlog.storage import _split_migration_sql

        sql = """
            CREATE TABLE foo (id INTEGER PRIMARY KEY);
            CREATE INDEX idx_foo ON foo(id);
        """
        stmts = _split_migration_sql(sql)
        assert len(stmts) == 2
        assert stmts[0].startswith("CREATE TABLE")
        assert stmts[1].startswith("CREATE INDEX")

    def test_strips_comments(self) -> None:
        from helmlog.storage import _split_migration_sql

        sql = """
            -- This is a comment
            ALTER TABLE foo ADD COLUMN bar TEXT;
            -- Another comment
        """
        stmts = _split_migration_sql(sql)
        assert len(stmts) == 1
        assert "comment" not in stmts[0].lower()

    def test_handles_all_migrations(self) -> None:
        """Every migration in _MIGRATIONS must split into at least one statement."""
        from helmlog.storage import _MIGRATIONS, _split_migration_sql

        for version, sql in _MIGRATIONS.items():
            stmts = _split_migration_sql(sql)
            assert len(stmts) >= 1, f"Migration {version} produced no statements"


# ---------------------------------------------------------------------------
# Write + query round-trips
# ---------------------------------------------------------------------------


class TestWriteQuery:
    async def test_heading_round_trip(self, storage: Storage) -> None:
        record = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=180.0,
            deviation_deg=None,
            variation_deg=None,
        )
        await storage.write(record)
        rows = await storage.query_range("headings", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["heading_deg"] - 180.0) < 0.001

    async def test_speed_round_trip(self, storage: Storage) -> None:
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=5.0,
        )
        await storage.write(record)
        rows = await storage.query_range("speeds", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["speed_kts"] - 5.0) < 0.001

    async def test_depth_round_trip(self, storage: Storage) -> None:
        record = DepthRecord(
            pgn=PGN_WATER_DEPTH,
            source_addr=5,
            timestamp=_TS,
            depth_m=10.0,
            offset_m=0.5,
        )
        await storage.write(record)
        rows = await storage.query_range("depths", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["depth_m"] - 10.0) < 0.001
        assert rows[0]["offset_m"] is not None
        assert abs(rows[0]["offset_m"] - 0.5) < 0.001

    async def test_position_round_trip(self, storage: Storage) -> None:
        record = PositionRecord(
            pgn=PGN_POSITION_RAPID,
            source_addr=5,
            timestamp=_TS,
            latitude_deg=37.8044,
            longitude_deg=-122.2712,
        )
        await storage.write(record)
        rows = await storage.query_range("positions", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["latitude_deg"] - 37.8044) < 1e-4
        assert abs(rows[0]["longitude_deg"] - (-122.2712)) < 1e-4

    async def test_cogsog_round_trip(self, storage: Storage) -> None:
        record = COGSOGRecord(
            pgn=PGN_COG_SOG_RAPID,
            source_addr=5,
            timestamp=_TS,
            cog_deg=45.0,
            sog_kts=6.0,
        )
        await storage.write(record)
        rows = await storage.query_range("cogsog", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["cog_deg"] - 45.0) < 0.001
        assert abs(rows[0]["sog_kts"] - 6.0) < 0.001

    async def test_wind_round_trip(self, storage: Storage) -> None:
        record = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=15.0,
            wind_angle_deg=30.0,
            reference=0,
        )
        await storage.write(record)
        rows = await storage.query_range("winds", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["wind_speed_kts"] - 15.0) < 0.001
        assert rows[0]["reference"] == 0

    async def test_environmental_round_trip(self, storage: Storage) -> None:
        record = EnvironmentalRecord(
            pgn=PGN_ENVIRONMENTAL,
            source_addr=5,
            timestamp=_TS,
            water_temp_c=20.0,
        )
        await storage.write(record)
        rows = await storage.query_range("environmental", _TS, _TS + timedelta(seconds=1))
        assert len(rows) == 1
        assert abs(rows[0]["water_temp_c"] - 20.0) < 0.001

    async def test_none_fields_stored_as_null(self, storage: Storage) -> None:
        record = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=90.0,
            deviation_deg=None,
            variation_deg=None,
        )
        await storage.write(record)
        rows = await storage.query_range("headings", _TS, _TS + timedelta(seconds=1))
        assert rows[0]["deviation_deg"] is None
        assert rows[0]["variation_deg"] is None

    async def test_timestamps_stored_as_utc_iso(self, storage: Storage) -> None:
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=7.0,
        )
        await storage.write(record)
        rows = await storage.query_range("speeds", _TS, _TS + timedelta(seconds=1))
        ts_str: str = rows[0]["ts"]
        # Must be parseable as ISO 8601 and contain UTC info
        parsed = datetime.fromisoformat(ts_str)
        assert parsed.tzinfo is not None

    async def test_query_range_excludes_outside(self, storage: Storage) -> None:
        ts_in = _TS + timedelta(seconds=30)
        ts_out = _TS + timedelta(seconds=90)
        for ts, spd in [(ts_in, 5.0), (ts_out, 9.0)]:
            await storage.write(
                SpeedRecord(
                    pgn=PGN_SPEED_THROUGH_WATER,
                    source_addr=5,
                    timestamp=ts,
                    speed_kts=spd,
                )
            )
        rows = await storage.query_range(
            "speeds",
            _TS,
            _TS + timedelta(seconds=60),
        )
        assert len(rows) == 1
        assert abs(rows[0]["speed_kts"] - 5.0) < 0.001

    async def test_query_unknown_table_raises(self, storage: Storage) -> None:
        with pytest.raises(ValueError, match="Unknown table"):
            await storage.query_range(
                "drop_table_injected",
                _TS,
                _TS + timedelta(seconds=1),
            )


# ---------------------------------------------------------------------------
# Session gate flag tests
# ---------------------------------------------------------------------------

_DATE = "2025-08-10"
_START1 = datetime(2025, 8, 10, 13, 45, 0, tzinfo=UTC)
_START2 = datetime(2025, 8, 10, 14, 5, 30, tzinfo=UTC)
_END1 = datetime(2025, 8, 10, 14, 5, 0, tzinfo=UTC)


class TestSessionGate:
    async def test_session_active_false_initially(self, storage: Storage) -> None:
        assert storage.session_active is False

    async def test_session_active_true_after_start_race(self, storage: Storage) -> None:
        await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
        assert storage.session_active is True

    async def test_session_active_false_after_end_race(self, storage: Storage) -> None:
        race = await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1")
        await storage.end_race(race.id, _END1)
        assert storage.session_active is False

    async def test_count_sessions_for_date(self, storage: Storage) -> None:
        await storage.start_race("BallardCup", _START1, _DATE, 1, "20250810-BallardCup-1", "race")
        await storage.start_race(
            "BallardCup", _START2, _DATE, 1, "20250810-BallardCup-P1", "practice"
        )
        assert await storage.count_sessions_for_date(_DATE, "race") == 1
        assert await storage.count_sessions_for_date(_DATE, "practice") == 1
        assert await storage.count_sessions_for_date("2025-08-11", "race") == 0


# ---------------------------------------------------------------------------
# Live instrument cache tests
# ---------------------------------------------------------------------------


class TestLiveCache:
    def test_cache_initially_empty(self, storage: Storage) -> None:
        result = storage.live_instruments()
        assert all(v is None for v in result.values())

    def test_update_live_heading(self, storage: Storage) -> None:
        record = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=270.0,
            deviation_deg=None,
            variation_deg=None,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["heading_deg"] == 270.0

    def test_update_live_speed(self, storage: Storage) -> None:
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=6.54,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["bsp_kts"] == 6.54

    def test_update_live_cogsog(self, storage: Storage) -> None:
        record = COGSOGRecord(
            pgn=PGN_COG_SOG_RAPID,
            source_addr=5,
            timestamp=_TS,
            cog_deg=45.0,
            sog_kts=7.0,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["cog_deg"] == 45.0
        assert result["sog_kts"] == 7.0

    def test_update_live_apparent_wind(self, storage: Storage) -> None:
        record = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=12.0,
            wind_angle_deg=35.0,
            reference=2,
        )
        storage.update_live(record)
        result = storage.live_instruments()
        assert result["aws_kts"] == 12.0
        assert result["awa_deg"] == 35.0

    def test_update_live_true_wind_boat_ref(self, storage: Storage) -> None:
        """reference=0 (TWA boat-referenced): TWD computed from heading."""
        hdg = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=100.0,
            deviation_deg=None,
            variation_deg=None,
        )
        tw = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=10.0,
            wind_angle_deg=45.0,
            reference=0,
        )
        storage.update_live(hdg)
        storage.update_live(tw)
        result = storage.live_instruments()
        assert result["tws_kts"] == 10.0
        assert result["twa_deg"] == 45.0
        assert result["twd_deg"] == 145.0  # (100 + 45) % 360

    def test_update_live_true_wind_north_ref(self, storage: Storage) -> None:
        """reference=4 (TWD north-referenced): TWA computed from heading."""
        hdg = HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=100.0,
            deviation_deg=None,
            variation_deg=None,
        )
        tw = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=10.0,
            wind_angle_deg=145.0,
            reference=4,
        )
        storage.update_live(hdg)
        storage.update_live(tw)
        result = storage.live_instruments()
        assert result["tws_kts"] == 10.0
        assert result["twd_deg"] == 145.0
        assert result["twa_deg"] == 45.0  # (145 - 100 + 360) % 360

    def test_update_live_twa_no_heading(self, storage: Storage) -> None:
        """TWD is None when heading unavailable and reference=0."""
        tw = WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS,
            wind_speed_kts=10.0,
            wind_angle_deg=45.0,
            reference=0,
        )
        storage.update_live(tw)
        result = storage.live_instruments()
        assert result["twa_deg"] == 45.0
        assert result["twd_deg"] is None

    async def test_latest_instruments_uses_live_cache(self, storage: Storage) -> None:
        """latest_instruments() returns live values when cache is populated."""
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=8.5,
        )
        storage.update_live(record)
        result = await storage.latest_instruments()
        assert result["bsp_kts"] == 8.5

    async def test_latest_instruments_db_fallback_when_cache_empty(self, storage: Storage) -> None:
        """latest_instruments() falls back to DB when cache is entirely empty."""
        record = SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS,
            speed_kts=4.2,
        )
        await storage.write(record)
        result = await storage.latest_instruments()
        assert result["bsp_kts"] is not None
        assert abs(result["bsp_kts"] - 4.2) < 0.01


# ---------------------------------------------------------------------------
# Crew storage tests (#305)
# ---------------------------------------------------------------------------

_CREW_DATE = "2025-09-01"
_CREW_START = datetime(2025, 9, 1, 14, 0, 0, tzinfo=UTC)


class TestCrewStorage:
    async def _make_race(self, storage: Storage) -> int:
        race = await storage.start_race("Regatta", _CREW_START, _CREW_DATE, 1, "20250901-Regatta-1")
        assert race.id is not None
        return race.id

    async def _pos_id(self, storage: Storage, name: str) -> int:
        positions = await storage.get_crew_positions()
        for p in positions:
            if p["name"] == name:
                return p["id"]
        raise ValueError(f"Position {name!r} not found")

    async def test_crew_positions_seeded(self, storage: Storage) -> None:
        """Migration seeds 6 default crew positions."""
        positions = await storage.get_crew_positions()
        names = [p["name"] for p in positions]
        assert names == ["helm", "main", "pit", "bow", "tactician", "guest"]

    async def test_crew_positions_crud(self, storage: Storage) -> None:
        """set_crew_positions can add, reorder, and remove positions."""
        await storage.set_crew_positions(
            [
                {"name": "helm", "display_order": 0},
                {"name": "main", "display_order": 1},
                {"name": "trimmer", "display_order": 2},
            ]
        )
        positions = await storage.get_crew_positions()
        names = [p["name"] for p in positions]
        assert names == ["helm", "main", "trimmer"]
        assert "pit" not in names

    async def test_set_and_get_crew_defaults_boat_level(self, storage: Storage) -> None:
        """set_crew_defaults with race_id=None stores boat-level defaults."""
        helm_id = await self._pos_id(storage, "helm")
        main_id = await self._pos_id(storage, "main")
        alice_id = await storage.create_placeholder_user("Alice")
        bob_id = await storage.create_placeholder_user("Bob")

        await storage.set_crew_defaults(
            None,
            [
                {"position_id": helm_id, "user_id": alice_id},
                {"position_id": main_id, "user_id": bob_id},
            ],
        )
        defaults = await storage.get_crew_defaults(None)
        assert len(defaults) == 2
        pos_map = {d["position"]: d["user_name"] for d in defaults}
        assert pos_map["helm"] == "Alice"
        assert pos_map["main"] == "Bob"

    async def test_set_and_get_crew_defaults_race_level(self, storage: Storage) -> None:
        """set_crew_defaults with race_id stores race-level overrides."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        mark_id = await storage.create_placeholder_user("Mark")

        await storage.set_crew_defaults(
            race_id,
            [
                {"position_id": helm_id, "user_id": mark_id},
            ],
        )
        entries = await storage.get_crew_defaults(race_id)
        assert len(entries) == 1
        assert entries[0]["user_name"] == "Mark"

    async def test_resolve_crew_boat_defaults_only(self, storage: Storage) -> None:
        """resolve_crew falls through to boat-level when no race-level set."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        alice_id = await storage.create_placeholder_user("Alice")

        await storage.set_crew_defaults(
            None,
            [
                {"position_id": helm_id, "user_id": alice_id},
            ],
        )
        resolved = await storage.resolve_crew(race_id)
        helm_entry = next(e for e in resolved if e["position"] == "helm")
        assert helm_entry["user_name"] == "Alice"
        assert helm_entry["source"] == "boat"

    async def test_resolve_crew_race_overrides(self, storage: Storage) -> None:
        """Race-level entry wins over boat-level default."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        alice_id = await storage.create_placeholder_user("Alice")
        mark_id = await storage.create_placeholder_user("Mark")

        await storage.set_crew_defaults(
            None,
            [
                {"position_id": helm_id, "user_id": alice_id},
            ],
        )
        await storage.set_crew_defaults(
            race_id,
            [
                {"position_id": helm_id, "user_id": mark_id},
            ],
        )
        resolved = await storage.resolve_crew(race_id)
        helm_entry = next(e for e in resolved if e["position"] == "helm")
        assert helm_entry["user_name"] == "Mark"
        assert helm_entry["source"] == "race"
        assert helm_entry["supersedes_user_name"] == "Alice"

    async def test_resolve_crew_mixed(self, storage: Storage) -> None:
        """Some positions overridden at race level, others from boat defaults."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        main_id = await self._pos_id(storage, "main")
        alice_id = await storage.create_placeholder_user("Alice")
        bob_id = await storage.create_placeholder_user("Bob")
        mark_id = await storage.create_placeholder_user("Mark")

        await storage.set_crew_defaults(
            None,
            [
                {"position_id": helm_id, "user_id": alice_id},
                {"position_id": main_id, "user_id": bob_id},
            ],
        )
        await storage.set_crew_defaults(
            race_id,
            [
                {"position_id": helm_id, "user_id": mark_id},
            ],
        )
        resolved = await storage.resolve_crew(race_id)
        by_pos = {e["position"]: e for e in resolved}
        assert by_pos["helm"]["user_name"] == "Mark"
        assert by_pos["helm"]["source"] == "race"
        assert by_pos["main"]["user_name"] == "Bob"
        assert by_pos["main"]["source"] == "boat"

    async def test_non_attributed_crew(self, storage: Storage) -> None:
        """Non-attributed crew entries track position filled without user name."""
        race_id = await self._make_race(storage)
        guest_id = await self._pos_id(storage, "guest")
        await storage.set_crew_defaults(
            race_id,
            [
                {"position_id": guest_id, "attributed": False},
            ],
        )
        entries = await storage.get_crew_defaults(race_id)
        assert len(entries) == 1
        assert entries[0]["attributed"] == 0
        assert entries[0]["user_id"] is None

    async def test_crew_weight_tracking(self, storage: Storage) -> None:
        """Body and gear weight are stored per crew entry."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        alice_id = await storage.create_placeholder_user("Alice")

        await storage.set_crew_defaults(
            race_id,
            [
                {
                    "position_id": helm_id,
                    "user_id": alice_id,
                    "body_weight": 155.0,
                    "gear_weight": 12.5,
                },
            ],
        )
        entries = await storage.get_crew_defaults(race_id)
        assert entries[0]["body_weight"] == 155.0
        assert entries[0]["gear_weight"] == 12.5

    async def test_placeholder_user_creation(self, storage: Storage) -> None:
        """create_placeholder_user creates user, second call returns same id."""
        uid1 = await storage.create_placeholder_user("TestSailor")
        uid2 = await storage.create_placeholder_user("TestSailor")
        assert uid1 == uid2
        user = await storage.get_user_by_id(uid1)
        assert user is not None
        assert user["name"] == "TestSailor"

    async def test_consent_with_user_id(self, storage: Storage) -> None:
        """set_crew_consent and get_crew_consents work with user_id."""
        uid = await storage.create_placeholder_user("ConsentSailor")
        row_id = await storage.set_crew_consent(uid, "audio", True)
        assert row_id > 0
        consents = await storage.get_crew_consents(uid)
        assert len(consents) == 1
        assert consents[0]["consent_type"] == "audio"
        assert consents[0]["granted"] == 1

    async def test_set_crew_defaults_full_replace(self, storage: Storage) -> None:
        """Second set_crew_defaults call replaces all entries for that level."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        main_id = await self._pos_id(storage, "main")
        alice_id = await storage.create_placeholder_user("Alice")
        bob_id = await storage.create_placeholder_user("Bob")

        await storage.set_crew_defaults(
            race_id,
            [
                {"position_id": helm_id, "user_id": alice_id},
                {"position_id": main_id, "user_id": bob_id},
            ],
        )
        # Replace with just helm
        await storage.set_crew_defaults(
            race_id,
            [
                {"position_id": helm_id, "user_id": bob_id},
            ],
        )
        entries = await storage.get_crew_defaults(race_id)
        assert len(entries) == 1
        assert entries[0]["user_name"] == "Bob"

    async def test_get_race_crew_legacy_compat(self, storage: Storage) -> None:
        """get_race_crew returns legacy format from resolved crew."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        alice_id = await storage.create_placeholder_user("Alice")

        await storage.set_crew_defaults(
            None,
            [
                {"position_id": helm_id, "user_id": alice_id},
            ],
        )
        crew = await storage.get_race_crew(race_id)
        assert len(crew) == 1
        assert crew[0] == {"position": "helm", "sailor": "Alice"}

    async def test_user_without_name_shows_email(self, storage: Storage) -> None:
        """Users with no name show email as user_name in crew entries."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        # Create a real user with no name (like an OAuth user)
        uid = await storage.create_user("noname@example.com", None, "crew")
        await storage.set_crew_defaults(
            race_id,
            [{"position_id": helm_id, "user_id": uid}],
        )
        entries = await storage.get_crew_defaults(race_id)
        assert entries[0]["user_name"] == "noname@example.com"
        # Legacy compat also works
        crew = await storage.get_race_crew(race_id)
        assert crew[0]["sailor"] == "noname@example.com"

    async def test_duplicate_user_id_rejected(self, storage: Storage) -> None:
        """set_crew_defaults raises ValueError if same user_id appears twice."""
        race_id = await self._make_race(storage)
        helm_id = await self._pos_id(storage, "helm")
        main_id = await self._pos_id(storage, "main")
        alice_id = await storage.create_placeholder_user("Alice")

        with pytest.raises(ValueError, match="Duplicate user_id"):
            await storage.set_crew_defaults(
                race_id,
                [
                    {"position_id": helm_id, "user_id": alice_id},
                    {"position_id": main_id, "user_id": alice_id},
                ],
            )

    async def test_user_weight(self, storage: Storage) -> None:
        """update_user_weight stores weight_lbs on user record."""
        uid = await storage.create_placeholder_user("WeightTest")
        await storage.update_user_weight(uid, 175.0)
        user = await storage.get_user_by_id(uid)
        assert user is not None
        assert user["weight_lbs"] == 175.0


# ---------------------------------------------------------------------------
# Boat registry tests
# ---------------------------------------------------------------------------

_BOAT_DATE = "2025-10-01"
_BOAT_START = datetime(2025, 10, 1, 14, 0, 0, tzinfo=UTC)


class TestBoatRegistry:
    async def test_add_boat(self, storage: Storage) -> None:
        """add_boat returns a positive integer id."""
        boat_id = await storage.add_boat("USA 52091", "Jubilee", "J105")
        assert isinstance(boat_id, int)
        assert boat_id > 0

    async def test_find_or_create_boat_existing(self, storage: Storage) -> None:
        """find_or_create_boat returns the same id for the same sail_number."""
        id1 = await storage.add_boat("USA 1234", "Test Boat", "J105")
        id2 = await storage.find_or_create_boat("USA 1234")
        assert id1 == id2

    async def test_find_or_create_boat_new(self, storage: Storage) -> None:
        """find_or_create_boat creates a new boat if sail_number not found."""
        boat_id = await storage.find_or_create_boat("USA 9999")
        assert isinstance(boat_id, int)
        assert boat_id > 0
        boats = await storage.list_boats()
        sail_numbers = [b["sail_number"] for b in boats]
        assert "USA 9999" in sail_numbers

    async def test_list_boats_mru_order(self, storage: Storage) -> None:
        """list_boats returns boats ordered by last_used descending."""
        race = await storage.start_race("Test", _BOAT_START, _BOAT_DATE, 1, "20251001-Test-1")
        id_older = await storage.add_boat("USA 0001", None, None)
        id_newer = await storage.add_boat("USA 0002", None, None)
        # Use newer boat in a result to bump its last_used
        await storage.upsert_race_result(race.id, 1, id_newer)
        await storage.upsert_race_result(race.id, 2, id_older)
        # Make newer's last_used actually newer by upserting again
        await storage.upsert_race_result(race.id, 1, id_newer)
        boats = await storage.list_boats()
        ids = [b["id"] for b in boats]
        assert ids.index(id_newer) < ids.index(id_older)

    async def test_list_boats_exclude_race(self, storage: Storage) -> None:
        """list_boats(exclude_race_id=X) omits boats already in that race."""
        race = await storage.start_race("Test", _BOAT_START, _BOAT_DATE, 1, "20251001-Test-1")
        id_in = await storage.add_boat("USA 1111", None, None)
        id_out = await storage.add_boat("USA 2222", None, None)
        await storage.upsert_race_result(race.id, 1, id_in)
        boats = await storage.list_boats(exclude_race_id=race.id)
        boat_ids = [b["id"] for b in boats]
        assert id_in not in boat_ids
        assert id_out in boat_ids

    async def test_list_boats_search(self, storage: Storage) -> None:
        """list_boats(q=...) filters by sail_number or name."""
        await storage.add_boat("USA 5555", "Windward", "J105")
        await storage.add_boat("USA 6666", "Leeward", "J105")
        results = await storage.list_boats(q="windward")
        assert len(results) == 1
        assert results[0]["sail_number"] == "USA 5555"

    async def test_update_boat(self, storage: Storage) -> None:
        """update_boat changes the boat's fields."""
        boat_id = await storage.add_boat("USA 7777", "Old Name", "J105")
        await storage.update_boat(boat_id, "USA 7777", "New Name", "J/105")
        boats = await storage.list_boats(q="USA 7777")
        assert boats[0]["name"] == "New Name"
        assert boats[0]["class"] == "J/105"

    async def test_delete_boat(self, storage: Storage) -> None:
        """delete_boat removes the boat from the registry."""
        boat_id = await storage.add_boat("USA 8888", None, None)
        await storage.delete_boat(boat_id)
        boats = await storage.list_boats()
        assert not any(b["id"] == boat_id for b in boats)


# ---------------------------------------------------------------------------
# Race results tests
# ---------------------------------------------------------------------------


class TestRaceResults:
    async def _make_race(self, storage: Storage) -> int:
        race = await storage.start_race("Regatta", _BOAT_START, _BOAT_DATE, 1, "20251001-Regatta-1")
        assert race.id is not None
        return race.id

    async def _make_boat(self, storage: Storage, sail_number: str) -> int:
        return await storage.add_boat(sail_number, None, None)

    async def test_upsert_race_result_updates_last_used(self, storage: Storage) -> None:
        """upsert_race_result sets last_used on the boat."""
        race_id = await self._make_race(storage)
        boat_id = await self._make_boat(storage, "USA 0010")
        await storage.upsert_race_result(race_id, 1, boat_id)
        boats = await storage.list_boats(q="USA 0010")
        assert boats[0]["last_used"] is not None

    async def test_list_race_results_ordered_by_place(self, storage: Storage) -> None:
        """list_race_results returns results in ascending place order."""
        race_id = await self._make_race(storage)
        b1 = await self._make_boat(storage, "USA 0020")
        b2 = await self._make_boat(storage, "USA 0021")
        b3 = await self._make_boat(storage, "USA 0022")
        await storage.upsert_race_result(race_id, 3, b3)
        await storage.upsert_race_result(race_id, 1, b1)
        await storage.upsert_race_result(race_id, 2, b2)
        results = await storage.list_race_results(race_id)
        assert [r["place"] for r in results] == [1, 2, 3]

    async def test_delete_race_result(self, storage: Storage) -> None:
        """delete_race_result removes the row."""
        race_id = await self._make_race(storage)
        boat_id = await self._make_boat(storage, "USA 0030")
        result_id = await storage.upsert_race_result(race_id, 1, boat_id)
        await storage.delete_race_result(result_id)
        results = await storage.list_race_results(race_id)
        assert results == []

    async def test_unique_place_constraint(self, storage: Storage) -> None:
        """Upserting the same place replaces the old boat assignment."""
        race_id = await self._make_race(storage)
        b1 = await self._make_boat(storage, "USA 0040")
        b2 = await self._make_boat(storage, "USA 0041")
        await storage.upsert_race_result(race_id, 1, b1)
        await storage.upsert_race_result(race_id, 1, b2)
        results = await storage.list_race_results(race_id)
        assert len(results) == 1
        assert results[0]["boat_id"] == b2

    async def test_unique_boat_constraint(self, storage: Storage) -> None:
        """The same boat cannot occupy two places simultaneously."""
        race_id = await self._make_race(storage)
        b1 = await self._make_boat(storage, "USA 0050")
        await storage.upsert_race_result(race_id, 1, b1)
        # Move same boat to place 2 — old place-1 row should disappear
        await storage.upsert_race_result(race_id, 2, b1)
        results = await storage.list_race_results(race_id)
        boat_entries = [r for r in results if r["boat_id"] == b1]
        assert len(boat_entries) == 1
        assert boat_entries[0]["place"] == 2

    async def test_result_dnf_dns_flags(self, storage: Storage) -> None:
        """DNF and DNS flags are stored and retrieved correctly."""
        race_id = await self._make_race(storage)
        boat_id = await self._make_boat(storage, "USA 0060")
        await storage.upsert_race_result(race_id, 1, boat_id, dnf=True, dns=False)
        results = await storage.list_race_results(race_id)
        assert results[0]["dnf"] is True
        assert results[0]["dns"] is False

    async def test_result_includes_boat_fields(self, storage: Storage) -> None:
        """list_race_results joins boat name and sail_number."""
        race_id = await self._make_race(storage)
        boat_id = await storage.add_boat("USA 0070", "Jubilee", "J105")
        await storage.upsert_race_result(race_id, 1, boat_id)
        results = await storage.list_race_results(race_id)
        assert results[0]["sail_number"] == "USA 0070"
        assert results[0]["boat_name"] == "Jubilee"


# ---------------------------------------------------------------------------
# Session notes
# ---------------------------------------------------------------------------

_NOTE_DATE = "2026-02-27"
_NOTE_START = datetime(2026, 2, 27, 14, 0, 0, tzinfo=UTC)


class TestSessionNotes:
    async def _make_race(self, storage: Storage) -> int:
        race = await storage.start_race("Regatta", _NOTE_START, _NOTE_DATE, 1, "20260227-Regatta-1")
        assert race.id is not None
        return race.id

    async def test_create_note_returns_id(self, storage: Storage) -> None:
        """create_note returns a positive integer id."""
        race_id = await self._make_race(storage)
        note_id = await storage.create_note(_NOTE_START.isoformat(), "Test note", race_id=race_id)
        assert isinstance(note_id, int)
        assert note_id > 0

    async def test_list_notes_by_race(self, storage: Storage) -> None:
        """list_notes returns notes in ts-ascending order for a race."""
        race_id = await self._make_race(storage)
        ts1 = _NOTE_START.isoformat()
        ts2 = (_NOTE_START + timedelta(seconds=60)).isoformat()
        await storage.create_note(ts2, "Second", race_id=race_id)
        await storage.create_note(ts1, "First", race_id=race_id)
        notes = await storage.list_notes(race_id=race_id)
        assert len(notes) == 2
        assert notes[0]["body"] == "First"
        assert notes[1]["body"] == "Second"

    async def test_list_notes_empty_for_new_race(self, storage: Storage) -> None:
        """list_notes returns [] when no notes exist."""
        race_id = await self._make_race(storage)
        assert await storage.list_notes(race_id=race_id) == []

    async def test_delete_note_returns_true(self, storage: Storage) -> None:
        """delete_note returns True when the note is found and deleted."""
        race_id = await self._make_race(storage)
        note_id = await storage.create_note(_NOTE_START.isoformat(), "Gone", race_id=race_id)
        assert await storage.delete_note(note_id) is True
        assert await storage.list_notes(race_id=race_id) == []

    async def test_delete_note_returns_false_when_missing(self, storage: Storage) -> None:
        """delete_note returns False for a non-existent id."""
        assert await storage.delete_note(99999) is False

    async def test_list_notes_range(self, storage: Storage) -> None:
        """list_notes_range returns only notes within the time window."""
        race_id = await self._make_race(storage)
        ts_in = _NOTE_START + timedelta(seconds=30)
        ts_out = _NOTE_START + timedelta(seconds=90)
        await storage.create_note(ts_in.isoformat(), "In range", race_id=race_id)
        await storage.create_note(ts_out.isoformat(), "Out of range", race_id=race_id)
        notes = await storage.list_notes_range(_NOTE_START, _NOTE_START + timedelta(seconds=60))
        assert len(notes) == 1
        assert notes[0]["body"] == "In range"

    async def test_note_cascade_delete_with_race(self, storage: Storage) -> None:
        """Notes are deleted when their parent race is deleted (CASCADE)."""
        race_id = await self._make_race(storage)
        await storage.create_note(_NOTE_START.isoformat(), "Will be gone", race_id=race_id)
        db = storage._conn()
        await db.execute("DELETE FROM races WHERE id = ?", (race_id,))
        await db.commit()
        assert await storage.list_notes(race_id=race_id) == []

    async def test_list_notes_requires_session_arg(self, storage: Storage) -> None:
        """list_notes raises ValueError when called with no session argument."""
        with pytest.raises(ValueError, match="Either race_id or audio_session_id"):
            await storage.list_notes()

    # ------------------------------------------------------------------
    # list_settings_keys tests
    # ------------------------------------------------------------------

    async def test_list_settings_keys_empty_when_no_notes(self, storage: Storage) -> None:
        """Returns [] when no settings notes have been saved."""
        keys = await storage.list_settings_keys()
        assert keys == []

    async def test_list_settings_keys_returns_sorted_distinct_keys(self, storage: Storage) -> None:
        """Returns alphabetically sorted unique keys across all settings notes."""
        race_id = await self._make_race(storage)
        ts = _NOTE_START.isoformat()
        import json as _json

        await storage.create_note(
            ts,
            _json.dumps({"backstay": "2.5", "cunningham": "off"}),
            race_id=race_id,
            note_type="settings",
        )
        await storage.create_note(
            ts,
            _json.dumps({"jib_lead": "5", "backstay": "3"}),
            race_id=race_id,
            note_type="settings",
        )
        keys = await storage.list_settings_keys()
        assert keys == ["backstay", "cunningham", "jib_lead"]

    async def test_list_settings_keys_ignores_text_notes(self, storage: Storage) -> None:
        """Non-settings notes are excluded from the key list."""
        race_id = await self._make_race(storage)
        await storage.create_note(
            _NOTE_START.isoformat(), "plain text note", race_id=race_id, note_type="text"
        )
        keys = await storage.list_settings_keys()
        assert keys == []

    async def test_list_settings_keys_ignores_malformed_bodies(self, storage: Storage) -> None:
        """Notes with non-JSON or non-object bodies are silently skipped."""
        race_id = await self._make_race(storage)
        ts = _NOTE_START.isoformat()
        # insert a malformed settings note directly via storage
        await storage.create_note(ts, "not-json", race_id=race_id, note_type="settings")
        await storage.create_note(ts, "[1, 2, 3]", race_id=race_id, note_type="settings")
        keys = await storage.list_settings_keys()
        assert keys == []


# ---------------------------------------------------------------------------
# Race videos tests
# ---------------------------------------------------------------------------

_VID_DATE = "2026-02-27"
_VID_RACE_START = datetime(2026, 2, 27, 18, 0, 0, tzinfo=UTC)
_VID_SYNC_UTC = datetime(2026, 2, 27, 18, 5, 0, tzinfo=UTC)  # 5 min into race


@pytest.mark.asyncio
class TestRaceVideos:
    async def _make_race(self, storage: Storage) -> int:
        race = await storage.start_race(
            "BallardCup", _VID_RACE_START, _VID_DATE, 1, "20260227-BallardCup-1"
        )
        assert race.id is not None
        return race.id

    async def _add_video(self, storage: Storage, race_id: int, **kwargs: object) -> int:
        defaults: dict[str, object] = {
            "youtube_url": "https://youtu.be/dQw4w9WgXcQ",
            "video_id": "dQw4w9WgXcQ",
            "title": "Test Video",
            "label": "Bow cam",
            "sync_utc": _VID_SYNC_UTC,
            "sync_offset_s": 323.0,  # video was at 5:23 when the gun went off
            "duration_s": 3600.0,
        }
        defaults.update(kwargs)
        return await storage.add_race_video(race_id=race_id, **defaults)  # type: ignore[arg-type]

    async def test_add_and_list(self, storage: Storage) -> None:
        """add_race_video returns an id; list_race_videos returns it."""
        race_id = await self._make_race(storage)
        vid_id = await self._add_video(storage, race_id)
        rows = await storage.list_race_videos(race_id)
        assert len(rows) == 1
        assert rows[0]["id"] == vid_id
        assert rows[0]["video_id"] == "dQw4w9WgXcQ"
        assert rows[0]["label"] == "Bow cam"
        assert rows[0]["sync_offset_s"] == 323.0

    async def test_list_returns_empty_for_new_race(self, storage: Storage) -> None:
        race_id = await self._make_race(storage)
        assert await storage.list_race_videos(race_id) == []

    async def test_list_ordered_by_created_at(self, storage: Storage) -> None:
        """Videos are returned in insertion order."""
        race_id = await self._make_race(storage)
        id1 = await self._add_video(storage, race_id, label="Bow cam")
        id2 = await self._add_video(storage, race_id, label="Cockpit cam")
        rows = await storage.list_race_videos(race_id)
        assert [r["id"] for r in rows] == [id1, id2]

    async def test_update_label(self, storage: Storage) -> None:
        race_id = await self._make_race(storage)
        vid_id = await self._add_video(storage, race_id)
        found = await storage.update_race_video(vid_id, label="Mastcam")
        assert found is True
        rows = await storage.list_race_videos(race_id)
        assert rows[0]["label"] == "Mastcam"

    async def test_update_sync(self, storage: Storage) -> None:
        race_id = await self._make_race(storage)
        vid_id = await self._add_video(storage, race_id)
        new_sync = datetime(2026, 2, 27, 18, 10, 0, tzinfo=UTC)
        found = await storage.update_race_video(vid_id, sync_utc=new_sync, sync_offset_s=500.0)
        assert found is True
        rows = await storage.list_race_videos(race_id)
        assert rows[0]["sync_offset_s"] == 500.0
        assert rows[0]["sync_utc"] == new_sync.isoformat()

    async def test_update_returns_false_for_missing(self, storage: Storage) -> None:
        found = await storage.update_race_video(99999, label="ghost")
        assert found is False

    async def test_delete(self, storage: Storage) -> None:
        race_id = await self._make_race(storage)
        vid_id = await self._add_video(storage, race_id)
        assert await storage.delete_race_video(vid_id) is True
        assert await storage.list_race_videos(race_id) == []

    async def test_delete_returns_false_for_missing(self, storage: Storage) -> None:
        assert await storage.delete_race_video(99999) is False

    async def test_cascade_delete_with_race(self, storage: Storage) -> None:
        """Videos are removed when the parent race is deleted."""
        race_id = await self._make_race(storage)
        await self._add_video(storage, race_id)
        db = storage._conn()
        await db.execute("DELETE FROM races WHERE id = ?", (race_id,))
        await db.commit()
        assert await storage.list_race_videos(race_id) == []


# ---------------------------------------------------------------------------
# VideoSession offset math tests (pure unit tests, no DB)
# ---------------------------------------------------------------------------


def test_video_offset_at_sync_point() -> None:
    """At the sync point itself, video_offset_at == sync_offset_s."""
    from helmlog.video import VideoSession

    sync = datetime(2026, 2, 27, 18, 5, 0, tzinfo=UTC)
    vs = VideoSession(
        url="https://youtu.be/x",
        video_id="x",
        title="t",
        duration_s=3600.0,
        sync_utc=sync,
        sync_offset_s=323.0,
    )
    assert vs.video_offset_at(sync) == pytest.approx(323.0)


def test_video_offset_at_30s_after_sync() -> None:
    """30 seconds after sync, the video position is sync_offset_s + 30."""
    from datetime import timedelta

    from helmlog.video import VideoSession

    sync = datetime(2026, 2, 27, 18, 5, 0, tzinfo=UTC)
    vs = VideoSession(
        url="https://youtu.be/x",
        video_id="x",
        title="t",
        duration_s=3600.0,
        sync_utc=sync,
        sync_offset_s=323.0,
    )
    assert vs.video_offset_at(sync + timedelta(seconds=30)) == pytest.approx(353.0)


def test_url_at_returns_none_outside_duration() -> None:
    """url_at returns None when the computed position is outside [0, duration_s]."""
    from helmlog.video import VideoSession

    sync = datetime(2026, 2, 27, 18, 5, 0, tzinfo=UTC)
    vs = VideoSession(
        url="https://youtu.be/dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        title="t",
        duration_s=60.0,  # only 1-minute video
        sync_utc=sync,
        sync_offset_s=30.0,
    )
    from datetime import timedelta

    # 35 seconds after sync → position 65 > duration 60 → None
    assert vs.url_at(sync + timedelta(seconds=35)) is None


def test_url_at_generates_correct_link() -> None:
    """url_at embeds floor(offset) as ?t= parameter."""
    from helmlog.video import VideoSession

    sync = datetime(2026, 2, 27, 18, 0, 0, tzinfo=UTC)
    vs = VideoSession(
        url="https://youtu.be/dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        title="t",
        duration_s=3600.0,
        sync_utc=sync,
        sync_offset_s=0.0,
    )
    from datetime import timedelta

    link = vs.url_at(sync + timedelta(seconds=90))
    assert link == "https://youtu.be/dQw4w9WgXcQ?t=90"


# ---------------------------------------------------------------------------
# Boat settings tests
# ---------------------------------------------------------------------------

_BS_DATE = "2026-03-12"
_BS_START = datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
class TestBoatSettings:
    async def _make_race(self, storage: Storage) -> int:
        race = await storage.start_race("Regatta", _BS_START, _BS_DATE, 1, "20260312-Regatta-1")
        assert race.id is not None
        return race.id

    async def test_create_and_list(self, storage: Storage) -> None:
        """Create settings and list them back."""
        race_id = await self._make_race(storage)
        ts = _BS_START.isoformat()
        ids = await storage.create_boat_settings(
            race_id,
            [
                {"ts": ts, "parameter": "backstay", "value": "3.5"},
                {"ts": ts, "parameter": "vang", "value": "2.0"},
            ],
            source="manual",
        )
        assert len(ids) == 2
        rows = await storage.list_boat_settings(race_id)
        assert len(rows) == 2
        params = {r["parameter"] for r in rows}
        assert params == {"backstay", "vang"}

    async def test_create_rejects_unknown_parameter(self, storage: Storage) -> None:
        """Unknown parameter names are rejected."""
        race_id = await self._make_race(storage)
        ts = _BS_START.isoformat()
        with pytest.raises(ValueError, match="Unknown parameter"):
            await storage.create_boat_settings(
                race_id,
                [{"ts": ts, "parameter": "not_a_real_param", "value": "1"}],
                source="manual",
            )

    async def test_current_returns_latest_per_parameter(self, storage: Storage) -> None:
        """current_boat_settings returns only the most recent value per param."""
        race_id = await self._make_race(storage)
        ts1 = _BS_START.isoformat()
        ts2 = (_BS_START + timedelta(seconds=60)).isoformat()
        await storage.create_boat_settings(
            race_id,
            [{"ts": ts1, "parameter": "backstay", "value": "3.0"}],
            source="manual",
        )
        await storage.create_boat_settings(
            race_id,
            [
                {"ts": ts2, "parameter": "backstay", "value": "4.0"},
                {"ts": ts2, "parameter": "vang", "value": "1.5"},
            ],
            source="manual",
        )
        current = await storage.current_boat_settings(race_id)
        by_param = {r["parameter"]: r["value"] for r in current}
        assert by_param["backstay"] == "4.0"
        assert by_param["vang"] == "1.5"
        assert len(current) == 2

    async def test_delete_extraction_run(self, storage: Storage) -> None:
        """Deleting an extraction run removes only those entries."""
        race_id = await self._make_race(storage)
        ts = _BS_START.isoformat()
        await storage.create_boat_settings(
            race_id,
            [{"ts": ts, "parameter": "backstay", "value": "3.0"}],
            source="manual",
        )
        await storage.create_boat_settings(
            race_id,
            [{"ts": ts, "parameter": "vang", "value": "2.0"}],
            source="transcript:whisper-base",
            extraction_run_id=42,
        )
        count = await storage.delete_boat_settings_extraction_run(42)
        assert count == 1
        rows = await storage.list_boat_settings(race_id)
        assert len(rows) == 1
        assert rows[0]["parameter"] == "backstay"

    async def test_cascade_delete_with_race(self, storage: Storage) -> None:
        """Settings are removed when the parent race is deleted."""
        race_id = await self._make_race(storage)
        ts = _BS_START.isoformat()
        await storage.create_boat_settings(
            race_id,
            [{"ts": ts, "parameter": "cunningham", "value": "0"}],
            source="manual",
        )
        db = storage._conn()
        await db.execute("DELETE FROM races WHERE id = ?", (race_id,))
        await db.commit()
        rows = await storage.list_boat_settings(race_id)
        assert rows == []

    async def test_list_empty_for_new_race(self, storage: Storage) -> None:
        """list_boat_settings returns [] for a race with no settings."""
        race_id = await self._make_race(storage)
        assert await storage.list_boat_settings(race_id) == []

    async def test_null_race_id_allowed(self, storage: Storage) -> None:
        """Settings can be created with race_id=None (dock setup)."""
        ts = _BS_START.isoformat()
        ids = await storage.create_boat_settings(
            None,
            [{"ts": ts, "parameter": "shroud_tension_upper", "value": "28"}],
            source="manual",
        )
        assert len(ids) == 1

    async def test_null_race_id_list_and_current(self, storage: Storage) -> None:
        """list and current work with race_id=None."""
        ts1 = _BS_START.isoformat()
        ts2 = (_BS_START + timedelta(minutes=1)).isoformat()
        await storage.create_boat_settings(
            None,
            [{"ts": ts1, "parameter": "shroud_tension_upper", "value": "28"}],
            source="manual",
        )
        await storage.create_boat_settings(
            None,
            [{"ts": ts2, "parameter": "shroud_tension_upper", "value": "30"}],
            source="manual",
        )
        rows = await storage.list_boat_settings(None)
        assert len(rows) == 2
        current = await storage.current_boat_settings(None)
        assert len(current) == 1
        assert current[0]["value"] == "30"

    async def test_resolve_merges_boat_and_race_settings(self, storage: Storage) -> None:
        """resolve_boat_settings merges race-specific over boat-level defaults."""
        race_id = await self._make_race(storage)
        ts_dock = (_BS_START - timedelta(hours=1)).isoformat()
        ts_race = (_BS_START + timedelta(minutes=5)).isoformat()
        as_of = (_BS_START + timedelta(minutes=10)).isoformat()

        # Boat-level defaults (set at the dock)
        await storage.create_boat_settings(
            None,
            [
                {"ts": ts_dock, "parameter": "backstay", "value": "3.0"},
                {"ts": ts_dock, "parameter": "vang", "value": "2.0"},
            ],
            source="manual",
        )
        # Race-specific override for backstay only
        await storage.create_boat_settings(
            race_id,
            [{"ts": ts_race, "parameter": "backstay", "value": "5.0"}],
            source="transcript:whisper-base",
        )

        resolved = await storage.resolve_boat_settings(race_id, as_of)
        by_param = {r["parameter"]: r for r in resolved}

        # backstay: race-specific wins, supersedes boat-level
        assert by_param["backstay"]["value"] == "5.0"
        assert by_param["backstay"]["race_id"] == race_id
        assert by_param["backstay"]["supersedes_value"] == "3.0"
        assert by_param["backstay"]["supersedes_source"] == "manual"

        # vang: boat-level fallback, no override
        assert by_param["vang"]["value"] == "2.0"
        assert by_param["vang"]["race_id"] is None
        assert by_param["vang"]["supersedes_value"] is None

    async def test_resolve_respects_timestamp(self, storage: Storage) -> None:
        """resolve_boat_settings only considers settings at or before as_of."""
        race_id = await self._make_race(storage)
        ts1 = _BS_START.isoformat()
        ts2 = (_BS_START + timedelta(minutes=10)).isoformat()

        await storage.create_boat_settings(
            race_id,
            [{"ts": ts1, "parameter": "backstay", "value": "3.0"}],
            source="manual",
        )
        await storage.create_boat_settings(
            race_id,
            [{"ts": ts2, "parameter": "backstay", "value": "5.0"}],
            source="manual",
        )

        # Query at ts1+5min — should see 3.0 (ts2 hasn't happened yet)
        as_of = (_BS_START + timedelta(minutes=5)).isoformat()
        resolved = await storage.resolve_boat_settings(race_id, as_of)
        by_param = {r["parameter"]: r for r in resolved}
        assert by_param["backstay"]["value"] == "3.0"

        # Query at ts2 — should see 5.0
        resolved2 = await storage.resolve_boat_settings(race_id, ts2)
        by_param2 = {r["parameter"]: r for r in resolved2}
        assert by_param2["backstay"]["value"] == "5.0"

    async def test_resolve_empty_when_no_settings(self, storage: Storage) -> None:
        """resolve_boat_settings returns [] when no settings exist."""
        race_id = await self._make_race(storage)
        resolved = await storage.resolve_boat_settings(race_id, _BS_START.isoformat())
        assert resolved == []
