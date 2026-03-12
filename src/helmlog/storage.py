"""SQLite persistence layer.

Schema is versioned with simple integer migrations. All timestamps are stored
as UTC ISO 8601 strings. The Storage class is the single source of truth for
all logged data.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiosqlite
from loguru import logger

if TYPE_CHECKING:
    from datetime import datetime

    from helmlog.audio import AudioSession
    from helmlog.external import TideReading, WeatherReading
    from helmlog.races import Race

from helmlog.nmea2000 import (
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PGNRecord,
    PositionRecord,
    SpeedRecord,
    WindRecord,
)
from helmlog.video import VideoSession

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    """Configuration for the SQLite storage backend."""

    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "data/logger.db"))


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

_FLUSH_INTERVAL_S: float = 1.0  # commit to disk at most once per second
_FLUSH_BATCH_SIZE: int = 200  # also flush if this many records are buffered

_LIVE_KEYS = (
    "heading_deg",
    "bsp_kts",
    "cog_deg",
    "sog_kts",
    "tws_kts",
    "twa_deg",
    "twd_deg",
    "aws_kts",
    "awa_deg",
)


# ---------------------------------------------------------------------------
# Schema version & migrations
# ---------------------------------------------------------------------------

_CURRENT_VERSION: int = 33

_MIGRATIONS: dict[int, str] = {
    1: """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS headings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            heading_deg REAL    NOT NULL,
            deviation_deg REAL,
            variation_deg REAL
        );

        CREATE TABLE IF NOT EXISTS speeds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            speed_kts   REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS depths (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            depth_m     REAL    NOT NULL,
            offset_m    REAL
        );

        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            source_addr   INTEGER NOT NULL,
            latitude_deg  REAL    NOT NULL,
            longitude_deg REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cogsog (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            cog_deg     REAL    NOT NULL,
            sog_kts     REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS winds (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            source_addr     INTEGER NOT NULL,
            wind_speed_kts  REAL    NOT NULL,
            wind_angle_deg  REAL    NOT NULL,
            reference       INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS environmental (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            source_addr   INTEGER NOT NULL,
            water_temp_c  REAL    NOT NULL
        );
    """,
    2: """
        CREATE INDEX IF NOT EXISTS idx_headings_ts     ON headings(ts);
        CREATE INDEX IF NOT EXISTS idx_speeds_ts       ON speeds(ts);
        CREATE INDEX IF NOT EXISTS idx_depths_ts       ON depths(ts);
        CREATE INDEX IF NOT EXISTS idx_positions_ts    ON positions(ts);
        CREATE INDEX IF NOT EXISTS idx_cogsog_ts       ON cogsog(ts);
        CREATE INDEX IF NOT EXISTS idx_winds_ts        ON winds(ts);
        CREATE INDEX IF NOT EXISTS idx_environmental_ts ON environmental(ts);
    """,
    3: """
        CREATE TABLE IF NOT EXISTS video_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            url           TEXT    NOT NULL,
            video_id      TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            duration_s    REAL    NOT NULL,
            sync_utc      TEXT    NOT NULL,
            sync_offset_s REAL    NOT NULL,
            created_at    TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_video_sessions_sync_utc ON video_sessions(sync_utc);
    """,
    4: """
        CREATE TABLE IF NOT EXISTS weather (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            lat             REAL    NOT NULL,
            lon             REAL    NOT NULL,
            wind_speed_kts  REAL    NOT NULL,
            wind_dir_deg    REAL    NOT NULL,
            air_temp_c      REAL    NOT NULL,
            pressure_hpa    REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather(ts);
    """,
    5: """
        CREATE TABLE IF NOT EXISTS tides (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            station_id   TEXT    NOT NULL,
            station_name TEXT    NOT NULL,
            height_m     REAL    NOT NULL,
            type         TEXT    NOT NULL,
            UNIQUE(ts, station_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tides_ts ON tides(ts);
    """,
    6: """
        CREATE TABLE IF NOT EXISTS audio_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path    TEXT    NOT NULL,
            device_name  TEXT    NOT NULL,
            start_utc    TEXT    NOT NULL,
            end_utc      TEXT,
            sample_rate  INTEGER NOT NULL,
            channels     INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audio_sessions_start_utc ON audio_sessions(start_utc);
    """,
    7: """
        CREATE TABLE IF NOT EXISTS races (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            event       TEXT    NOT NULL,
            race_num    INTEGER NOT NULL,
            date        TEXT    NOT NULL,
            start_utc   TEXT    NOT NULL,
            end_utc     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_races_date      ON races(date);
        CREATE INDEX IF NOT EXISTS idx_races_start_utc ON races(start_utc);

        CREATE TABLE IF NOT EXISTS daily_events (
            date        TEXT    PRIMARY KEY,
            event_name  TEXT    NOT NULL
        );
    """,
    8: """
        ALTER TABLE races ADD COLUMN session_type TEXT NOT NULL DEFAULT 'race';
    """,
    9: """
        ALTER TABLE audio_sessions ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE audio_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'race';
        ALTER TABLE audio_sessions ADD COLUMN name TEXT;
    """,
    10: """
        CREATE TABLE IF NOT EXISTS race_crew (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id   INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            position  TEXT    NOT NULL,
            sailor    TEXT    NOT NULL,
            UNIQUE(race_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_race_crew_race_id ON race_crew(race_id);

        CREATE TABLE IF NOT EXISTS recent_sailors (
            sailor    TEXT PRIMARY KEY,
            last_used TEXT NOT NULL
        );
    """,
    11: """
        CREATE TABLE IF NOT EXISTS boats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sail_number TEXT UNIQUE NOT NULL,
            name        TEXT,
            class       TEXT,
            last_used   TEXT
        );
        CREATE TABLE IF NOT EXISTS race_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            place       INTEGER NOT NULL,
            boat_id     INTEGER NOT NULL REFERENCES boats(id),
            finish_time TEXT,
            dnf         INTEGER NOT NULL DEFAULT 0,
            dns         INTEGER NOT NULL DEFAULT 0,
            notes       TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(race_id, place),
            UNIQUE(race_id, boat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_race_results_race_id ON race_results(race_id);
    """,
    12: """
        CREATE TABLE IF NOT EXISTS session_notes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id            INTEGER REFERENCES races(id) ON DELETE CASCADE,
            audio_session_id   INTEGER REFERENCES audio_sessions(id) ON DELETE CASCADE,
            ts                 TEXT NOT NULL,
            note_type          TEXT NOT NULL DEFAULT 'text',
            body               TEXT,
            photo_path         TEXT,
            created_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_notes_race_id
            ON session_notes(race_id);
        CREATE INDEX IF NOT EXISTS idx_session_notes_ts
            ON session_notes(ts);
    """,
    13: """
        CREATE TABLE IF NOT EXISTS race_videos (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id          INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            youtube_url      TEXT NOT NULL,
            video_id         TEXT NOT NULL,
            label            TEXT NOT NULL DEFAULT '',
            sync_utc         TEXT NOT NULL,
            sync_offset_s    REAL NOT NULL DEFAULT 0,
            duration_s       REAL,
            title            TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_race_videos_race_id
            ON race_videos(race_id);
    """,
    14: """
        CREATE TABLE IF NOT EXISTS sails (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            type    TEXT    NOT NULL,
            name    TEXT    NOT NULL,
            notes   TEXT,
            active  INTEGER NOT NULL DEFAULT 1,
            UNIQUE(type, name)
        );

        CREATE TABLE IF NOT EXISTS race_sails (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id  INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            sail_id  INTEGER NOT NULL REFERENCES sails(id),
            UNIQUE(race_id, sail_id)
        );
        CREATE INDEX IF NOT EXISTS idx_race_sails_race_id ON race_sails(race_id);
    """,
    15: """
        CREATE TABLE IF NOT EXISTS transcripts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            audio_session_id INTEGER NOT NULL REFERENCES audio_sessions(id) ON DELETE CASCADE,
            status           TEXT    NOT NULL DEFAULT 'pending',
            text             TEXT,
            error_msg        TEXT,
            model            TEXT,
            created_utc      TEXT    NOT NULL,
            updated_utc      TEXT    NOT NULL,
            UNIQUE(audio_session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_transcripts_audio_session_id
            ON transcripts(audio_session_id);
    """,
    16: """
        ALTER TABLE transcripts ADD COLUMN segments_json TEXT;
    """,
    17: """
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT UNIQUE NOT NULL,
            name       TEXT,
            role       TEXT NOT NULL DEFAULT 'viewer',
            created_at TEXT NOT NULL,
            last_seen  TEXT
        );

        CREATE TABLE IF NOT EXISTS invite_tokens (
            token      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            role       TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id),
            expires_at TEXT NOT NULL,
            used_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            session_id TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            ip         TEXT,
            user_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions(user_id);

        ALTER TABLE session_notes ADD COLUMN user_id INTEGER REFERENCES users(id);
        ALTER TABLE race_videos   ADD COLUMN user_id INTEGER REFERENCES users(id);
    """,
    18: """
        CREATE TABLE IF NOT EXISTS polar_baseline (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tws_bin       INTEGER NOT NULL,
            twa_bin       INTEGER NOT NULL,
            mean_bsp      REAL    NOT NULL,
            p90_bsp       REAL    NOT NULL,
            session_count INTEGER NOT NULL,
            sample_count  INTEGER NOT NULL,
            built_at      TEXT    NOT NULL,
            UNIQUE(tws_bin, twa_bin)
        );
        CREATE INDEX IF NOT EXISTS idx_polar_tws_twa ON polar_baseline(tws_bin, twa_bin);
    """,
    19: """
        -- Audit log (#93)
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            user_id     INTEGER REFERENCES users(id),
            action      TEXT NOT NULL,
            detail      TEXT,
            ip_address  TEXT,
            user_agent  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
        CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);

        -- Tags (#99)
        CREATE TABLE IF NOT EXISTS tags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            color      TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_tags (
            session_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (session_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS note_tags (
            note_id    INTEGER NOT NULL REFERENCES session_notes(id) ON DELETE CASCADE,
            tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (note_id, tag_id)
        );

        -- Headshots (#100)
        ALTER TABLE users ADD COLUMN avatar_path TEXT;
    """,
    20: """
        -- Camera session tracking (#98)
        CREATE TABLE IF NOT EXISTS camera_sessions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id            INTEGER NOT NULL REFERENCES races(id),
            camera_name           TEXT NOT NULL,
            camera_ip             TEXT NOT NULL,
            recording_started_utc TEXT,
            recording_stopped_utc TEXT,
            sync_offset_ms        INTEGER,
            error                 TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_camera_sessions_session
            ON camera_sessions(session_id);
    """,
    21: """
        -- Persistent camera configuration (#147)
        CREATE TABLE IF NOT EXISTS cameras (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE,
            ip    TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'insta360-x4'
        );
    """,
    22: """
        -- WiFi credentials for camera AP networks (#147)
        ALTER TABLE cameras ADD COLUMN wifi_ssid TEXT;
        ALTER TABLE cameras ADD COLUMN wifi_password TEXT;
    """,
    23: """
        -- Configurable day-of-week event rules (#154)
        CREATE TABLE IF NOT EXISTS event_rules (
            weekday     INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6),
            event_name  TEXT NOT NULL
        );
        -- Seed with existing hardcoded defaults
        INSERT OR IGNORE INTO event_rules (weekday, event_name) VALUES (0, 'BallardCup');
        INSERT OR IGNORE INTO event_rules (weekday, event_name) VALUES (2, 'CYC');
    """,
    24: """
        -- Admin-configurable settings (#146)
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
    """,
    25: """
        -- Gaia GPS backfill (#101): track provenance metadata on races
        ALTER TABLE races ADD COLUMN source TEXT NOT NULL DEFAULT 'live';
        ALTER TABLE races ADD COLUMN source_id TEXT;
        ALTER TABLE races ADD COLUMN imported_at TEXT;
    """,
    26: """
        -- Data-policy compliance (#194–#211)

        -- Crew consent tracking (#202)
        CREATE TABLE IF NOT EXISTS crew_consents (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sailor_name  TEXT    NOT NULL,
            consent_type TEXT    NOT NULL,
            granted      INTEGER NOT NULL DEFAULT 1,
            granted_at   TEXT    NOT NULL,
            revoked_at   TEXT,
            UNIQUE(sailor_name, consent_type)
        );
        CREATE INDEX IF NOT EXISTS idx_crew_consents_sailor ON crew_consents(sailor_name);

        -- Transcript speaker anonymization map (#197)
        ALTER TABLE transcripts ADD COLUMN speaker_anon_map TEXT;

        -- FK cascade fixes (#206): recreate tables with proper ON DELETE behavior.
        -- race_results.boat_id → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS race_results_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id     INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            place       INTEGER NOT NULL,
            boat_id     INTEGER REFERENCES boats(id) ON DELETE SET NULL,
            finish_time TEXT,
            dnf         INTEGER NOT NULL DEFAULT 0,
            dns         INTEGER NOT NULL DEFAULT 0,
            notes       TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(race_id, place),
            UNIQUE(race_id, boat_id)
        );
        INSERT INTO race_results_new
            SELECT id, race_id, place, boat_id, finish_time, dnf, dns, notes, created_at
            FROM race_results;
        DROP TABLE race_results;
        ALTER TABLE race_results_new RENAME TO race_results;
        CREATE INDEX IF NOT EXISTS idx_race_results_race_id ON race_results(race_id);

        -- invite_tokens.created_by → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS invite_tokens_new (
            token      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            role       TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            expires_at TEXT NOT NULL,
            used_at    TEXT
        );
        INSERT INTO invite_tokens_new
            SELECT token, email, role, created_by, expires_at, used_at
            FROM invite_tokens;
        DROP TABLE invite_tokens;
        ALTER TABLE invite_tokens_new RENAME TO invite_tokens;

        -- session_notes.user_id → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS session_notes_new (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id            INTEGER REFERENCES races(id) ON DELETE CASCADE,
            audio_session_id   INTEGER REFERENCES audio_sessions(id) ON DELETE CASCADE,
            ts                 TEXT NOT NULL,
            note_type          TEXT NOT NULL DEFAULT 'text',
            body               TEXT,
            photo_path         TEXT,
            created_at         TEXT NOT NULL,
            user_id            INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        INSERT INTO session_notes_new
            SELECT id, race_id, audio_session_id, ts, note_type, body,
                   photo_path, created_at, user_id
            FROM session_notes;
        DROP TABLE session_notes;
        ALTER TABLE session_notes_new RENAME TO session_notes;
        CREATE INDEX IF NOT EXISTS idx_session_notes_race_id ON session_notes(race_id);
        CREATE INDEX IF NOT EXISTS idx_session_notes_ts ON session_notes(ts);

        -- race_videos.user_id → ON DELETE SET NULL
        CREATE TABLE IF NOT EXISTS race_videos_new (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id          INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            youtube_url      TEXT NOT NULL,
            video_id         TEXT NOT NULL,
            label            TEXT NOT NULL DEFAULT '',
            sync_utc         TEXT NOT NULL,
            sync_offset_s    REAL NOT NULL DEFAULT 0,
            duration_s       REAL,
            title            TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        INSERT INTO race_videos_new
            SELECT id, race_id, youtube_url, video_id, label, sync_utc,
                   sync_offset_s, duration_s, title, created_at, user_id
            FROM race_videos;
        DROP TABLE race_videos;
        ALTER TABLE race_videos_new RENAME TO race_videos;
        CREATE INDEX IF NOT EXISTS idx_race_videos_race_id ON race_videos(race_id);
    """,
    27: """
        -- Deployment management (#125)
        CREATE TABLE IF NOT EXISTS deployment_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            from_sha   TEXT NOT NULL,
            to_sha     TEXT NOT NULL,
            trigger    TEXT NOT NULL DEFAULT 'manual',
            status     TEXT NOT NULL DEFAULT 'success',
            error      TEXT,
            started_at TEXT NOT NULL,
            user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deployment_log_started
            ON deployment_log(started_at);
    """,
    28: """
        -- Federation Phase 1: identity, co-op membership, session sharing

        -- This boat's keypair reference (key material in filesystem, not DB)
        CREATE TABLE IF NOT EXISTS boat_identity (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            pub_key     TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            sail_number TEXT NOT NULL,
            boat_name   TEXT,
            created_at  TEXT NOT NULL
        );

        -- Co-ops this boat belongs to
        CREATE TABLE IF NOT EXISTS co_op_memberships (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            co_op_id        TEXT NOT NULL,
            co_op_name      TEXT NOT NULL,
            co_op_pub       TEXT NOT NULL,
            membership_json TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'member',
            joined_at       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            UNIQUE(co_op_id)
        );

        -- Per-session co-op sharing decisions
        CREATE TABLE IF NOT EXISTS session_sharing (
            session_id  INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            co_op_id    TEXT NOT NULL,
            shared_at   TEXT NOT NULL,
            shared_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            embargo_until TEXT,
            event_name  TEXT,
            PRIMARY KEY (session_id, co_op_id)
        );

        -- Known peers (other boats in co-ops we belong to)
        CREATE TABLE IF NOT EXISTS co_op_peers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            co_op_id        TEXT NOT NULL,
            boat_pub        TEXT NOT NULL,
            fingerprint     TEXT NOT NULL,
            sail_number     TEXT,
            boat_name       TEXT,
            tailscale_ip    TEXT,
            last_seen       TEXT,
            membership_json TEXT NOT NULL,
            UNIQUE(co_op_id, fingerprint)
        );

        -- Co-op data access audit trail
        CREATE TABLE IF NOT EXISTS co_op_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            co_op_id        TEXT NOT NULL,
            accessor_fp     TEXT NOT NULL,
            action          TEXT NOT NULL,
            resource        TEXT,
            timestamp       TEXT NOT NULL,
            ip              TEXT,
            points_count    INTEGER,
            bytes_transferred INTEGER,
            nonce_hash      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_co_op_audit_ts ON co_op_audit(timestamp);
        CREATE INDEX IF NOT EXISTS idx_co_op_audit_accessor ON co_op_audit(accessor_fp);

        -- Nonce deduplication for replay protection
        CREATE TABLE IF NOT EXISTS request_nonces (
            nonce_hash  TEXT PRIMARY KEY,
            timestamp   TEXT NOT NULL,
            boat_fp     TEXT NOT NULL
        );
    """,
    29: """
        ALTER TABLE races ADD COLUMN peer_fingerprint TEXT;
        ALTER TABLE races ADD COLUMN peer_co_op_id TEXT;
    """,
    30: """
        ALTER TABLE positions ADD COLUMN race_id INTEGER REFERENCES races(id);
        CREATE INDEX IF NOT EXISTS idx_positions_race_id ON positions (race_id);
    """,
    31: """
        -- Maneuver detection (#232)
        CREATE TABLE IF NOT EXISTS maneuvers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            type         TEXT NOT NULL,
            ts           TEXT NOT NULL,
            end_ts       TEXT,
            duration_sec REAL,
            loss_kts     REAL,
            vmg_loss_kts REAL,
            tws_bin      INTEGER,
            twa_bin      INTEGER,
            details      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_maneuvers_session ON maneuvers(session_id);
        CREATE INDEX IF NOT EXISTS idx_maneuvers_type ON maneuvers(type);
    """,
    32: """
        -- Add race_id to instrument tables so overlapping synthesized sessions
        -- can be distinguished.  Real sailing data has no overlaps, but the
        -- synthesizer generates sessions with overlapping timestamp ranges.
        ALTER TABLE headings ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE speeds   ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE winds    ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE cogsog   ADD COLUMN race_id INTEGER REFERENCES races(id);
        ALTER TABLE depths   ADD COLUMN race_id INTEGER REFERENCES races(id);
        CREATE INDEX IF NOT EXISTS idx_headings_race_id ON headings(race_id);
        CREATE INDEX IF NOT EXISTS idx_speeds_race_id   ON speeds(race_id);
        CREATE INDEX IF NOT EXISTS idx_winds_race_id    ON winds(race_id);
        CREATE INDEX IF NOT EXISTS idx_cogsog_race_id   ON cogsog(race_id);
        CREATE INDEX IF NOT EXISTS idx_depths_race_id   ON depths(race_id);
    """,
    33: """
        -- Wind field parameters and course marks for synthesized sessions (#248)
        CREATE TABLE IF NOT EXISTS synth_wind_params (
            session_id        INTEGER PRIMARY KEY REFERENCES races(id) ON DELETE CASCADE,
            seed              INTEGER NOT NULL,
            base_twd          REAL NOT NULL,
            tws_low           REAL NOT NULL,
            tws_high          REAL NOT NULL,
            shift_interval_lo REAL NOT NULL,
            shift_interval_hi REAL NOT NULL,
            shift_magnitude_lo REAL NOT NULL,
            shift_magnitude_hi REAL NOT NULL,
            ref_lat           REAL NOT NULL,
            ref_lon           REAL NOT NULL,
            duration_s        REAL NOT NULL,
            course_type       TEXT NOT NULL,
            leg_distance_nm   REAL,
            laps              INTEGER,
            mark_sequence     TEXT
        );
        CREATE TABLE IF NOT EXISTS synth_course_marks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            mark_key    TEXT NOT NULL,
            mark_name   TEXT NOT NULL,
            lat         REAL NOT NULL,
            lon         REAL NOT NULL,
            UNIQUE(session_id, mark_key)
        );
        CREATE INDEX IF NOT EXISTS idx_synth_course_marks_session
            ON synth_course_marks(session_id);
    """,
}

# Canonical order for the 5 J105 positions + one-off guests
_POSITIONS: tuple[str, ...] = ("helm", "main", "pit", "bow", "tactician", "guest")

# Valid sail slot types
_SAIL_TYPES: tuple[str, ...] = ("main", "jib", "spinnaker")

# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------


class Storage:
    """Async SQLite storage for all logger data."""

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._db: aiosqlite.Connection | None = None
        self._pending: int = 0
        self._last_flush: float = 0.0
        self._session_active: bool = False
        self._live: dict[str, float | None] = dict.fromkeys(_LIVE_KEYS)
        self._live_tw_ref: int | None = None
        self._live_tw_angle_raw: float | None = None

    @property
    def session_active(self) -> bool:
        """True when a race or practice session is currently in progress."""
        return self._session_active

    # ------------------------------------------------------------------
    # In-memory live instrument cache (always updated, no DB I/O)
    # ------------------------------------------------------------------

    def _recompute_true_wind(self) -> None:
        ref = self._live_tw_ref
        ang = self._live_tw_angle_raw
        hdg = self._live["heading_deg"]
        if ref is None or ang is None:
            return
        if ref == 0:  # boat-referenced angle (TWA)
            self._live["twa_deg"] = round(ang, 1)
            self._live["twd_deg"] = round((hdg + ang) % 360, 1) if hdg is not None else None
        else:  # reference=4: north-referenced direction (TWD)
            self._live["twd_deg"] = round(ang % 360, 1)
            self._live["twa_deg"] = round((ang - hdg + 360) % 360, 1) if hdg is not None else None

    def update_live(self, record: PGNRecord) -> None:
        """Update the in-memory live cache from a decoded record (no DB write)."""
        match record:
            case HeadingRecord():
                self._live["heading_deg"] = round(record.heading_deg, 1)
                self._recompute_true_wind()
            case SpeedRecord():
                self._live["bsp_kts"] = round(record.speed_kts, 2)
            case COGSOGRecord():
                self._live["cog_deg"] = round(record.cog_deg, 1)
                self._live["sog_kts"] = round(record.sog_kts, 2)
            case WindRecord() if record.reference == 2:  # apparent
                self._live["aws_kts"] = round(record.wind_speed_kts, 1)
                self._live["awa_deg"] = round(record.wind_angle_deg, 1)
            case WindRecord() if record.reference in (0, 4):  # true
                self._live["tws_kts"] = round(record.wind_speed_kts, 1)
                self._live_tw_ref = record.reference
                self._live_tw_angle_raw = record.wind_angle_deg
                self._recompute_true_wind()

    def live_instruments(self) -> dict[str, float | None]:
        """Return a snapshot of the current in-memory instrument cache."""
        return dict(self._live)

    async def connect(self) -> None:
        """Open the database connection."""
        self._db = await aiosqlite.connect(self._config.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        self._last_flush = time.monotonic()
        logger.info("Storage connected: {}", self._config.db_path)
        await self.migrate()
        current = await self.get_current_race()
        self._session_active = current is not None

    async def close(self) -> None:
        """Flush any buffered writes and close the database connection."""
        if self._db is not None:
            await self._flush()
            await self._db.close()
            self._db = None
            logger.info("Storage closed")

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not connected; call connect() first")
        return self._db

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def migrate(self) -> None:
        """Apply any pending schema migrations."""
        db = self._conn()

        # Ensure version table exists
        await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        await db.commit()

        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        current = row[0] if row and row[0] is not None else 0

        for version in sorted(_MIGRATIONS):
            if version <= current:
                continue
            logger.info("Applying schema migration v{}", version)
            await db.executescript(_MIGRATIONS[version])
            await db.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,)
            )
            await db.commit()

        logger.debug("Schema is at version {}", _CURRENT_VERSION)

    # ------------------------------------------------------------------
    # Write (batched)
    # ------------------------------------------------------------------

    async def write(self, record: PGNRecord) -> None:
        """Buffer a decoded PGN record; flushes to disk periodically."""
        match record:
            case HeadingRecord():
                await self._write_heading(record)
            case SpeedRecord():
                await self._write_speed(record)
            case DepthRecord():
                await self._write_depth(record)
            case PositionRecord():
                await self._write_position(record)
            case COGSOGRecord():
                await self._write_cogsog(record)
            case WindRecord():
                await self._write_wind(record)
            case EnvironmentalRecord():
                await self._write_environmental(record)
        self._pending += 1
        await self._auto_flush()

    async def _auto_flush(self) -> None:
        """Commit if the batch size or time interval threshold is reached."""
        now = time.monotonic()
        if self._pending >= _FLUSH_BATCH_SIZE or now - self._last_flush >= _FLUSH_INTERVAL_S:
            await self._flush()

    async def _flush(self) -> None:
        """Commit all pending writes to disk."""
        if self._pending == 0:
            return
        db = self._conn()
        await db.commit()
        logger.debug("Flushed {} records to SQLite", self._pending)
        self._pending = 0
        self._last_flush = time.monotonic()

    async def _write_heading(self, r: HeadingRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg, deviation_deg, variation_deg)"
            " VALUES (?, ?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.heading_deg, r.deviation_deg, r.variation_deg),
        )

    async def _write_speed(self, r: SpeedRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.speed_kts),
        )

    async def _write_depth(self, r: DepthRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO depths (ts, source_addr, depth_m, offset_m) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.depth_m, r.offset_m),
        )

    async def _write_position(self, r: PositionRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.latitude_deg, r.longitude_deg),
        )

    async def _write_cogsog(self, r: COGSOGRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.cog_deg, r.sog_kts),
        )

    async def _write_wind(self, r: WindRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                _ts(r.timestamp),
                r.source_addr,
                r.wind_speed_kts,
                r.wind_angle_deg,
                r.reference,
            ),
        )

    async def _write_environmental(self, r: EnvironmentalRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO environmental (ts, source_addr, water_temp_c) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.water_temp_c),
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def query_range(
        self,
        table: str,
        start: datetime,
        end: datetime,
        *,
        race_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all rows in [start, end] from the given table.

        Timestamps are compared as ISO strings (lexicographic order works for
        UTC ISO 8601 with consistent formatting).

        If *race_id* is provided **and** the table has a ``race_id`` column,
        an additional filter is applied so that only rows belonging to that
        session are returned.  This prevents data from overlapping synthesized
        sessions from being mixed together.
        """
        _ALLOWED_TABLES = {
            "headings",
            "speeds",
            "depths",
            "positions",
            "cogsog",
            "winds",
            "environmental",
        }
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Unknown table: {table!r}")

        db = self._conn()
        params: list[Any] = [_ts(start), _ts(end)]
        sql = f"SELECT * FROM {table} WHERE ts >= ? AND ts <= ?"  # noqa: S608
        if race_id is not None and table != "environmental":
            sql += " AND race_id = ?"
            params.append(race_id)
        sql += " ORDER BY ts"
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def status_summary(self) -> dict[str, dict[str, Any]]:
        """Return row counts and last-seen timestamps for each data table."""
        _TABLES = [
            "headings",
            "speeds",
            "depths",
            "positions",
            "cogsog",
            "winds",
            "environmental",
        ]
        db = self._conn()
        result: dict[str, dict[str, Any]] = {}
        for table in _TABLES:
            cur = await db.execute(f"SELECT COUNT(*), MAX(ts) FROM {table}")  # noqa: S608
            row = await cur.fetchone()
            result[table] = {
                "count": row[0] if row else 0,
                "last_seen": row[1] if (row and row[1]) else "never",
            }
        return result

    # ------------------------------------------------------------------
    # Video sessions
    # ------------------------------------------------------------------

    async def write_video_session(self, session: VideoSession) -> None:
        """Persist a VideoSession to the video_sessions table."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        await db.execute(
            "INSERT INTO video_sessions"
            " (url, video_id, title, duration_s, sync_utc, sync_offset_s, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.url,
                session.video_id,
                session.title,
                session.duration_s,
                session.sync_utc.isoformat(),
                session.sync_offset_s,
                _datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()
        logger.info("Video session stored: {!r}", session.title)

    async def list_video_sessions(self) -> list[VideoSession]:
        """Return all stored VideoSessions ordered by sync_utc."""
        from datetime import datetime as _datetime

        db = self._conn()
        cur = await db.execute(
            "SELECT url, video_id, title, duration_s, sync_utc, sync_offset_s"
            " FROM video_sessions ORDER BY sync_utc"
        )
        rows = await cur.fetchall()
        return [
            VideoSession(
                url=row["url"],
                video_id=row["video_id"],
                title=row["title"],
                duration_s=row["duration_s"],
                sync_utc=_datetime.fromisoformat(row["sync_utc"]),
                sync_offset_s=row["sync_offset_s"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Weather
    # ------------------------------------------------------------------

    async def write_weather(self, reading: WeatherReading) -> None:
        """Persist a WeatherReading to the weather table."""
        db = self._conn()
        await db.execute(
            "INSERT INTO weather"
            " (ts, lat, lon, wind_speed_kts, wind_dir_deg, air_temp_c, pressure_hpa)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _ts(reading.timestamp),
                reading.lat,
                reading.lon,
                reading.wind_speed_kts,
                reading.wind_direction_deg,
                reading.air_temp_c,
                reading.pressure_hpa,
            ),
        )
        await db.commit()
        logger.debug("Weather reading stored: ts={}", _ts(reading.timestamp))

    async def query_weather_range(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Return all weather rows in [start, end] ordered by ts."""
        db = self._conn()
        cur = await db.execute(
            "SELECT * FROM weather WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (_ts(start), _ts(end)),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Tides
    # ------------------------------------------------------------------

    async def write_tide(self, reading: TideReading) -> None:
        """Persist a TideReading to the tides table.

        Uses INSERT OR IGNORE so re-fetching the same predictions is safe.
        """
        db = self._conn()
        await db.execute(
            "INSERT OR IGNORE INTO tides"
            " (ts, station_id, station_name, height_m, type)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                _ts(reading.timestamp),
                reading.station_id,
                reading.station_name,
                reading.height_m,
                reading.type,
            ),
        )
        await db.commit()

    async def query_tide_range(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Return all tide rows in [start, end] ordered by ts."""
        db = self._conn()
        cur = await db.execute(
            "SELECT * FROM tides WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (_ts(start), _ts(end)),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def latest_position(self) -> dict[str, Any] | None:
        """Return the most recent row from the positions table, or None."""
        db = self._conn()
        cur = await db.execute("SELECT * FROM positions ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None

    async def latest_instruments(self) -> dict[str, float | None]:
        """Return the most recent reading from each instrument table.

        Prefers the in-memory live cache (updated on every SK/CAN record) so
        the instruments panel stays current even without an active session.
        Falls back to DB queries only on startup before the first message.
        """
        if any(v is not None for v in self._live.values()):
            return self.live_instruments()

        conn = self._conn()

        async def _q(table: str, cols: str, where: str = "") -> Any:  # noqa: ANN401
            cur = await conn.execute(
                f"SELECT {cols} FROM {table} {where} ORDER BY ts DESC LIMIT 1"  # noqa: S608
            )
            return await cur.fetchone()

        hdg = await _q("headings", "heading_deg")
        spd = await _q("speeds", "speed_kts")
        cs = await _q("cogsog", "cog_deg, sog_kts")
        tw = await _q(
            "winds", "wind_speed_kts, wind_angle_deg, reference", "WHERE reference IN (0, 4)"
        )
        aw = await _q("winds", "wind_speed_kts, wind_angle_deg", "WHERE reference=2")

        heading = hdg["heading_deg"] if hdg else None
        twa: float | None = None
        twd: float | None = None
        tws: float | None = None

        if tw:
            tws = round(tw["wind_speed_kts"], 1)
            tw_ang = tw["wind_angle_deg"]
            if tw["reference"] == 0:
                # Boat-referenced angle (TWA); compute TWD from heading
                twa = round(tw_ang, 1)
                twd = round((heading + twa) % 360, 1) if heading is not None else None
            else:
                # reference=4: north-referenced direction (TWD); compute TWA from heading
                twd = round(tw_ang % 360, 1)
                twa = round((tw_ang - heading + 360) % 360, 1) if heading is not None else None

        return {
            "heading_deg": round(heading, 1) if heading is not None else None,
            "bsp_kts": round(spd["speed_kts"], 2) if spd else None,
            "cog_deg": round(cs["cog_deg"], 1) if cs else None,
            "sog_kts": round(cs["sog_kts"], 2) if cs else None,
            "tws_kts": tws,
            "twa_deg": twa,
            "twd_deg": twd,
            "aws_kts": round(aw["wind_speed_kts"], 1) if aw else None,
            "awa_deg": round(aw["wind_angle_deg"], 1) if aw else None,
        }

    # ------------------------------------------------------------------
    # Audio sessions
    # ------------------------------------------------------------------

    async def write_audio_session(
        self,
        session: AudioSession,
        *,
        race_id: int | None = None,
        session_type: str = "race",
        name: str | None = None,
    ) -> int:
        """Insert an audio session row and return the new row id.

        *race_id* links this recording to a race/practice row.
        *session_type* is ``"race"``, ``"practice"``, or ``"debrief"``.
        *name* is a human-readable label (e.g. the race name or debrief name).
        """
        from helmlog.audio import AudioSession as _AudioSession

        assert isinstance(session, _AudioSession)

        db = self._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions"
            " (file_path, device_name, start_utc, end_utc, sample_rate, channels,"
            "  race_id, session_type, name)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.file_path,
                session.device_name,
                session.start_utc.isoformat(),
                session.end_utc.isoformat() if session.end_utc else None,
                session.sample_rate,
                session.channels,
                race_id,
                session_type,
                name,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.debug("Audio session stored: id={} file={}", cur.lastrowid, session.file_path)
        return cur.lastrowid

    async def update_audio_session_end(self, session_id: int, end_utc: datetime) -> None:
        """Set the end_utc for an existing audio session row."""
        db = self._conn()
        await db.execute(
            "UPDATE audio_sessions SET end_utc = ? WHERE id = ?",
            (end_utc.isoformat(), session_id),
        )
        await db.commit()
        logger.debug("Audio session {} end_utc updated", session_id)

    async def get_audio_session_row(self, session_id: int) -> dict[str, Any] | None:
        """Return a single audio_sessions row as a dict, or None if not found."""
        cur = await self._conn().execute(
            "SELECT id, file_path, device_name, start_utc, end_utc, sample_rate, channels,"
            " race_id, session_type, name"
            " FROM audio_sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_audio_sessions(self) -> list[AudioSession]:
        """Return all audio sessions ordered by start_utc descending."""
        from datetime import datetime as _datetime

        from helmlog.audio import AudioSession as _AudioSession

        db = self._conn()
        cur = await db.execute(
            "SELECT id, file_path, device_name, start_utc, end_utc, sample_rate, channels"
            " FROM audio_sessions ORDER BY start_utc DESC"
        )
        rows = await cur.fetchall()
        return [
            _AudioSession(
                file_path=row["file_path"],
                device_name=row["device_name"],
                start_utc=_datetime.fromisoformat(row["start_utc"]),
                end_utc=_datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                sample_rate=row["sample_rate"],
                channels=row["channels"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Races
    # ------------------------------------------------------------------

    async def start_race(
        self,
        event: str,
        start_utc: datetime,
        date_str: str,
        race_num: int,
        name: str,
        session_type: str = "race",
    ) -> Race:
        """Auto-close any open race for the day, insert a new race row, and return it."""
        from helmlog.races import Race as _Race

        db = self._conn()

        # Close any open race for this UTC date
        open_cur = await db.execute(
            "SELECT id FROM races WHERE date = ? AND end_utc IS NULL",
            (date_str,),
        )
        open_row = await open_cur.fetchone()
        if open_row is not None:
            await db.execute(
                "UPDATE races SET end_utc = ? WHERE id = ?",
                (start_utc.isoformat(), open_row["id"]),
            )

        cur = await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (name, event, race_num, date_str, start_utc.isoformat(), session_type),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info("Race started: {} (id={}) type={}", name, cur.lastrowid, session_type)
        self._session_active = True
        return _Race(
            id=cur.lastrowid,
            name=name,
            event=event,
            race_num=race_num,
            date=date_str,
            start_utc=start_utc,
            end_utc=None,
            session_type=session_type,
        )

    async def end_race(self, race_id: int, end_utc: datetime) -> None:
        """Set end_utc on the given race row."""
        db = self._conn()
        await db.execute(
            "UPDATE races SET end_utc = ? WHERE id = ?",
            (end_utc.isoformat(), race_id),
        )
        await db.commit()
        self._session_active = False
        logger.info("Race {} ended at {}", race_id, end_utc.isoformat())

    async def has_source_id(self, source: str, source_id: str) -> bool:
        """Check if a race with this source/source_id already exists (dedup)."""
        db = self._conn()
        cur = await db.execute(
            "SELECT 1 FROM races WHERE source = ? AND source_id = ?",
            (source, source_id),
        )
        return await cur.fetchone() is not None

    async def import_race(
        self,
        *,
        name: str,
        event: str,
        race_num: int,
        date_str: str,
        start_utc: datetime,
        end_utc: datetime,
        session_type: str,
        source: str,
        source_id: str,
        peer_fingerprint: str | None = None,
        peer_co_op_id: str | None = None,
    ) -> int:
        """Insert a backfilled race row with provenance metadata. Returns race_id."""
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(_UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc,"
            "  session_type, source, source_id, imported_at,"
            "  peer_fingerprint, peer_co_op_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                event,
                race_num,
                date_str,
                start_utc.isoformat(),
                end_utc.isoformat(),
                session_type,
                source,
                source_id,
                now,
                peer_fingerprint,
                peer_co_op_id,
            ),
        )
        await db.commit()
        race_id = cur.lastrowid
        assert race_id is not None
        logger.info("Imported race {} (source={}, source_id={})", race_id, source, source_id)
        return race_id

    async def import_synthesized_data(self, rows: list[Any], *, race_id: int) -> int:
        """Bulk-insert synthesized instrument data from SynthRow objects.

        Writes to: positions, headings, speeds, cogsog, depths, winds
        (both true and apparent). Returns the number of rows written.
        """
        db = self._conn()
        src_gps = 3  # synthetic GPS source address
        src_inst = 7  # synthetic instrument source address

        pos_rows = [(r.ts.isoformat(), src_gps, r.lat, r.lon, race_id) for r in rows]
        hdg_rows = [(r.ts.isoformat(), src_inst, r.heading, None, None, race_id) for r in rows]
        spd_rows = [(r.ts.isoformat(), src_inst, r.bsp, race_id) for r in rows]
        cs_rows = [(r.ts.isoformat(), src_gps, r.cog, r.sog, race_id) for r in rows]
        dep_rows = [(r.ts.isoformat(), src_inst, r.depth, None, race_id) for r in rows]
        # True wind (reference=0 = boat-referenced TWA)
        tw_rows = [(r.ts.isoformat(), src_inst, r.tws, r.twa, 0, race_id) for r in rows]
        # Apparent wind (reference=2)
        aw_rows = [(r.ts.isoformat(), src_inst, r.aws, r.awa, 2, race_id) for r in rows]

        await db.executemany(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            pos_rows,
        )
        await db.executemany(
            "INSERT INTO headings"
            " (ts, source_addr, heading_deg, deviation_deg, variation_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            hdg_rows,
        )
        await db.executemany(
            "INSERT INTO speeds (ts, source_addr, speed_kts, race_id) VALUES (?, ?, ?, ?)",
            spd_rows,
        )
        await db.executemany(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            cs_rows,
        )
        await db.executemany(
            "INSERT INTO depths (ts, source_addr, depth_m, offset_m, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            dep_rows,
        )
        await db.executemany(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            tw_rows,
        )
        await db.executemany(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            aw_rows,
        )
        await db.commit()
        logger.info("Imported {} synthesized data points", len(rows))
        return len(rows)

    async def import_track_points(
        self,
        points: list[tuple[str, float, float, float | None, float | None]],
    ) -> int:
        """Bulk-insert GPS points from a backfill source.

        Each tuple: (ts_iso, latitude_deg, longitude_deg, cog_deg | None, sog_kts | None).
        Inserts into both positions and cogsog tables.
        Returns number of points written.
        """
        db = self._conn()
        pos_rows = [(ts, 0, lat, lon) for ts, lat, lon, _, _ in points]
        await db.executemany(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            pos_rows,
        )

        cs_rows = [
            (ts, 0, cog, sog)
            for ts, _, _, cog, sog in points
            if cog is not None and sog is not None
        ]
        if cs_rows:
            await db.executemany(
                "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
                cs_rows,
            )
        await db.commit()
        return len(pos_rows)

    async def get_race(self, race_id: int) -> Race | None:
        """Return the race with the given id, or None if not found."""
        from datetime import datetime as _datetime

        from helmlog.races import Race as _Race

        db = self._conn()
        cur = await db.execute(
            "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
            " FROM races WHERE id = ?",
            (race_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _Race(
            id=row["id"],
            name=row["name"],
            event=row["event"],
            race_num=row["race_num"],
            date=row["date"],
            start_utc=_datetime.fromisoformat(row["start_utc"]),
            end_utc=_datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
            session_type=row["session_type"],
        )

    async def get_current_race(self) -> Race | None:
        """Return the most recent race with no end_utc, or None."""
        from datetime import datetime as _datetime

        from helmlog.races import Race as _Race

        db = self._conn()
        cur = await db.execute(
            "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
            " FROM races WHERE end_utc IS NULL ORDER BY start_utc DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _Race(
            id=row["id"],
            name=row["name"],
            event=row["event"],
            race_num=row["race_num"],
            date=row["date"],
            start_utc=_datetime.fromisoformat(row["start_utc"]),
            end_utc=None,
            session_type=row["session_type"],
        )

    async def list_races_for_date(self, date_str: str) -> list[Race]:
        """Return all races for a UTC date string, ordered by start_utc ASC."""
        from datetime import datetime as _datetime

        from helmlog.races import Race as _Race

        db = self._conn()
        cur = await db.execute(
            "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
            " FROM races WHERE date = ? ORDER BY start_utc ASC",
            (date_str,),
        )
        rows = await cur.fetchall()
        return [
            _Race(
                id=row["id"],
                name=row["name"],
                event=row["event"],
                race_num=row["race_num"],
                date=row["date"],
                start_utc=_datetime.fromisoformat(row["start_utc"]),
                end_utc=_datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                session_type=row["session_type"],
            )
            for row in rows
        ]

    async def list_races_in_range(self, start_utc: datetime, end_utc: datetime) -> list[Race]:
        """Return all races whose time window overlaps ``[start_utc, end_utc]``."""
        from datetime import datetime as _datetime

        from helmlog.races import Race as _Race

        db = self._conn()
        cur = await db.execute(
            "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
            " FROM races"
            " WHERE start_utc < ? AND (end_utc IS NULL OR end_utc > ?)"
            " ORDER BY start_utc ASC",
            (end_utc.isoformat(), start_utc.isoformat()),
        )
        rows = await cur.fetchall()
        return [
            _Race(
                id=row["id"],
                name=row["name"],
                event=row["event"],
                race_num=row["race_num"],
                date=row["date"],
                start_utc=_datetime.fromisoformat(row["start_utc"]),
                end_utc=_datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                session_type=row["session_type"],
            )
            for row in rows
        ]

    async def count_sessions_for_date(self, date_str: str, session_type: str) -> int:
        """Return the count of sessions of the given type for a UTC date string."""
        db = self._conn()
        cur = await db.execute(
            "SELECT COUNT(*) FROM races WHERE date = ? AND session_type = ?",
            (date_str, session_type),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_sessions(
        self,
        q: str | None = None,
        session_type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return (total_count, sessions) for the history browser.

        Sessions include races and practices (from the ``races`` table) and
        debriefs (from ``audio_sessions`` where ``session_type='debrief'``).
        Results are sorted newest-first.

        *session_type* may be ``"race"``, ``"practice"``, ``"debrief"``, or
        ``None`` for all types.
        """
        db = self._conn()

        include_races = session_type in (None, "race", "practice", "synthesized")
        include_debriefs = session_type in (None, "debrief")

        parts: list[str] = []
        params: list[Any] = []

        if include_races:
            race_where: list[str] = []
            race_params: list[Any] = []
            if session_type in ("race", "practice", "synthesized"):
                race_where.append("r.session_type = ?")
                race_params.append(session_type)
            else:
                race_where.append("r.session_type IN ('race', 'practice', 'synthesized')")
            if q:
                race_where.append("(r.name LIKE ? OR r.event LIKE ?)")
                like = f"%{q}%"
                race_params.extend([like, like])
            if from_date:
                race_where.append("r.date >= ?")
                race_params.append(from_date)
            if to_date:
                race_where.append("r.date <= ?")
                race_params.append(to_date)
            where = "WHERE " + " AND ".join(race_where)
            parts.append(
                f"SELECT r.id AS id, r.session_type AS type, r.name AS name,"
                f" r.event AS event, r.race_num AS race_num, r.date AS date,"
                f" r.start_utc AS start_utc, r.end_utc AS end_utc,"
                f" CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END AS has_audio,"
                f" a.id AS audio_session_id,"
                f" NULL AS parent_race_id, NULL AS parent_race_name,"
                f" (SELECT COUNT(*) > 0 FROM positions p"
                f"   WHERE p.ts >= r.start_utc AND p.ts <= COALESCE(r.end_utc, r.start_utc)"
                f" ) AS has_track,"
                f" (SELECT rv.youtube_url FROM race_videos rv"
                f"   WHERE rv.race_id = r.id LIMIT 1) AS first_video_url,"
                f" (SELECT COUNT(*) > 0 FROM transcripts t"
                f"   JOIN audio_sessions a2 ON a2.id = t.audio_session_id"
                f"   WHERE a2.race_id = r.id AND t.status = 'done'"
                f" ) AS has_transcript,"
                f" (SELECT COUNT(*) > 0 FROM race_results rr"
                f"   WHERE rr.race_id = r.id) AS has_results,"
                f" (SELECT COUNT(*) > 0 FROM race_crew rc"
                f"   WHERE rc.race_id = r.id) AS has_crew,"
                f" (SELECT COUNT(*) > 0 FROM race_sails rs"
                f"   WHERE rs.race_id = r.id) AS has_sails,"
                f" (SELECT COUNT(*) > 0 FROM session_notes sn"
                f"   WHERE sn.race_id = r.id) AS has_notes"
                f" FROM races r"
                f" LEFT JOIN audio_sessions a"
                f"   ON a.race_id = r.id AND a.session_type IN ('race', 'practice')"
                f" {where}"
            )
            params.extend(race_params)

        if include_debriefs:
            deb_where: list[str] = ["a.session_type = 'debrief'"]
            deb_params: list[Any] = []
            if q:
                deb_where.append("(a.name LIKE ? OR r.event LIKE ?)")
                like = f"%{q}%"
                deb_params.extend([like, like])
            if from_date:
                deb_where.append("substr(a.start_utc, 1, 10) >= ?")
                deb_params.append(from_date)
            if to_date:
                deb_where.append("substr(a.start_utc, 1, 10) <= ?")
                deb_params.append(to_date)
            where = "WHERE " + " AND ".join(deb_where)
            parts.append(
                f"SELECT a.id AS id, 'debrief' AS type,"
                f" COALESCE(a.name, a.file_path) AS name,"
                f" COALESCE(r.event, '') AS event,"
                f" r.race_num AS race_num,"
                f" COALESCE(r.date, substr(a.start_utc, 1, 10)) AS date,"
                f" a.start_utc AS start_utc, a.end_utc AS end_utc,"
                f" 1 AS has_audio, a.id AS audio_session_id,"
                f" r.id AS parent_race_id, r.name AS parent_race_name,"
                f" 0 AS has_track, NULL AS first_video_url,"
                f" (SELECT COUNT(*) > 0 FROM transcripts t"
                f"   WHERE t.audio_session_id = a.id AND t.status = 'done'"
                f" ) AS has_transcript,"
                f" 0 AS has_results, 0 AS has_crew, 0 AS has_sails, 0 AS has_notes"
                f" FROM audio_sessions a"
                f" LEFT JOIN races r ON r.id = a.race_id"
                f" {where}"
            )
            params.extend(deb_params)

        if not parts:
            return (0, [])

        union = " UNION ALL ".join(parts)

        count_cur = await db.execute(f"SELECT COUNT(*) FROM ({union})", params)
        count_row = await count_cur.fetchone()
        total = int(count_row[0]) if count_row else 0

        data_cur = await db.execute(
            f"SELECT * FROM ({union}) ORDER BY start_utc DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await data_cur.fetchall()

        from datetime import datetime as _datetime

        result: list[dict[str, Any]] = []
        for row in rows:
            start_utc = _datetime.fromisoformat(row["start_utc"])
            end_utc = _datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None
            duration_s = (end_utc - start_utc).total_seconds() if end_utc else None
            result.append(
                {
                    "id": row["id"],
                    "type": row["type"],
                    "name": row["name"],
                    "event": row["event"],
                    "race_num": row["race_num"],
                    "date": row["date"],
                    "start_utc": start_utc.isoformat(),
                    "end_utc": end_utc.isoformat() if end_utc else None,
                    "duration_s": round(duration_s, 1) if duration_s is not None else None,
                    "has_audio": bool(row["has_audio"]),
                    "audio_session_id": row["audio_session_id"],
                    "parent_race_id": row["parent_race_id"],
                    "parent_race_name": row["parent_race_name"],
                    "has_track": bool(row["has_track"]),
                    "first_video_url": row["first_video_url"],
                    "has_transcript": bool(row["has_transcript"]),
                    "has_results": bool(row["has_results"]),
                    "has_crew": bool(row["has_crew"]),
                    "has_sails": bool(row["has_sails"]),
                    "has_notes": bool(row["has_notes"]),
                    "crew": [],
                }
            )

        # Attach crew to all session types (debriefs inherit from parent race)
        for session in result:
            if session["type"] in ("race", "practice"):
                session["crew"] = await self.get_race_crew(session["id"])
            elif session["type"] == "debrief" and session.get("parent_race_id"):
                session["crew"] = await self.get_race_crew(session["parent_race_id"])
            else:
                session["crew"] = []

        return (total, result)

    async def get_daily_event(self, date_str: str) -> str | None:
        """Look up a stored custom event name for the given UTC date."""
        db = self._conn()
        cur = await db.execute("SELECT event_name FROM daily_events WHERE date = ?", (date_str,))
        row = await cur.fetchone()
        return row["event_name"] if row else None

    async def set_daily_event(self, date_str: str, event_name: str) -> None:
        """Upsert a custom event name for the given UTC date."""
        db = self._conn()
        await db.execute(
            "INSERT INTO daily_events (date, event_name) VALUES (?, ?)"
            " ON CONFLICT(date) DO UPDATE SET event_name = excluded.event_name",
            (date_str, event_name),
        )
        await db.commit()
        logger.debug("Daily event set: {} → {}", date_str, event_name)

    # ------------------------------------------------------------------
    # Event rules (day-of-week → event name)
    # ------------------------------------------------------------------

    async def list_event_rules(self) -> list[dict[str, Any]]:
        """Return all day-of-week event rules, ordered by weekday."""
        db = self._conn()
        cur = await db.execute("SELECT weekday, event_name FROM event_rules ORDER BY weekday")
        return [dict(row) for row in await cur.fetchall()]

    async def get_event_rule(self, weekday: int) -> str | None:
        """Return the event name for a weekday (0=Mon … 6=Sun), or None."""
        db = self._conn()
        cur = await db.execute("SELECT event_name FROM event_rules WHERE weekday = ?", (weekday,))
        row = await cur.fetchone()
        return row["event_name"] if row else None

    async def set_event_rule(self, weekday: int, event_name: str) -> None:
        """Upsert a day-of-week event rule."""
        db = self._conn()
        await db.execute(
            "INSERT INTO event_rules (weekday, event_name) VALUES (?, ?)"
            " ON CONFLICT(weekday) DO UPDATE SET event_name = excluded.event_name",
            (weekday, event_name),
        )
        await db.commit()

    async def delete_event_rule(self, weekday: int) -> bool:
        """Delete a day-of-week event rule. Returns True if a row was deleted."""
        db = self._conn()
        cur = await db.execute("DELETE FROM event_rules WHERE weekday = ?", (weekday,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Race crew
    # ------------------------------------------------------------------

    async def set_race_crew(self, race_id: int, crew: list[dict[str, str]]) -> None:
        """Set crew positions for a race (full-replace semantics).

        Each entry must have ``position`` and ``sailor`` keys.  Blank sailor
        names must be filtered by the caller before invoking this method.
        All existing positions not present in *crew* are deleted.
        Each sailor name is upserted into ``recent_sailors``.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now_str = _datetime.now(UTC).isoformat()

        if crew:
            positions = [c["position"] for c in crew]
            placeholders = ",".join("?" * len(positions))
            await db.execute(
                f"DELETE FROM race_crew WHERE race_id = ? AND position NOT IN ({placeholders})",
                (race_id, *positions),
            )
        else:
            await db.execute("DELETE FROM race_crew WHERE race_id = ?", (race_id,))

        for entry in crew:
            position = entry["position"]
            sailor = entry["sailor"]
            await db.execute(
                "INSERT OR REPLACE INTO race_crew (race_id, position, sailor) VALUES (?, ?, ?)",
                (race_id, position, sailor),
            )
            await db.execute(
                "INSERT INTO recent_sailors (sailor, last_used) VALUES (?, ?)"
                " ON CONFLICT(sailor) DO UPDATE SET last_used = excluded.last_used",
                (sailor, now_str),
            )

        await db.commit()
        logger.debug("Crew set for race {}: {} positions", race_id, len(crew))

    async def get_race_crew(self, race_id: int) -> list[dict[str, str]]:
        """Return crew for *race_id* ordered by canonical position (helm first)."""
        db = self._conn()
        cur = await db.execute(
            "SELECT position, sailor FROM race_crew WHERE race_id = ?",
            (race_id,),
        )
        rows = await cur.fetchall()
        position_order = {p: i for i, p in enumerate(_POSITIONS)}
        result = [{"position": row["position"], "sailor": row["sailor"]} for row in rows]
        result.sort(key=lambda x: position_order.get(x["position"], 99))
        return result

    async def get_recent_sailors(self, limit: int = 10) -> list[str]:
        """Return the most recently used sailor names, newest first."""
        db = self._conn()
        cur = await db.execute(
            "SELECT sailor FROM recent_sailors ORDER BY last_used DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [row["sailor"] for row in rows]

    async def get_last_session_crew(self) -> list[dict[str, str]]:
        """Return crew from the most recently ended race/practice, or [] if none."""
        db = self._conn()
        cur = await db.execute(
            "SELECT position, sailor FROM race_crew"
            " WHERE race_id = ("
            "   SELECT id FROM races WHERE end_utc IS NOT NULL"
            "   ORDER BY end_utc DESC LIMIT 1"
            " )"
        )
        rows = await cur.fetchall()
        position_order = {p: i for i, p in enumerate(_POSITIONS)}
        result = [{"position": row["position"], "sailor": row["sailor"]} for row in rows]
        result.sort(key=lambda x: position_order.get(x["position"], 99))
        return result

    # ------------------------------------------------------------------
    # Boat registry
    # ------------------------------------------------------------------

    async def add_boat(
        self,
        sail_number: str,
        name: str | None,
        class_name: str | None,
    ) -> int:
        """Insert a new boat and return its id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now_str = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO boats (sail_number, name, class, last_used) VALUES (?, ?, ?, ?)",
            (sail_number.strip(), name, class_name, now_str),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.debug("Boat added: sail_number={} id={}", sail_number, cur.lastrowid)
        return cur.lastrowid

    async def find_or_create_boat(self, sail_number: str) -> int:
        """Return existing boat id, or insert a minimal boat and return its id."""
        db = self._conn()
        cur = await db.execute("SELECT id FROM boats WHERE sail_number = ?", (sail_number.strip(),))
        row = await cur.fetchone()
        if row is not None:
            return int(row["id"])
        return await self.add_boat(sail_number, None, None)

    async def list_boats(
        self,
        *,
        exclude_race_id: int | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return boats sorted by last_used desc (MRU order).

        *exclude_race_id*: omit boats already placed in this race.
        *q*: substring search on sail_number or name.
        """
        db = self._conn()
        where_parts: list[str] = []
        params: list[Any] = []

        if q:
            where_parts.append("(sail_number LIKE ? OR name LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])

        if exclude_race_id is not None:
            where_parts.append("id NOT IN (SELECT boat_id FROM race_results WHERE race_id = ?)")
            params.append(exclude_race_id)

        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        cur = await db.execute(
            f"SELECT id, sail_number, name, class, last_used FROM boats"  # noqa: S608
            f" {where} ORDER BY last_used IS NULL, last_used DESC",
            params,
        )
        rows = await cur.fetchall()
        return [
            {
                "id": row["id"],
                "sail_number": row["sail_number"],
                "name": row["name"],
                "class": row["class"],
                "last_used": row["last_used"],
            }
            for row in rows
        ]

    async def update_boat(
        self,
        boat_id: int,
        sail_number: str,
        name: str | None,
        class_name: str | None,
    ) -> None:
        """Update an existing boat's fields."""
        db = self._conn()
        await db.execute(
            "UPDATE boats SET sail_number = ?, name = ?, class = ? WHERE id = ?",
            (sail_number.strip(), name, class_name, boat_id),
        )
        await db.commit()
        logger.debug("Boat {} updated: sail_number={}", boat_id, sail_number)

    async def delete_boat(self, boat_id: int) -> None:
        """Delete a boat by id."""
        db = self._conn()
        await db.execute("DELETE FROM boats WHERE id = ?", (boat_id,))
        await db.commit()
        logger.debug("Boat {} deleted", boat_id)

    # ------------------------------------------------------------------
    # Race results
    # ------------------------------------------------------------------

    async def upsert_race_result(
        self,
        race_id: int,
        place: int,
        boat_id: int,
        *,
        finish_time: str | None = None,
        dnf: bool = False,
        dns: bool = False,
        notes: str | None = None,
    ) -> int:
        """Insert or replace a race result row and update boat.last_used.

        UNIQUE constraints on (race_id, place) and (race_id, boat_id) are
        resolved by INSERT OR REPLACE, which removes conflicting rows first.

        Returns:
            The id of the upserted row.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now_str = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT OR REPLACE INTO race_results"
            " (race_id, place, boat_id, finish_time, dnf, dns, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (race_id, place, boat_id, finish_time, int(dnf), int(dns), notes, now_str),
        )
        await db.execute(
            "UPDATE boats SET last_used = ? WHERE id = ?",
            (now_str, boat_id),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.debug("Race result upserted: race={} place={} boat={}", race_id, place, boat_id)
        return cur.lastrowid

    async def list_race_results(self, race_id: int) -> list[dict[str, Any]]:
        """Return results for *race_id* ordered by place."""
        db = self._conn()
        cur = await db.execute(
            "SELECT rr.id, rr.race_id, rr.place, rr.boat_id,"
            " b.sail_number, b.name AS boat_name, b.class AS boat_class,"
            " rr.finish_time, rr.dnf, rr.dns, rr.notes, rr.created_at"
            " FROM race_results rr"
            " JOIN boats b ON b.id = rr.boat_id"
            " WHERE rr.race_id = ?"
            " ORDER BY rr.place ASC",
            (race_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": row["id"],
                "race_id": row["race_id"],
                "place": row["place"],
                "boat_id": row["boat_id"],
                "sail_number": row["sail_number"],
                "boat_name": row["boat_name"],
                "boat_class": row["boat_class"],
                "finish_time": row["finish_time"],
                "dnf": bool(row["dnf"]),
                "dns": bool(row["dns"]),
                "notes": row["notes"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def delete_race_result(self, result_id: int) -> None:
        """Delete a single race result row by id."""
        db = self._conn()
        await db.execute("DELETE FROM race_results WHERE id = ?", (result_id,))
        await db.commit()
        logger.debug("Race result {} deleted", result_id)

    # ------------------------------------------------------------------
    # Session notes
    # ------------------------------------------------------------------

    async def create_note(
        self,
        ts: str,
        body: str | None,
        *,
        race_id: int | None = None,
        audio_session_id: int | None = None,
        note_type: str = "text",
        photo_path: str | None = None,
        user_id: int | None = None,
    ) -> int:
        """Insert a new note and return its id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now_str = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO session_notes"
            " (race_id, audio_session_id, ts, note_type, body, photo_path, created_at, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (race_id, audio_session_id, ts, note_type, body, photo_path, now_str, user_id),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.debug("Note created: id={} race_id={} type={}", cur.lastrowid, race_id, note_type)
        return cur.lastrowid

    async def list_notes(
        self,
        race_id: int | None = None,
        audio_session_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return notes for a session ordered by ts ASC.

        Exactly one of *race_id* or *audio_session_id* must be supplied.
        """
        if race_id is None and audio_session_id is None:
            raise ValueError("Either race_id or audio_session_id must be supplied")
        db = self._conn()
        if race_id is not None:
            cur = await db.execute(
                "SELECT id, race_id, audio_session_id, ts, note_type, body,"
                " photo_path, created_at"
                " FROM session_notes WHERE race_id = ? ORDER BY ts ASC",
                (race_id,),
            )
        else:
            cur = await db.execute(
                "SELECT id, race_id, audio_session_id, ts, note_type, body,"
                " photo_path, created_at"
                " FROM session_notes WHERE audio_session_id = ? ORDER BY ts ASC",
                (audio_session_id,),
            )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def delete_note(self, note_id: int) -> bool:
        """Delete a note by id.  Returns True if deleted, False if not found."""
        db = self._conn()
        cur = await db.execute("DELETE FROM session_notes WHERE id = ?", (note_id,))
        await db.commit()
        deleted = (cur.rowcount or 0) > 0
        logger.debug("Note {} {}", note_id, "deleted" if deleted else "not found")
        return deleted

    async def list_notes_range(
        self,
        start: datetime,
        end: datetime,
        *,
        race_id: int | None = None,
        audio_session_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return notes whose ts falls in [start, end], ordered by ts ASC.

        Optionally scoped to a single race or audio session.
        Used by the Grafana annotations endpoint.
        """
        db = self._conn()
        where = "ts >= ? AND ts <= ?"
        params: list[object] = [start.isoformat(), end.isoformat()]
        if race_id is not None:
            where += " AND race_id = ?"
            params.append(race_id)
        elif audio_session_id is not None:
            where += " AND audio_session_id = ?"
            params.append(audio_session_id)
        cur = await db.execute(
            "SELECT id, race_id, audio_session_id, ts, note_type, body,"
            f" photo_path, created_at FROM session_notes WHERE {where} ORDER BY ts ASC",
            params,
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def list_settings_keys(self) -> list[str]:
        """Return all distinct keys used in settings notes, sorted alphabetically.

        Parses the JSON body of every saved settings note and collects the union
        of all keys across all sessions.  Used to populate the typeahead datalist
        on the settings note entry form.
        """
        db = self._conn()
        cur = await db.execute(
            "SELECT body FROM session_notes WHERE note_type = 'settings' AND body IS NOT NULL"
        )
        rows = await cur.fetchall()
        keys: set[str] = set()
        for (body,) in rows:
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    keys.update(obj.keys())
            except (json.JSONDecodeError, ValueError):
                pass
        return sorted(keys)

    # ------------------------------------------------------------------
    # Race videos
    # ------------------------------------------------------------------

    async def add_race_video(
        self,
        race_id: int,
        youtube_url: str,
        video_id: str,
        title: str,
        label: str,
        sync_utc: datetime,
        sync_offset_s: float,
        duration_s: float | None = None,
        user_id: int | None = None,
    ) -> int:
        """Add a YouTube video linked to a race.  Returns the new row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now_str = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO race_videos"
            " (race_id, youtube_url, video_id, title, label,"
            " sync_utc, sync_offset_s, duration_s, created_at, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                race_id,
                youtube_url,
                video_id,
                title,
                label,
                sync_utc.isoformat(),
                sync_offset_s,
                duration_s,
                now_str,
                user_id,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info(
            "Race video added: id={} race_id={} video_id={}", cur.lastrowid, race_id, video_id
        )  # noqa: E501
        return cur.lastrowid

    async def list_race_videos(self, race_id: int) -> list[dict[str, Any]]:
        """Return all videos linked to a race, ordered by created_at ASC."""
        db = self._conn()
        cur = await db.execute(
            "SELECT id, race_id, youtube_url, video_id, title, label,"
            " sync_utc, sync_offset_s, duration_s, created_at"
            " FROM race_videos WHERE race_id = ? ORDER BY created_at ASC",
            (race_id,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def update_race_video(
        self,
        video_row_id: int,
        *,
        label: str | None = None,
        sync_utc: datetime | None = None,
        sync_offset_s: float | None = None,
    ) -> bool:
        """Update mutable fields on a race video.  Returns True if found."""
        updates: list[str] = []
        params: list[object] = []
        if label is not None:
            updates.append("label = ?")
            params.append(label)
        if sync_utc is not None:
            updates.append("sync_utc = ?")
            params.append(sync_utc.isoformat())
        if sync_offset_s is not None:
            updates.append("sync_offset_s = ?")
            params.append(sync_offset_s)
        if not updates:
            return True  # nothing to do
        params.append(video_row_id)
        db = self._conn()
        cur = await db.execute(
            f"UPDATE race_videos SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
            params,
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_race_video(self, video_row_id: int) -> bool:
        """Delete a race video by id.  Returns True if deleted."""
        db = self._conn()
        cur = await db.execute("DELETE FROM race_videos WHERE id = ?", (video_row_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Camera sessions (#98)
    # ------------------------------------------------------------------

    async def add_camera_session(
        self,
        session_id: int,
        camera_name: str,
        camera_ip: str,
        started_utc: datetime | None,
        sync_offset_ms: int | None,
        error: str | None,
    ) -> int:
        """Record a camera session start. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO camera_sessions"
            " (session_id, camera_name, camera_ip,"
            "  recording_started_utc, sync_offset_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                camera_name,
                camera_ip,
                started_utc.isoformat() if started_utc else None,
                sync_offset_ms,
                error,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info(
            "Camera session added: id={} session={} camera={}",
            cur.lastrowid,
            session_id,
            camera_name,
        )
        return cur.lastrowid

    async def update_camera_session_stop(
        self,
        session_id: int,
        camera_name: str,
        stopped_utc: datetime | None,
        error: str | None,
    ) -> bool:
        """Update a camera session with stop time. Returns True if row was found."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE camera_sessions"
            " SET recording_stopped_utc = ?, error = COALESCE(?, error)"
            " WHERE session_id = ? AND camera_name = ?"
            " AND recording_stopped_utc IS NULL",
            (
                stopped_utc.isoformat() if stopped_utc else None,
                error,
                session_id,
                camera_name,
            ),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def list_camera_sessions(self, session_id: int) -> list[dict[str, Any]]:
        """Return all camera sessions for a race, ordered by camera_name."""
        db = self._conn()
        cur = await db.execute(
            "SELECT id, session_id, camera_name, camera_ip,"
            " recording_started_utc, recording_stopped_utc,"
            " sync_offset_ms, error"
            " FROM camera_sessions WHERE session_id = ?"
            " ORDER BY camera_name ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def list_unlinked_camera_sessions(self) -> list[dict[str, Any]]:
        """Return camera sessions that have no matching race_video entry.

        Used by ``sync-videos`` CLI to find recordings not yet linked to
        a YouTube video.
        """
        db = self._conn()
        cur = await db.execute(
            "SELECT cs.id, cs.session_id, cs.camera_name, cs.camera_ip,"
            " cs.recording_started_utc, cs.recording_stopped_utc,"
            " cs.sync_offset_ms, cs.error,"
            " r.name AS race_name"
            " FROM camera_sessions cs"
            " JOIN races r ON r.id = cs.session_id"
            " LEFT JOIN race_videos rv"
            "   ON rv.race_id = cs.session_id"
            " WHERE rv.id IS NULL"
            "   AND cs.recording_started_utc IS NOT NULL"
            " ORDER BY cs.recording_started_utc DESC",
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Camera configuration (#147)
    # ------------------------------------------------------------------

    async def list_cameras(self) -> list[dict[str, Any]]:
        """Return all configured cameras, ordered by name."""
        db = self._conn()
        cur = await db.execute(
            "SELECT id, name, ip, model, wifi_ssid, wifi_password FROM cameras ORDER BY name ASC"
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def add_camera(
        self,
        name: str,
        ip: str,
        model: str = "insta360-x4",
        wifi_ssid: str | None = None,
        wifi_password: str | None = None,
    ) -> int:
        """Add a camera. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO cameras (name, ip, model, wifi_ssid, wifi_password)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, ip, model, wifi_ssid, wifi_password),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info("Camera added: id={} name={} ip={}", cur.lastrowid, name, ip)
        return cur.lastrowid

    async def update_camera(
        self,
        name: str,
        ip: str,
        model: str | None = None,
        wifi_ssid: str | None = None,
        wifi_password: str | None = None,
    ) -> bool:
        """Update a camera's IP and optional fields by name. Returns True if found."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE cameras SET ip = ?, model = COALESCE(?, model),"
            " wifi_ssid = ?, wifi_password = ? WHERE name = ?",
            (ip, model, wifi_ssid, wifi_password, name),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def rename_camera(
        self,
        old_name: str,
        new_name: str,
        ip: str,
        model: str | None = None,
        wifi_ssid: str | None = None,
        wifi_password: str | None = None,
    ) -> bool:
        """Rename a camera and update its IP. Returns True if found."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE cameras SET name = ?, ip = ?, model = COALESCE(?, model),"
            " wifi_ssid = ?, wifi_password = ? WHERE name = ?",
            (new_name, ip, model, wifi_ssid, wifi_password, old_name),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_camera(self, name: str) -> bool:
        """Delete a camera by name. Returns True if found."""
        db = self._conn()
        cur = await db.execute("DELETE FROM cameras WHERE name = ?", (name,))
        await db.commit()
        deleted = (cur.rowcount or 0) > 0
        if deleted:
            logger.info("Camera deleted: name={}", name)
        return deleted

    async def seed_cameras_from_env(self, cameras_str: str) -> int:
        """Seed the cameras table from the CAMERAS env var if table is empty.

        Returns the number of cameras seeded.
        """
        db = self._conn()
        cur = await db.execute("SELECT COUNT(*) FROM cameras")
        row = await cur.fetchone()
        assert row is not None
        if row[0] > 0:
            return 0

        from helmlog.cameras import parse_cameras_config

        cameras = parse_cameras_config(cameras_str)
        count = 0
        for cam in cameras:
            await db.execute(
                "INSERT OR IGNORE INTO cameras (name, ip, model) VALUES (?, ?, ?)",
                (cam.name, cam.ip, cam.model),
            )
            count += 1
        await db.commit()
        if count:
            logger.info("Seeded {} camera(s) from CAMERAS env var", count)
        return count

    # ------------------------------------------------------------------
    # Sail inventory
    # ------------------------------------------------------------------

    async def add_sail(
        self,
        sail_type: str,
        name: str,
        notes: str | None = None,
    ) -> int:
        """Insert a sail into the inventory and return its id.

        Raises ``ValueError`` on duplicate (type, name).
        """
        db = self._conn()
        try:
            cur = await db.execute(
                "INSERT INTO sails (type, name, notes) VALUES (?, ?, ?)",
                (sail_type, name.strip(), notes),
            )
            await db.commit()
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ValueError(f"Sail already exists: type={sail_type!r} name={name!r}") from exc
            raise
        assert cur.lastrowid is not None
        logger.debug("Sail added: type={} name={} id={}", sail_type, name, cur.lastrowid)
        return cur.lastrowid

    async def list_sails(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        """Return sails ordered by type then name.

        By default only active sails are returned.  Pass *include_inactive=True*
        to include retired sails.
        """
        db = self._conn()
        where = "" if include_inactive else "WHERE active = 1"
        cur = await db.execute(
            f"SELECT id, type, name, notes, active FROM sails {where} ORDER BY type, name"
        )
        rows = await cur.fetchall()
        return [
            {
                "id": row["id"],
                "type": row["type"],
                "name": row["name"],
                "notes": row["notes"],
                "active": bool(row["active"]),
            }
            for row in rows
        ]

    async def update_sail(
        self,
        sail_id: int,
        *,
        name: str | None = None,
        notes: str | None = None,
        active: bool | None = None,
    ) -> bool:
        """Update sail fields.  Returns True if the sail was found and updated."""
        if name is None and notes is None and active is None:
            return True  # nothing to do — treat as no-op success
        db = self._conn()
        parts: list[str] = []
        params: list[Any] = []
        if name is not None:
            parts.append("name = ?")
            params.append(name.strip())
        if notes is not None:
            parts.append("notes = ?")
            params.append(notes)
        if active is not None:
            parts.append("active = ?")
            params.append(1 if active else 0)
        params.append(sail_id)
        cur = await db.execute(f"UPDATE sails SET {', '.join(parts)} WHERE id = ?", params)
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def set_race_sails(
        self,
        race_id: int,
        *,
        main_id: int | None,
        jib_id: int | None,
        spinnaker_id: int | None,
    ) -> None:
        """Replace the sail selection for *race_id*.

        Existing sail associations for the race are deleted and the provided
        non-None sail ids are re-inserted.
        """
        db = self._conn()
        await db.execute("DELETE FROM race_sails WHERE race_id = ?", (race_id,))
        for sail_id in (main_id, jib_id, spinnaker_id):
            if sail_id is not None:
                await db.execute(
                    "INSERT OR IGNORE INTO race_sails (race_id, sail_id) VALUES (?, ?)",
                    (race_id, sail_id),
                )
        await db.commit()
        logger.debug("Race sails set for race {}", race_id)

    async def get_race_sails(self, race_id: int) -> dict[str, Any]:
        """Return the sail selection for *race_id*.

        Returns a dict with keys ``main``, ``jib``, ``spinnaker``, each
        containing the full sail row dict or ``None`` if not set.
        """
        db = self._conn()
        cur = await db.execute(
            "SELECT s.id, s.type, s.name, s.notes, s.active"
            " FROM race_sails rs"
            " JOIN sails s ON s.id = rs.sail_id"
            " WHERE rs.race_id = ?",
            (race_id,),
        )
        rows = await cur.fetchall()
        result: dict[str, dict[str, Any] | None] = dict.fromkeys(_SAIL_TYPES)
        for row in rows:
            sail_type = row["type"]
            if sail_type in result:
                result[sail_type] = {
                    "id": row["id"],
                    "type": sail_type,
                    "name": row["name"],
                    "notes": row["notes"],
                    "active": bool(row["active"]),
                }
        return result

    # ------------------------------------------------------------------
    # Transcripts
    # ------------------------------------------------------------------

    async def create_transcript_job(self, audio_session_id: int, model: str) -> int:
        """Insert a transcript row in 'pending' status; return the new id.

        Raises ValueError if a transcript job already exists for this session.
        """
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        now = _datetime.now(_UTC).isoformat()
        db = self._conn()
        try:
            cur = await db.execute(
                "INSERT INTO transcripts"
                " (audio_session_id, status, model, created_utc, updated_utc)"
                " VALUES (?, 'pending', ?, ?, ?)",
                (audio_session_id, model, now, now),
            )
            await db.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid
        except aiosqlite.IntegrityError as exc:
            raise ValueError(
                f"Transcript job already exists for audio_session_id={audio_session_id}"
            ) from exc

    async def update_transcript(
        self,
        transcript_id: int,
        *,
        status: str,
        text: str | None = None,
        error_msg: str | None = None,
        segments_json: str | None = None,
    ) -> None:
        """Update the status (and optionally text/error_msg/segments_json) of a transcript row."""
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        now = _datetime.now(_UTC).isoformat()
        db = self._conn()
        await db.execute(
            "UPDATE transcripts SET status=?, text=?, error_msg=?, segments_json=?, updated_utc=?"
            " WHERE id=?",
            (status, text, error_msg, segments_json, now, transcript_id),
        )
        await db.commit()

    async def get_transcript(self, audio_session_id: int) -> dict[str, Any] | None:
        """Return the transcript row for *audio_session_id*, or None if not found."""
        cur = await self._conn().execute(
            "SELECT id, audio_session_id, status, text, error_msg, model,"
            " created_utc, updated_utc, segments_json"
            " FROM transcripts WHERE audio_session_id = ?",
            (audio_session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Polar baseline
    # ------------------------------------------------------------------

    async def get_polar_point(self, tws_bin: int, twa_bin: int) -> dict[str, Any] | None:
        """Return the polar_baseline row for *(tws_bin, twa_bin)*, or None."""
        cur = await self._conn().execute(
            "SELECT tws_bin, twa_bin, mean_bsp, p90_bsp, session_count, sample_count"
            " FROM polar_baseline WHERE tws_bin = ? AND twa_bin = ?",
            (tws_bin, twa_bin),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_polar_baseline(self, rows: list[dict[str, Any]], built_at: str) -> None:
        """Replace all polar_baseline rows with the freshly computed set."""
        db = self._conn()
        await db.execute("DELETE FROM polar_baseline")
        for row in rows:
            await db.execute(
                "INSERT INTO polar_baseline"
                " (tws_bin, twa_bin, mean_bsp, p90_bsp, session_count, sample_count, built_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["tws_bin"],
                    row["twa_bin"],
                    row["mean_bsp"],
                    row["p90_bsp"],
                    row["session_count"],
                    row["sample_count"],
                    built_at,
                ),
            )
        await db.commit()

    # ------------------------------------------------------------------
    # Maneuvers
    # ------------------------------------------------------------------

    async def write_maneuvers(self, session_id: int, maneuvers: list[Any]) -> None:
        """Replace all maneuvers for a session with the new list (idempotent)."""
        import json

        db = self._conn()
        await db.execute("DELETE FROM maneuvers WHERE session_id = ?", (session_id,))
        for m in maneuvers:
            await db.execute(
                "INSERT INTO maneuvers"
                " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
                "  vmg_loss_kts, tws_bin, twa_bin, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    m.type,
                    m.ts.isoformat(),
                    m.end_ts.isoformat() if m.end_ts is not None else None,
                    m.duration_sec,
                    m.loss_kts,
                    m.vmg_loss_kts,
                    m.tws_bin,
                    m.twa_bin,
                    json.dumps(m.details) if m.details else None,
                ),
            )
        await db.commit()

    async def get_session_maneuvers(self, session_id: int) -> list[dict[str, Any]]:
        """Return all stored maneuvers for a session, ordered by timestamp."""
        cur = await self._conn().execute(
            "SELECT id, session_id, type, ts, end_ts, duration_sec, loss_kts,"
            " vmg_loss_kts, tws_bin, twa_bin, details"
            " FROM maneuvers WHERE session_id = ? ORDER BY ts",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Synthesized wind field params and course marks
    # ------------------------------------------------------------------

    async def save_synth_wind_params(self, session_id: int, params: dict[str, Any]) -> None:
        """Persist wind field constructor parameters for a synthesized session."""
        db = self._conn()
        await db.execute(
            "INSERT OR REPLACE INTO synth_wind_params"
            " (session_id, seed, base_twd, tws_low, tws_high,"
            "  shift_interval_lo, shift_interval_hi,"
            "  shift_magnitude_lo, shift_magnitude_hi,"
            "  ref_lat, ref_lon, duration_s,"
            "  course_type, leg_distance_nm, laps, mark_sequence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                params["seed"],
                params["base_twd"],
                params["tws_low"],
                params["tws_high"],
                params["shift_interval_lo"],
                params["shift_interval_hi"],
                params["shift_magnitude_lo"],
                params["shift_magnitude_hi"],
                params["ref_lat"],
                params["ref_lon"],
                params["duration_s"],
                params["course_type"],
                params.get("leg_distance_nm"),
                params.get("laps"),
                params.get("mark_sequence"),
            ),
        )
        await db.commit()

    async def get_synth_wind_params(self, session_id: int) -> dict[str, Any] | None:
        """Return wind field parameters for a synthesized session, or None."""
        cur = await self._conn().execute(
            "SELECT * FROM synth_wind_params WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def save_synth_course_marks(self, session_id: int, marks: list[dict[str, Any]]) -> None:
        """Persist course mark positions for a synthesized session."""
        db = self._conn()
        await db.execute("DELETE FROM synth_course_marks WHERE session_id = ?", (session_id,))
        for m in marks:
            await db.execute(
                "INSERT INTO synth_course_marks"
                " (session_id, mark_key, mark_name, lat, lon)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, m["mark_key"], m["mark_name"], m["lat"], m["lon"]),
            )
        await db.commit()

    async def get_synth_course_marks(self, session_id: int) -> list[dict[str, Any]]:
        """Return course marks for a synthesized session."""
        cur = await self._conn().execute(
            "SELECT mark_key, mark_name, lat, lon"
            " FROM synth_course_marks WHERE session_id = ? ORDER BY mark_key",
            (session_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Auth: users, invite tokens, sessions
    # ------------------------------------------------------------------

    async def create_user(self, email: str, name: str | None, role: str) -> int:
        """Insert a new user and return the new id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO users (email, name, role, created_at) VALUES (?, ?, ?, ?)",
            (email.lower().strip(), name, role, now),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        cols = "id, email, name, role, created_at, last_seen, avatar_path"
        cur = await self._conn().execute(
            f"SELECT {cols} FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        cols = "id, email, name, role, created_at, last_seen, avatar_path"
        cur = await self._conn().execute(
            f"SELECT {cols} FROM users WHERE email = ?",
            (email.lower().strip(),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_user_role(self, user_id: int, role: str) -> None:
        db = self._conn()
        await db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        await db.commit()

    async def update_user_last_seen(self, user_id: int) -> None:
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, user_id))
        await db.commit()

    async def list_users(self) -> list[dict[str, Any]]:
        cur = await self._conn().execute(
            "SELECT id, email, name, role, created_at, last_seen FROM users ORDER BY created_at"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def create_invite_token(
        self,
        token: str,
        email: str,
        role: str,
        created_by: int | None,
        expires_at: str,
    ) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO invite_tokens (token, email, role, created_by, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (token, email.lower().strip(), role, created_by, expires_at),
        )
        await db.commit()

    async def get_invite_token(self, token: str) -> dict[str, Any] | None:
        cur = await self._conn().execute(
            "SELECT token, email, role, created_by, expires_at, used_at"
            " FROM invite_tokens WHERE token = ?",
            (token,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def redeem_invite_token(self, token: str) -> None:
        """Mark the token as used (sets used_at to now)."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE invite_tokens SET used_at = ? WHERE token = ?", (now, token))
        await db.commit()

    async def count_recent_tokens_for_email(self, email: str, window_hours: int = 1) -> int:
        """Count invite tokens created for *email* in the last *window_hours* hours.

        Since ``invite_tokens`` has no ``created_at`` column, we derive creation
        time from ``expires_at`` (tokens expire 7 days after creation).  A token
        created within the window has ``expires_at > now + 7d - window``.
        """
        from datetime import UTC, timedelta
        from datetime import datetime as _dt

        threshold = (_dt.now(UTC) + timedelta(days=7) - timedelta(hours=window_hours)).isoformat()
        cur = await self._conn().execute(
            "SELECT COUNT(*) FROM invite_tokens WHERE email = ? AND expires_at > ?",
            (email.lower().strip(), threshold),
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def create_session(
        self,
        session_id: str,
        user_id: int,
        expires_at: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO auth_sessions"
            " (session_id, user_id, created_at, expires_at, ip, user_agent)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, now, expires_at, ip, user_agent),
        )
        await db.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = await self._conn().execute(
            "SELECT session_id, user_id, created_at, expires_at, ip, user_agent"
            " FROM auth_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_session(self, session_id: str) -> None:
        db = self._conn()
        await db.execute("DELETE FROM auth_sessions WHERE session_id = ?", (session_id,))
        await db.commit()

    async def list_auth_sessions(self, user_id: int | None = None) -> list[dict[str, Any]]:
        if user_id is not None:
            cur = await self._conn().execute(
                "SELECT session_id, user_id, created_at, expires_at, ip, user_agent"
                " FROM auth_sessions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            cur = await self._conn().execute(
                "SELECT s.session_id, s.user_id, s.created_at, s.expires_at, s.ip, s.user_agent,"
                " u.email, u.name, u.role"
                " FROM auth_sessions s JOIN users u ON s.user_id = u.id"
                " ORDER BY s.created_at DESC"
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_expired_sessions(self) -> None:
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
        await db.commit()

    # ------------------------------------------------------------------
    # Audit log (#93)
    # ------------------------------------------------------------------

    async def log_action(
        self,
        action: str,
        *,
        detail: str | None = None,
        user_id: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> int:
        """Insert an audit log entry. Returns the row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        ts = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO audit_log (ts, user_id, action, detail, ip_address, user_agent)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts, user_id, action, detail, ip_address, user_agent),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def list_audit_log(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """Return recent audit log entries, newest first."""
        cur = await self._conn().execute(
            "SELECT a.id, a.ts, a.action, a.detail, a.ip_address, a.user_agent,"
            " a.user_id, u.name AS user_name, u.email AS user_email"
            " FROM audit_log a LEFT JOIN users u ON a.user_id = u.id"
            " ORDER BY a.ts DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Tags (#99)
    # ------------------------------------------------------------------

    async def create_tag(self, name: str, color: str | None = None) -> int:
        """Create a tag. Returns the tag id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        name = name.strip().lower()
        ts = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO tags (name, color, created_at) VALUES (?, ?, ?)",
            (name, color, ts),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_tag_by_name(self, name: str) -> dict[str, Any] | None:
        """Fetch a tag by name (case-insensitive)."""
        cur = await self._conn().execute(
            "SELECT id, name, color, created_at FROM tags WHERE name = ?",
            (name.strip().lower(),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_tags(self) -> list[dict[str, Any]]:
        """Return all tags with usage counts."""
        cur = await self._conn().execute(
            "SELECT t.id, t.name, t.color, t.created_at,"
            " (SELECT COUNT(*) FROM session_tags st WHERE st.tag_id = t.id) AS session_count,"
            " (SELECT COUNT(*) FROM note_tags nt WHERE nt.tag_id = t.id) AS note_count"
            " FROM tags t ORDER BY t.name"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_session_tag(self, session_id: int, tag_id: int) -> None:
        """Tag a session. Idempotent."""
        db = self._conn()
        await db.execute(
            "INSERT OR IGNORE INTO session_tags (session_id, tag_id) VALUES (?, ?)",
            (session_id, tag_id),
        )
        await db.commit()

    async def remove_session_tag(self, session_id: int, tag_id: int) -> None:
        """Remove a tag from a session."""
        db = self._conn()
        await db.execute(
            "DELETE FROM session_tags WHERE session_id = ? AND tag_id = ?",
            (session_id, tag_id),
        )
        await db.commit()

    async def get_session_tags(self, session_id: int) -> list[dict[str, Any]]:
        """Return tags for a session."""
        cur = await self._conn().execute(
            "SELECT t.id, t.name, t.color FROM tags t"
            " JOIN session_tags st ON t.id = st.tag_id"
            " WHERE st.session_id = ? ORDER BY t.name",
            (session_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def add_note_tag(self, note_id: int, tag_id: int) -> None:
        """Tag a note. Idempotent."""
        db = self._conn()
        await db.execute(
            "INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)",
            (note_id, tag_id),
        )
        await db.commit()

    async def remove_note_tag(self, note_id: int, tag_id: int) -> None:
        """Remove a tag from a note."""
        db = self._conn()
        await db.execute(
            "DELETE FROM note_tags WHERE note_id = ? AND tag_id = ?",
            (note_id, tag_id),
        )
        await db.commit()

    async def get_note_tags(self, note_id: int) -> list[dict[str, Any]]:
        """Return tags for a note."""
        cur = await self._conn().execute(
            "SELECT t.id, t.name, t.color FROM tags t"
            " JOIN note_tags nt ON t.id = nt.tag_id"
            " WHERE nt.note_id = ? ORDER BY t.name",
            (note_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_or_create_tag(self, name: str, color: str | None = None) -> int:
        """Return the tag id for *name*, creating it if it doesn't exist."""
        tag = await self.get_tag_by_name(name)
        if tag:
            return tag["id"]  # type: ignore[no-any-return]
        return await self.create_tag(name, color)

    async def update_tag(
        self, tag_id: int, *, name: str | None = None, color: str | None = None
    ) -> bool:
        """Update a tag's name or color. Returns True if found."""
        parts: list[str] = []
        params: list[Any] = []
        if name is not None:
            parts.append("name = ?")
            params.append(name.strip().lower())
        if color is not None:
            parts.append("color = ?")
            params.append(color)
        if not parts:
            return True
        params.append(tag_id)
        db = self._conn()
        cur = await db.execute(
            f"UPDATE tags SET {', '.join(parts)} WHERE id = ?",
            params,  # noqa: S608
        )
        await db.commit()
        return cur.rowcount > 0

    async def delete_tag(self, tag_id: int) -> bool:
        """Delete a tag and all its associations. Returns True if found."""
        db = self._conn()
        await db.execute("DELETE FROM session_tags WHERE tag_id = ?", (tag_id,))
        await db.execute("DELETE FROM note_tags WHERE tag_id = ?", (tag_id,))
        cur = await db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        await db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Avatars (#100)
    # ------------------------------------------------------------------

    async def set_avatar_path(self, user_id: int, avatar_path: str) -> None:
        """Set the avatar_path for a user."""
        db = self._conn()
        await db.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (avatar_path, user_id))
        await db.commit()

    async def get_avatar_path(self, user_id: int) -> str | None:
        """Return the avatar_path for a user, or None."""
        cur = await self._conn().execute("SELECT avatar_path FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return row["avatar_path"] if row else None

    # ------------------------------------------------------------------
    # App settings (#146)
    # ------------------------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        """Return the stored value for *key*, or None if not set."""
        cur = await self._conn().execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return str(row["value"]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        """Upsert a setting (insert or update)."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, value, now),
        )
        await db.commit()

    async def delete_setting(self, key: str) -> bool:
        """Delete a setting. Returns True if a row was removed."""
        db = self._conn()
        cur = await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        await db.commit()
        return cur.rowcount > 0

    async def list_settings(self) -> list[dict[str, str]]:
        """Return all stored settings as a list of dicts."""
        cur = await self._conn().execute(
            "SELECT key, value, updated_at FROM app_settings ORDER BY key"
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Session deletion (#194)
    # ------------------------------------------------------------------

    async def delete_race_session(self, session_id: int) -> list[str]:
        """Delete a race/practice session and all related data.

        Returns a list of file paths (audio WAV, photos) that should be
        deleted from disk by the caller.
        """
        db = self._conn()
        files: list[str] = []

        # Collect audio file paths
        cur = await db.execute(
            "SELECT file_path FROM audio_sessions WHERE race_id = ?", (session_id,)
        )
        for row in await cur.fetchall():
            if row["file_path"]:
                files.append(row["file_path"])

        # Collect photo file paths from notes
        cur = await db.execute(
            "SELECT photo_path FROM session_notes WHERE race_id = ? AND photo_path IS NOT NULL",
            (session_id,),
        )
        for row in await cur.fetchall():
            if row["photo_path"]:
                files.append(row["photo_path"])

        # Cascade delete handles: race_crew, race_results, race_sails,
        # race_videos, session_notes, camera_sessions, session_tags.
        # audio_sessions → transcripts also cascades.
        await db.execute("DELETE FROM audio_sessions WHERE race_id = ?", (session_id,))

        # Delete instrument data in the time range of this session
        cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
        race_row = await cur.fetchone()
        if race_row and race_row["end_utc"]:
            s, e = race_row["start_utc"], race_row["end_utc"]
            for table in (
                "headings",
                "speeds",
                "depths",
                "positions",
                "cogsog",
                "winds",
                "environmental",
            ):
                await db.execute(
                    f"DELETE FROM {table} WHERE ts >= ? AND ts <= ?",  # noqa: S608
                    (s, e),
                )

        await db.execute("DELETE FROM races WHERE id = ?", (session_id,))
        await db.commit()
        logger.info("Session {} deleted (cascade + {} files)", session_id, len(files))
        return files

    # ------------------------------------------------------------------
    # Audio deletion (#196)
    # ------------------------------------------------------------------

    async def delete_audio_session(self, audio_session_id: int) -> str | None:
        """Delete an audio session and its transcript. Returns the file_path for disk cleanup."""
        db = self._conn()
        cur = await db.execute(
            "SELECT file_path FROM audio_sessions WHERE id = ?", (audio_session_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        file_path: str = row["file_path"]
        # transcripts cascade via FK
        await db.execute("DELETE FROM audio_sessions WHERE id = ?", (audio_session_id,))
        await db.commit()
        logger.info("Audio session {} deleted", audio_session_id)
        return file_path

    # ------------------------------------------------------------------
    # Photo cleanup on note deletion (#205)
    # ------------------------------------------------------------------

    async def delete_note_with_file(self, note_id: int) -> tuple[bool, str | None]:
        """Delete a note and return (found, photo_path) for disk cleanup."""
        db = self._conn()
        cur = await db.execute("SELECT photo_path FROM session_notes WHERE id = ?", (note_id,))
        row = await cur.fetchone()
        if row is None:
            return False, None
        photo_path: str | None = row["photo_path"]
        await db.execute("DELETE FROM session_notes WHERE id = ?", (note_id,))
        await db.commit()
        return True, photo_path

    # ------------------------------------------------------------------
    # User deletion (#195)
    # ------------------------------------------------------------------

    async def delete_user(self, user_id: int) -> None:
        """Anonymize and soft-delete a user.

        Replaces email with deleted_<id>@redacted, clears name and avatar,
        deletes auth sessions and invite tokens. Preserves audit trail with
        anonymized user_id references.
        """
        db = self._conn()
        anon_email = f"deleted_{user_id}@redacted"
        await db.execute(
            "UPDATE users SET email = ?, name = NULL, avatar_path = NULL WHERE id = ?",
            (anon_email, user_id),
        )
        await db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        await db.execute(
            "UPDATE invite_tokens SET created_by = NULL WHERE created_by = ?", (user_id,)
        )
        await db.commit()
        logger.info("User {} anonymized and sessions deleted", user_id)

    # ------------------------------------------------------------------
    # Speaker anonymization (#197)
    # ------------------------------------------------------------------

    async def anonymize_speaker(self, transcript_id: int, speaker_label: str) -> bool:
        """Add a speaker to the anonymization map for a transcript.

        Returns True if the transcript was found and updated.
        """
        db = self._conn()
        cur = await db.execute(
            "SELECT speaker_anon_map FROM transcripts WHERE id = ?", (transcript_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return False
        existing: dict[str, str] = json.loads(row["speaker_anon_map"] or "{}")
        existing[speaker_label] = "REDACTED"
        await db.execute(
            "UPDATE transcripts SET speaker_anon_map = ? WHERE id = ?",
            (json.dumps(existing), transcript_id),
        )
        await db.commit()
        logger.info("Speaker {} anonymized in transcript {}", speaker_label, transcript_id)
        return True

    async def get_transcript_with_anon(self, audio_session_id: int) -> dict[str, Any] | None:
        """Get transcript with speaker anonymization map applied to segments."""
        t = await self.get_transcript(audio_session_id)
        if t is None:
            return None
        anon_map: dict[str, str] = json.loads(t.get("speaker_anon_map") or "{}")
        if anon_map and t.get("segments_json"):
            segments = json.loads(t["segments_json"])
            for seg in segments:
                speaker = seg.get("speaker", "")
                if speaker in anon_map:
                    seg["speaker"] = anon_map[speaker]
                    seg["text"] = "[REDACTED]"
            t["segments_json"] = json.dumps(segments)
            # Also redact the plain text
            if t.get("text"):
                lines = t["text"].split("\n")
                redacted_lines = []
                for line in lines:
                    for original, replacement in anon_map.items():
                        if line.startswith(f"{original}:"):
                            line = f"{replacement}: [REDACTED]"
                    redacted_lines.append(line)
                t["text"] = "\n".join(redacted_lines)
        return t

    # ------------------------------------------------------------------
    # Crew consent (#202)
    # ------------------------------------------------------------------

    async def set_crew_consent(self, sailor_name: str, consent_type: str, granted: bool) -> int:
        """Record or update crew consent. Returns the row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        if granted:
            cur = await db.execute(
                "INSERT INTO crew_consents (sailor_name, consent_type, granted, granted_at)"
                " VALUES (?, ?, 1, ?)"
                " ON CONFLICT(sailor_name, consent_type)"
                " DO UPDATE SET granted = 1, granted_at = excluded.granted_at, revoked_at = NULL",
                (sailor_name.strip(), consent_type, now),
            )
        else:
            cur = await db.execute(
                "INSERT INTO crew_consents"
                " (sailor_name, consent_type, granted, granted_at, revoked_at)"
                " VALUES (?, ?, 0, ?, ?)"
                " ON CONFLICT(sailor_name, consent_type)"
                " DO UPDATE SET granted = 0, revoked_at = excluded.revoked_at",
                (sailor_name.strip(), consent_type, now, now),
            )
        await db.commit()
        return cur.lastrowid or 0

    async def get_crew_consents(self, sailor_name: str) -> list[dict[str, Any]]:
        """Return all consent records for a sailor."""
        cur = await self._conn().execute(
            "SELECT id, sailor_name, consent_type, granted, granted_at, revoked_at"
            " FROM crew_consents WHERE sailor_name = ?",
            (sailor_name.strip(),),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_crew_consents(self) -> list[dict[str, Any]]:
        """Return all consent records."""
        cur = await self._conn().execute(
            "SELECT id, sailor_name, consent_type, granted, granted_at, revoked_at"
            " FROM crew_consents ORDER BY sailor_name, consent_type"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def anonymize_sailor(self, sailor_name: str, replacement: str = "Anonymous") -> int:
        """Replace a sailor name across all sessions. Returns number of rows updated."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE race_crew SET sailor = ? WHERE sailor = ?",
            (replacement, sailor_name.strip()),
        )
        count = cur.rowcount or 0
        await db.execute("DELETE FROM recent_sailors WHERE sailor = ?", (sailor_name.strip(),))
        await db.commit()
        logger.info("Sailor {!r} anonymized to {!r} ({} rows)", sailor_name, replacement, count)
        return count

    # ------------------------------------------------------------------
    # Deployment log (#125)
    # ------------------------------------------------------------------

    async def log_deployment(
        self,
        from_sha: str,
        to_sha: str,
        trigger: str = "manual",
        status: str = "success",
        error: str | None = None,
        user_id: int | None = None,
    ) -> int:
        """Record a deployment event. Returns the new row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO deployment_log"
            " (from_sha, to_sha, trigger, status, error, started_at, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (from_sha, to_sha, trigger, status, error, now, user_id),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def list_deployments(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent deployment log entries, newest first."""
        db = self._conn()
        cur = await db.execute(
            "SELECT d.*, u.email AS user_email"
            " FROM deployment_log d"
            " LEFT JOIN users u ON d.user_id = u.id"
            " ORDER BY d.started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def last_deployment(self) -> dict[str, Any] | None:
        """Return the most recent successful deployment, or None."""
        db = self._conn()
        cur = await db.execute(
            "SELECT d.*, u.email AS user_email"
            " FROM deployment_log d"
            " LEFT JOIN users u ON d.user_id = u.id"
            " WHERE d.status = 'success'"
            " ORDER BY d.started_at DESC LIMIT 1",
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Federation — identity & co-op
    # ------------------------------------------------------------------

    async def save_boat_identity(
        self,
        pub_key: str,
        fingerprint: str,
        sail_number: str,
        boat_name: str,
    ) -> None:
        """Store (or update) this boat's identity reference in SQLite."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO boat_identity"
            " (id, pub_key, fingerprint, sail_number, boat_name, created_at)"
            " VALUES (1, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   pub_key = excluded.pub_key,"
            "   fingerprint = excluded.fingerprint,"
            "   sail_number = excluded.sail_number,"
            "   boat_name = excluded.boat_name",
            (pub_key, fingerprint, sail_number, boat_name, now),
        )
        await db.commit()

    async def get_boat_identity(self) -> dict[str, Any] | None:
        """Return this boat's identity row, or None."""
        cur = await self._conn().execute(
            "SELECT pub_key, fingerprint, sail_number, boat_name, created_at"
            " FROM boat_identity WHERE id = 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def save_co_op_membership(
        self,
        co_op_id: str,
        co_op_name: str,
        co_op_pub: str,
        membership_json: str,
        role: str = "member",
        joined_at: str | None = None,
    ) -> int:
        """Store a co-op membership record. Returns the row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        if joined_at is None:
            joined_at = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO co_op_memberships"
            " (co_op_id, co_op_name, co_op_pub, membership_json, role, joined_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(co_op_id) DO UPDATE SET"
            "   co_op_name = excluded.co_op_name,"
            "   co_op_pub = excluded.co_op_pub,"
            "   membership_json = excluded.membership_json,"
            "   role = excluded.role,"
            "   joined_at = excluded.joined_at,"
            "   status = 'active'",
            (co_op_id, co_op_name, co_op_pub, membership_json, role, joined_at),
        )
        await db.commit()
        return cur.lastrowid or 0

    async def list_co_op_memberships(self) -> list[dict[str, Any]]:
        """Return all co-op memberships for this boat."""
        cur = await self._conn().execute(
            "SELECT id, co_op_id, co_op_name, co_op_pub, membership_json,"
            " role, joined_at, status"
            " FROM co_op_memberships ORDER BY joined_at"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_co_op_membership(self, co_op_id: str) -> dict[str, Any] | None:
        """Return a specific co-op membership, or None."""
        cur = await self._conn().execute(
            "SELECT id, co_op_id, co_op_name, co_op_pub, membership_json,"
            " role, joined_at, status"
            " FROM co_op_memberships WHERE co_op_id = ?",
            (co_op_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def share_session(
        self,
        session_id: int,
        co_op_id: str,
        *,
        user_id: int | None = None,
        embargo_until: str | None = None,
        event_name: str | None = None,
    ) -> None:
        """Mark a session as shared with a co-op."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO session_sharing"
            " (session_id, co_op_id, shared_at, shared_by, embargo_until, event_name)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id, co_op_id) DO UPDATE SET"
            "   shared_at = excluded.shared_at,"
            "   shared_by = excluded.shared_by,"
            "   embargo_until = excluded.embargo_until,"
            "   event_name = excluded.event_name",
            (session_id, co_op_id, now, user_id, embargo_until, event_name),
        )
        await db.commit()

    async def unshare_session(self, session_id: int, co_op_id: str) -> bool:
        """Remove a session's co-op sharing. Returns True if a row was deleted."""
        db = self._conn()
        cur = await db.execute(
            "DELETE FROM session_sharing WHERE session_id = ? AND co_op_id = ?",
            (session_id, co_op_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def get_session_sharing(self, session_id: int) -> list[dict[str, Any]]:
        """Return all co-op sharing records for a session."""
        cur = await self._conn().execute(
            "SELECT ss.session_id, ss.co_op_id, ss.shared_at, ss.shared_by,"
            " ss.embargo_until, ss.event_name, cm.co_op_name"
            " FROM session_sharing ss"
            " LEFT JOIN co_op_memberships cm ON ss.co_op_id = cm.co_op_id"
            " WHERE ss.session_id = ?",
            (session_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def is_session_shared(self, session_id: int, co_op_id: str) -> bool:
        """Check if a session is shared with a specific co-op."""
        cur = await self._conn().execute(
            "SELECT 1 FROM session_sharing WHERE session_id = ? AND co_op_id = ?",
            (session_id, co_op_id),
        )
        return await cur.fetchone() is not None

    async def save_co_op_peer(
        self,
        co_op_id: str,
        boat_pub: str,
        fingerprint: str,
        membership_json: str,
        *,
        sail_number: str | None = None,
        boat_name: str | None = None,
        tailscale_ip: str | None = None,
    ) -> None:
        """Store or update a known co-op peer."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO co_op_peers"
            " (co_op_id, boat_pub, fingerprint, sail_number, boat_name,"
            "  tailscale_ip, last_seen, membership_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(co_op_id, fingerprint) DO UPDATE SET"
            "   boat_pub = excluded.boat_pub,"
            "   sail_number = excluded.sail_number,"
            "   boat_name = excluded.boat_name,"
            "   tailscale_ip = excluded.tailscale_ip,"
            "   last_seen = excluded.last_seen,"
            "   membership_json = excluded.membership_json",
            (
                co_op_id,
                boat_pub,
                fingerprint,
                sail_number,
                boat_name,
                tailscale_ip,
                now,
                membership_json,
            ),
        )
        await db.commit()

    async def list_co_op_peers(self, co_op_id: str) -> list[dict[str, Any]]:
        """Return all known peers for a co-op."""
        cur = await self._conn().execute(
            "SELECT id, co_op_id, boat_pub, fingerprint, sail_number,"
            " boat_name, tailscale_ip, last_seen, membership_json"
            " FROM co_op_peers WHERE co_op_id = ? ORDER BY boat_name",
            (co_op_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_co_op_peer(
        self,
        co_op_id: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return a specific peer by co-op and fingerprint."""
        cur = await self._conn().execute(
            "SELECT id, co_op_id, boat_pub, fingerprint, sail_number,"
            " boat_name, tailscale_ip, last_seen, membership_json"
            " FROM co_op_peers WHERE co_op_id = ? AND fingerprint = ?",
            (co_op_id, fingerprint),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Federation — nonce replay protection
    # ------------------------------------------------------------------

    async def check_nonce(self, nonce_hash: str) -> bool:
        """Return True if this nonce has already been used."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        # Prune expired nonces (older than 20 minutes)
        cutoff = _datetime.now(UTC).isoformat()[:-6]  # rough cutoff
        await db.execute("DELETE FROM request_nonces WHERE timestamp < ?", (cutoff,))
        cur = await db.execute("SELECT 1 FROM request_nonces WHERE nonce_hash = ?", (nonce_hash,))
        return await cur.fetchone() is not None

    async def save_nonce(self, nonce_hash: str, boat_fp: str) -> None:
        """Record a nonce as used for replay protection."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO request_nonces (nonce_hash, timestamp, boat_fp)"
            " VALUES (?, ?, ?)",
            (nonce_hash, now, boat_fp),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # Federation — co-op audit logging
    # ------------------------------------------------------------------

    async def log_co_op_audit(
        self,
        co_op_id: str,
        accessor_fp: str,
        action: str,
        *,
        resource: str | None = None,
        ip: str | None = None,
        points_count: int | None = None,
    ) -> int:
        """Write a co-op data access event to the co_op_audit table."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO co_op_audit"
            " (co_op_id, accessor_fp, action, resource, timestamp, ip, points_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (co_op_id, accessor_fp, action, resource, now, ip, points_count),
        )
        await db.commit()
        return cur.lastrowid or 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    """Format a datetime as a UTC ISO 8601 string."""
    return dt.isoformat()


async def get_effective_setting(storage: Storage, key: str, default: str = "") -> str:
    """Return the effective value for a setting: DB → env → default."""
    db_val = await storage.get_setting(key)
    if db_val is not None:
        return db_val
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    return default
