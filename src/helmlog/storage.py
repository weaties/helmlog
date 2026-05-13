"""SQLite persistence layer.

Schema is versioned with simple integer migrations. All timestamps are stored
as UTC ISO 8601 strings. The Storage class is the single source of truth for
all logged data.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import aiosqlite
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from helmlog.audio import AudioSession
    from helmlog.cache import RaceCache
    from helmlog.external import TideReading, WeatherReading
    from helmlog.races import Race
    from helmlog.vakaros import VakarosSession

from helmlog.anchors import AnchorError
from helmlog.nmea2000 import (
    AttitudeRecord,
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PGNRecord,
    PositionRecord,
    RudderRecord,
    SpeedRecord,
    WindRecord,
)
from helmlog.video import VideoSession


class AnchorScopeError(ValueError):
    """Raised when an anchor's referenced entity does not scope to the expected session."""


# Valid values for entity_tags.entity_type. Extended carefully — every
# addition needs a list_entity_ids() branch in storage plus attach UI.
ENTITY_TYPES: frozenset[str] = frozenset({"session", "maneuver", "moment"})


def _hms_from_iso(iso: str | None) -> str:
    """Return the HH:MM:SS portion of an ISO 8601 timestamp.

    Used by anchor-picker labels where the full ISO is visually noisy but
    the time-of-day is the useful information. Falls back to the raw
    input if parsing fails, so malformed rows still surface.
    """
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return iso


def _project_moment_anchor(row: dict[str, Any]) -> dict[str, Any]:
    """Build a serializable ``anchor`` key from the four anchor_* columns."""
    kind = row.get("anchor_kind")
    if kind is None:
        row["anchor"] = None
        return row
    anchor: dict[str, Any] = {"kind": kind}
    if row.get("anchor_entity_id") is not None:
        anchor["entity_id"] = row["anchor_entity_id"]
    if row.get("anchor_t_start") is not None:
        anchor["t_start"] = row["anchor_t_start"]
    if row.get("anchor_t_end") is not None:
        anchor["t_end"] = row["anchor_t_end"]
    row["anchor"] = anchor
    return row


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_utc(s: str | None) -> datetime | None:
    """Parse an ISO-8601 string from the DB into a tz-aware UTC datetime (#532).

    Normalizes at the storage boundary so downstream consumers never receive
    a naive ``datetime`` that would blow up on arithmetic against
    ``datetime.now(UTC)``.

    Behavior:
    - ``None`` / empty string → ``None``
    - Naive ISO datetime → treated as UTC
    - Aware ISO datetime → converted to UTC
    - Date-only string (``"YYYY-MM-DD"``) → midnight UTC of that date
    - Malformed string → ``None`` (logged at WARNING)
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        logger.warning("_parse_utc: could not parse timestamp {!r}", s)
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    """Configuration for the SQLite storage backend."""

    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "data/logger.db"))
    rudder_storage_hz: float = field(
        default_factory=lambda: float(os.environ.get("RUDDER_STORAGE_HZ", "2"))
    )


# ---------------------------------------------------------------------------
# Crew TypedDicts (#305)
# ---------------------------------------------------------------------------


class CrewPosition(TypedDict):
    id: int
    name: str
    display_order: int
    created_at: str


class CrewDefault(TypedDict):
    id: int
    race_id: int | None
    position_id: int
    user_id: int | None
    attributed: int
    body_weight: float | None
    gear_weight: float | None
    created_at: str
    position: str
    display_order: int
    user_name: str | None
    user_email: str | None


class ResolvedCrew(CrewDefault):
    source: str
    supersedes_user_id: int | None
    supersedes_user_name: str | None


class CrewConsent(TypedDict):
    id: int
    user_id: int
    consent_type: str
    granted: int
    granted_at: str
    revoked_at: str | None
    user_name: str | None


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
    "rudder_deg",
    # Derived current vector — computed in _recompute_set_drift from the
    # already-smoothed sog/cog/stw/hdg, then routed through its own EMA
    # so the gauge doesn't twitch on every input fix.
    "set_deg",
    "drift_kts",
)

# ---------------------------------------------------------------------------
# Schema version & migrations
# ---------------------------------------------------------------------------

_CURRENT_VERSION: int = 85

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

        CREATE TABLE IF NOT EXISTS sail_defaults (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            sail_id INTEGER NOT NULL REFERENCES sails(id) ON DELETE CASCADE,
            UNIQUE(sail_id)
        );

        CREATE TABLE IF NOT EXISTS sail_changes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id      INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            ts           TEXT NOT NULL,
            main_id      INTEGER REFERENCES sails(id),
            jib_id       INTEGER REFERENCES sails(id),
            spinnaker_id INTEGER REFERENCES sails(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sail_changes_race_ts ON sail_changes(race_id, ts);
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
    34: """
        ALTER TABLE users ADD COLUMN is_developer INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE invite_tokens ADD COLUMN is_developer INTEGER NOT NULL DEFAULT 0;
    """,
    35: """
        -- Migration 35: Invitation + flexible auth (#268)
        CREATE TABLE IF NOT EXISTS user_credentials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider      TEXT NOT NULL,
            provider_uid  TEXT,
            password_hash TEXT,
            created_at    TEXT NOT NULL,
            UNIQUE(user_id, provider)
        );
        CREATE INDEX IF NOT EXISTS idx_user_credentials_provider
            ON user_credentials(provider, provider_uid);

        CREATE TABLE IF NOT EXISTS invitations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            token       TEXT UNIQUE NOT NULL,
            email       TEXT NOT NULL,
            role        TEXT NOT NULL,
            name        TEXT,
            is_developer INTEGER NOT NULL DEFAULT 0,
            invited_by  INTEGER REFERENCES users(id),
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            accepted_at TEXT,
            revoked_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token      TEXT UNIQUE NOT NULL,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            used_at    TEXT
        );

        ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;

        DROP TABLE IF EXISTS invite_tokens;
    """,
    36: """
        CREATE TABLE IF NOT EXISTS boat_settings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id          INTEGER REFERENCES races(id) ON DELETE CASCADE,
            ts               TEXT NOT NULL,
            parameter        TEXT NOT NULL,
            value            TEXT NOT NULL,
            source           TEXT NOT NULL,
            extraction_run_id INTEGER,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_boat_settings_race_ts
            ON boat_settings(race_id, ts);
        CREATE INDEX IF NOT EXISTS idx_boat_settings_extraction_run
            ON boat_settings(extraction_run_id);
    """,
    37: """
        CREATE TABLE IF NOT EXISTS comment_threads (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id         INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            anchor_timestamp   TEXT,
            mark_reference     TEXT,
            title              TEXT,
            created_by         INTEGER REFERENCES users(id),
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            resolved           INTEGER NOT NULL DEFAULT 0,
            resolved_at        TEXT,
            resolved_by        INTEGER REFERENCES users(id),
            resolution_summary TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_comment_threads_session
            ON comment_threads(session_id);

        CREATE TABLE IF NOT EXISTS comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id  INTEGER NOT NULL REFERENCES comment_threads(id) ON DELETE CASCADE,
            author     INTEGER REFERENCES users(id),
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            edited_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_comments_thread
            ON comments(thread_id);

        CREATE TABLE IF NOT EXISTS comment_read_state (
            user_id   INTEGER REFERENCES users(id) ON DELETE CASCADE,
            thread_id INTEGER NOT NULL REFERENCES comment_threads(id) ON DELETE CASCADE,
            last_read TEXT NOT NULL,
            PRIMARY KEY (user_id, thread_id)
        );
    """,
    38: """
        -- Crew management overhaul (#305)

        -- 1. Add weight column to users
        ALTER TABLE users ADD COLUMN weight_lbs REAL;

        -- 2. Configurable crew positions (replaces hardcoded _POSITIONS)
        CREATE TABLE IF NOT EXISTS crew_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            display_order INTEGER NOT NULL,
            created_at    TEXT NOT NULL
        );

        -- 3. Two-tier crew defaults (boat-level + race-level)
        CREATE TABLE IF NOT EXISTS crew_defaults (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id       INTEGER REFERENCES races(id) ON DELETE CASCADE,
            position_id   INTEGER NOT NULL REFERENCES crew_positions(id),
            user_id       INTEGER REFERENCES users(id),
            attributed    INTEGER NOT NULL DEFAULT 1,
            body_weight   REAL,
            gear_weight   REAL,
            created_at    TEXT NOT NULL,
            UNIQUE(race_id, position_id)
        );
        CREATE INDEX IF NOT EXISTS idx_crew_defaults_race ON crew_defaults(race_id);

        -- 4. Add user_id column to crew_consents
        ALTER TABLE crew_consents ADD COLUMN user_id INTEGER REFERENCES users(id);
    """,
    39: """
        -- Point-of-sail field for sail inventory (#308)
        ALTER TABLE sails ADD COLUMN point_of_sail TEXT NOT NULL DEFAULT 'both';
        UPDATE sails SET point_of_sail = 'upwind' WHERE type = 'jib';
        UPDATE sails SET point_of_sail = 'downwind' WHERE type = 'spinnaker';
    """,
    40: """
        -- Default sail selection — boat-level defaults (#306)
        CREATE TABLE IF NOT EXISTS sail_defaults (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            sail_id INTEGER NOT NULL REFERENCES sails(id) ON DELETE CASCADE,
            UNIQUE(sail_id)
        );
    """,
    41: """
        -- Timestamped sail changes (#311)
        CREATE TABLE IF NOT EXISTS sail_changes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id      INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            ts           TEXT NOT NULL,
            main_id      INTEGER REFERENCES sails(id),
            jib_id       INTEGER REFERENCES sails(id),
            spinnaker_id INTEGER REFERENCES sails(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sail_changes_race_ts ON sail_changes(race_id, ts);
    """,
    42: """
        -- Pluggable analysis framework (#283)
        CREATE TABLE IF NOT EXISTS analysis_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            plugin_name TEXT NOT NULL,
            plugin_version TEXT NOT NULL,
            data_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, plugin_name)
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_cache_session
            ON analysis_cache(session_id);

        CREATE TABLE IF NOT EXISTS analysis_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            scope_id TEXT,
            model_name TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, scope_id)
        );
    """,
    43: """
        -- Threaded comments Phase 2 — notifications (#284)
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            source_thread_id INTEGER REFERENCES comment_threads(id) ON DELETE CASCADE,
            source_comment_id INTEGER REFERENCES comments(id) ON DELETE SET NULL,
            session_id INTEGER REFERENCES races(id) ON DELETE CASCADE,
            actor_id INTEGER REFERENCES users(id),
            message TEXT,
            created_at TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            dismissed INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user
            ON notifications(user_id, read);
        CREATE INDEX IF NOT EXISTS idx_notifications_session
            ON notifications(session_id);

        CREATE TABLE IF NOT EXISTS notification_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            scope TEXT NOT NULL,
            type TEXT NOT NULL,
            channel TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            frequency TEXT NOT NULL DEFAULT 'immediate',
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, scope, type, channel)
        );
    """,
    44: """
        -- Co-op session matching (#281)
        ALTER TABLE races ADD COLUMN shared_name TEXT;
        ALTER TABLE races ADD COLUMN match_group_id TEXT;
        ALTER TABLE races ADD COLUMN match_confirmed INTEGER DEFAULT 0;

        CREATE TABLE IF NOT EXISTS session_match_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_group_id TEXT NOT NULL,
            proposer_fingerprint TEXT NOT NULL,
            local_session_id INTEGER REFERENCES races(id) ON DELETE CASCADE,
            peer_session_id INTEGER,
            centroid_lat REAL,
            centroid_lon REAL,
            start_utc TEXT,
            end_utc TEXT,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_smp_match_group
            ON session_match_proposals(match_group_id);
        CREATE INDEX IF NOT EXISTS idx_smp_status
            ON session_match_proposals(status);

        CREATE TABLE IF NOT EXISTS session_match_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_group_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            UNIQUE(match_group_id, fingerprint)
        );

        CREATE TABLE IF NOT EXISTS session_match_names (
            match_group_id TEXT PRIMARY KEY,
            shared_name TEXT NOT NULL,
            proposed_by TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """,
    45: """
        -- Cache session centroids on races table to avoid N+1 queries (#session-matching)
        ALTER TABLE races ADD COLUMN centroid_lat REAL;
        ALTER TABLE races ADD COLUMN centroid_lon REAL;
    """,
    46: """
        -- Scheduled race starts (#345)
        CREATE TABLE IF NOT EXISTS scheduled_starts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_start_utc TEXT    NOT NULL,
            event               TEXT    NOT NULL,
            session_type        TEXT    NOT NULL DEFAULT 'race',
            created_at          TEXT    NOT NULL
        );
    """,
    47: """
        -- Customizable color schemes (#347)
        ALTER TABLE users ADD COLUMN color_scheme TEXT;
        CREATE TABLE IF NOT EXISTS color_schemes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            bg          TEXT    NOT NULL,
            text_color  TEXT    NOT NULL,
            accent      TEXT    NOT NULL,
            created_by  INTEGER REFERENCES users(id),
            created_at  TEXT    NOT NULL
        );
    """,
    48: """
        -- Tuning extraction from transcripts (#276)
        CREATE TABLE IF NOT EXISTS extraction_runs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id  INTEGER NOT NULL REFERENCES transcripts(id),
            method         TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'created',
            item_count     INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_extraction_runs_transcript
            ON extraction_runs(transcript_id);

        CREATE TABLE IF NOT EXISTS extraction_items (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_run_id INTEGER NOT NULL REFERENCES extraction_runs(id) ON DELETE CASCADE,
            parameter_name    TEXT NOT NULL,
            extracted_value   REAL NOT NULL,
            segment_start     REAL NOT NULL,
            segment_end       REAL NOT NULL,
            segment_text      TEXT NOT NULL,
            confidence        REAL NOT NULL DEFAULT 1.0,
            status            TEXT NOT NULL DEFAULT 'pending',
            reviewed_at       TEXT,
            reviewed_by       INTEGER REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_extraction_items_run
            ON extraction_items(extraction_run_id);
    """,
    49: """
        -- Pluggable visualization framework (#286)
        CREATE TABLE IF NOT EXISTS visualization_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            scope_id TEXT,
            plugin_names TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, scope_id)
        );

        CREATE TABLE IF NOT EXISTS visualization_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            plugin_names TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, session_id)
        );
    """,
    50: """
        -- WLAN profile management (#256)
        CREATE TABLE IF NOT EXISTS wlan_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            ssid        TEXT NOT NULL,
            password    TEXT,
            is_default  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
    """,
    51: """
        -- Phase 2: analysis catalog and version staleness tracking (#285)
        ALTER TABLE analysis_cache ADD COLUMN stale_reason TEXT;

        CREATE TABLE IF NOT EXISTS analysis_catalog (
            plugin_name TEXT NOT NULL,
            co_op_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'proposed',
            proposing_boat TEXT,
            version TEXT,
            author TEXT,
            changelog TEXT,
            proposed_at TEXT NOT NULL,
            resolved_at TEXT,
            reject_reason TEXT,
            data_license_gate_passed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (plugin_name, co_op_id)
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_catalog_co_op
            ON analysis_catalog(co_op_id, state);
    """,
    52: """
        -- Rudder angle table (#419)
        CREATE TABLE IF NOT EXISTS rudder_angles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            source_addr     INTEGER NOT NULL,
            rudder_angle_deg REAL   NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rudder_angles_ts ON rudder_angles(ts);
    """,
    53: """
        -- Device API keys for headless IoT devices (#423)
        CREATE TABLE IF NOT EXISTS devices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            key_hash    TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'crew',
            scope       TEXT,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            last_used   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_devices_key_hash ON devices(key_hash);
    """,
    54: """
        -- ArUco marker tracking (#425)
        CREATE TABLE IF NOT EXISTS aruco_cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            ip TEXT NOT NULL,
            marker_size_mm REAL NOT NULL DEFAULT 50.0,
            capture_interval_s INTEGER NOT NULL DEFAULT 60,
            retain_images INTEGER NOT NULL DEFAULT 0,
            calibration JSON,
            calibration_state TEXT NOT NULL DEFAULT 'uncalibrated',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS aruco_camera_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id INTEGER NOT NULL REFERENCES aruco_cameras(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            settings JSON NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            UNIQUE (camera_id, name)
        );

        CREATE TABLE IF NOT EXISTS aruco_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            camera_id INTEGER NOT NULL REFERENCES aruco_cameras(id) ON DELETE CASCADE,
            marker_id_a INTEGER NOT NULL,
            marker_id_b INTEGER NOT NULL,
            tolerance_mm REAL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS aruco_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            control_id INTEGER NOT NULL REFERENCES aruco_controls(id) ON DELETE CASCADE,
            distance_cm REAL NOT NULL,
            image_path TEXT,
            session_id INTEGER REFERENCES races(id) ON DELETE SET NULL,
            measured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_aruco_measurements_control_time
            ON aruco_measurements(control_id, measured_at DESC);

        CREATE TABLE IF NOT EXISTS aruco_trigger_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT NOT NULL UNIQUE,
            control_id INTEGER NOT NULL REFERENCES aruco_controls(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS aruco_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO aruco_settings VALUES ('tolerance_mm_default', '5.0');
    """,
    55: """
        -- Unified boat controls (#425)
        CREATE TABLE IF NOT EXISTS controls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            label         TEXT NOT NULL,
            unit          TEXT NOT NULL DEFAULT '',
            input_type    TEXT NOT NULL DEFAULT 'number',
            category      TEXT NOT NULL DEFAULT 'sail_controls',
            sort_order    INTEGER NOT NULL DEFAULT 0,
            preset_values TEXT,
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS control_aruco (
            control_id   INTEGER PRIMARY KEY REFERENCES controls(id) ON DELETE CASCADE,
            camera_id    INTEGER NOT NULL REFERENCES aruco_cameras(id) ON DELETE CASCADE,
            marker_id_a  INTEGER NOT NULL,
            marker_id_b  INTEGER NOT NULL,
            tolerance_mm REAL
        );

        CREATE TABLE IF NOT EXISTS control_trigger_words (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            control_id INTEGER NOT NULL REFERENCES controls(id) ON DELETE CASCADE,
            phrase     TEXT NOT NULL UNIQUE
        );
    """,
    56: """
        -- Configurable control categories (#425)
        CREATE TABLE IF NOT EXISTS control_categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            label       TEXT NOT NULL,
            sort_order  INTEGER NOT NULL DEFAULT 0
        );
    """,
    57: """
        -- Diarized transcript crew association + voice profiles (#443)
        ALTER TABLE transcripts ADD COLUMN speaker_map TEXT;

        CREATE TABLE IF NOT EXISTS crew_voice_profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            embedding     BLOB    NOT NULL,
            segment_count INTEGER NOT NULL DEFAULT 0,
            session_count INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL,
            UNIQUE(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_crew_voice_profiles_user ON crew_voice_profiles(user_id);
    """,
    58: """
        -- Session rename + human-readable URL slugs (#449).
        -- slug is NULL until the post-DDL backfill in _migrate_v58_slugs runs.
        ALTER TABLE races ADD COLUMN slug TEXT;
        ALTER TABLE races ADD COLUMN renamed_at TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_races_slug ON races(slug);

        CREATE TABLE IF NOT EXISTS race_slug_history (
            slug       TEXT PRIMARY KEY,
            race_id    INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            retired_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_race_slug_history_race ON race_slug_history(race_id);
    """,
    59: """
        -- Vakaros VKX ingest (#458).  Sessions sourced from a Vakaros Atlas
        -- via the watched-folder ingest path.  Kept in parallel with races(),
        -- linked via matched_race_id when session matching finds an overlap.
        CREATE TABLE IF NOT EXISTS vakaros_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_hash     TEXT    NOT NULL UNIQUE,
            source_file     TEXT    NOT NULL,
            start_utc       TEXT    NOT NULL,
            end_utc         TEXT    NOT NULL,
            ingested_at     TEXT    NOT NULL,
            matched_race_id INTEGER REFERENCES races(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_sessions_start ON vakaros_sessions(start_utc);
        CREATE INDEX IF NOT EXISTS idx_vakaros_sessions_match ON vakaros_sessions(matched_race_id);

        CREATE TABLE IF NOT EXISTS vakaros_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts            TEXT    NOT NULL,
            latitude_deg  REAL    NOT NULL,
            longitude_deg REAL    NOT NULL,
            sog_mps       REAL    NOT NULL,
            cog_deg       REAL    NOT NULL,
            altitude_m    REAL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_positions_session
            ON vakaros_positions(session_id, ts);

        CREATE TABLE IF NOT EXISTS vakaros_line_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts            TEXT    NOT NULL,
            line_type     TEXT    NOT NULL,  -- 'pin' or 'boat'
            latitude_deg  REAL    NOT NULL,
            longitude_deg REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_line_session ON vakaros_line_positions(session_id);

        CREATE TABLE IF NOT EXISTS vakaros_race_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts             TEXT    NOT NULL,
            event_type     TEXT    NOT NULL,  -- 'reset'|'start'|'sync'|'race_start'|'race_end'
            timer_value_s  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_events_session
            ON vakaros_race_events(session_id, ts);

        CREATE TABLE IF NOT EXISTS vakaros_winds (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER NOT NULL REFERENCES vakaros_sessions(id) ON DELETE CASCADE,
            ts             TEXT    NOT NULL,
            direction_deg  REAL    NOT NULL,
            speed_mps      REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vakaros_winds_session ON vakaros_winds(session_id, ts);
    """,
    60: """
        ALTER TABLE races ADD COLUMN vakaros_session_id INTEGER
            REFERENCES vakaros_sessions(id) ON DELETE SET NULL;
        CREATE INDEX IF NOT EXISTS idx_races_vakaros_session
            ON races(vakaros_session_id);
    """,
    61: """
        -- Imported race results from external providers (#459): Clubspot, STYC, ...
        -- Additive only. No existing column is dropped, renamed, or retyped.

        CREATE TABLE IF NOT EXISTS regattas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT    NOT NULL,
            source_id       TEXT    NOT NULL,
            name            TEXT    NOT NULL,
            start_date      TEXT,
            end_date        TEXT,
            url             TEXT,
            default_class   TEXT,
            last_fetched_at TEXT,
            created_at      TEXT    NOT NULL,
            UNIQUE(source, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_regattas_source ON regattas(source);

        CREATE TABLE IF NOT EXISTS series_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            regatta_id      INTEGER NOT NULL REFERENCES regattas(id) ON DELETE CASCADE,
            boat_id         INTEGER NOT NULL REFERENCES boats(id) ON DELETE CASCADE,
            class           TEXT,
            total_points    REAL,
            net_points      REAL,
            place_in_class  INTEGER,
            place_overall   INTEGER,
            updated_at      TEXT    NOT NULL,
            UNIQUE(regatta_id, boat_id, class)
        );
        CREATE INDEX IF NOT EXISTS idx_series_results_regatta ON series_results(regatta_id);
        CREATE INDEX IF NOT EXISTS idx_series_results_boat    ON series_results(boat_id);

        ALTER TABLE boats ADD COLUMN skipper TEXT;
        ALTER TABLE boats ADD COLUMN boat_type TEXT;
        ALTER TABLE boats ADD COLUMN phrf_rating INTEGER;
        ALTER TABLE boats ADD COLUMN yacht_club TEXT;
        ALTER TABLE boats ADD COLUMN owner_email TEXT;

        ALTER TABLE race_results ADD COLUMN start_time TEXT;
        ALTER TABLE race_results ADD COLUMN elapsed_seconds INTEGER;
        ALTER TABLE race_results ADD COLUMN corrected_seconds INTEGER;
        ALTER TABLE race_results ADD COLUMN points REAL;
        ALTER TABLE race_results ADD COLUMN points_throwout INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE race_results ADD COLUMN status_code TEXT;
        ALTER TABLE race_results ADD COLUMN division TEXT;
        ALTER TABLE race_results ADD COLUMN fleet TEXT;

        ALTER TABLE races ADD COLUMN regatta_id INTEGER
            REFERENCES regattas(id) ON DELETE SET NULL;
        ALTER TABLE races ADD COLUMN local_session_id INTEGER
            REFERENCES races(id) ON DELETE SET NULL;
        ALTER TABLE races ADD COLUMN source TEXT;
        ALTER TABLE races ADD COLUMN source_id TEXT;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_races_source ON races(source, source_id)
            WHERE source IS NOT NULL AND source_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_races_regatta ON races(regatta_id);
        CREATE INDEX IF NOT EXISTS idx_races_local_session ON races(local_session_id);
    """,
    62: """
        -- Multi-channel audio recording and isolation (#462)
        ALTER TABLE audio_sessions ADD COLUMN channel_map TEXT;
    """,
    63: """
        -- Multi-channel audio: relational channel_map + per-segment channel
        -- tagging foundation (#462 pt.1 / #493).
        CREATE TABLE IF NOT EXISTS channel_map (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id        INTEGER NOT NULL,
            product_id       INTEGER NOT NULL,
            serial           TEXT    NOT NULL DEFAULT '',
            usb_port_path    TEXT    NOT NULL,
            channel_index    INTEGER NOT NULL,
            position_name    TEXT    NOT NULL,
            audio_session_id INTEGER REFERENCES audio_sessions(id) ON DELETE CASCADE,
            created_utc      TEXT    NOT NULL,
            created_by       INTEGER REFERENCES users(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_map_unique
            ON channel_map(
                vendor_id, product_id, serial, usb_port_path,
                channel_index, IFNULL(audio_session_id, -1)
            );
        CREATE INDEX IF NOT EXISTS idx_channel_map_session
            ON channel_map(audio_session_id);

        CREATE TABLE IF NOT EXISTS transcript_segments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            segment_index INTEGER NOT NULL,
            start_time    REAL    NOT NULL,
            end_time      REAL    NOT NULL,
            text          TEXT    NOT NULL,
            speaker       TEXT,
            channel_index INTEGER,
            position_name TEXT,
            override_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_segments_transcript
            ON transcript_segments(transcript_id);
        CREATE INDEX IF NOT EXISTS idx_transcript_segments_channel
            ON transcript_segments(transcript_id, channel_index);
    """,
    64: """
        -- Multi-channel audio: persist active USB device identity onto each
        -- audio_sessions row so playback knows which channel_map to load
        -- (#462 pt.2 / #494). The tuple matches the channel_map composite
        -- key from v63.
        ALTER TABLE audio_sessions ADD COLUMN vendor_id INTEGER;
        ALTER TABLE audio_sessions ADD COLUMN product_id INTEGER;
        ALTER TABLE audio_sessions ADD COLUMN serial TEXT;
        ALTER TABLE audio_sessions ADD COLUMN usb_port_path TEXT;
    """,
    65: """
        -- Sibling-card capture stopgap (#509 / #462 follow-up): when two or
        -- more mono USB receivers are used in parallel (e.g. the Jieli
        -- 4-mic wireless sets that mix all transmitters to mono before USB),
        -- each card produces its own mono WAV and its own audio_sessions
        -- row. All siblings from one start/stop cycle share a
        -- ``capture_group_id`` UUID and each carries its ordinal within the
        -- group. NULL capture_group_id = legacy single-device session.
        ALTER TABLE audio_sessions ADD COLUMN capture_group_id TEXT;
        ALTER TABLE audio_sessions ADD COLUMN capture_ordinal INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_audio_sessions_capture_group
            ON audio_sessions(capture_group_id);
    """,
    67: """
        -- Enriched maneuver payload cache (#530). The per-session GET
        -- /api/sessions/{id}/maneuvers endpoint re-runs the full enrichment
        -- pipeline (load instrument series, rank, build tracks, attach video
        -- sync) on every request. This table stores the JSON-ready payload
        -- and is invalidated when maneuvers are re-detected or the linked
        -- race video changes. ``code_version`` lets us force-refresh all
        -- entries when the enrichment logic changes — bump
        -- ENRICH_CACHE_VERSION in analysis/maneuvers.py.
        CREATE TABLE IF NOT EXISTS maneuver_cache (
            session_id   INTEGER PRIMARY KEY REFERENCES races(id) ON DELETE CASCADE,
            payload      TEXT    NOT NULL,
            code_version INTEGER NOT NULL,
            computed_at  TEXT    NOT NULL
        );
    """,
    66: """
        -- Per-segment polar grading cache for race replay (#469).
        -- One row per (session_id, polar_source, segment_index). Invalidated
        -- by bumping the polar_baseline_version app_setting in
        -- build_polar_baseline().
        CREATE TABLE IF NOT EXISTS polar_segment_grades (
            session_id        INTEGER NOT NULL,
            polar_source      TEXT    NOT NULL DEFAULT 'own',
            segment_index     INTEGER NOT NULL,
            t_start           TEXT    NOT NULL,
            t_end             TEXT    NOT NULL,
            lat               REAL,
            lon               REAL,
            tws_kts           REAL,
            twa_deg           REAL,
            bsp_kts           REAL,
            target_bsp_kts    REAL,
            pct_target        REAL,
            delta_kts         REAL,
            grade             TEXT NOT NULL,
            baseline_version  INTEGER NOT NULL,
            computed_at       TEXT NOT NULL,
            PRIMARY KEY (session_id, polar_source, segment_index)
        );
        CREATE INDEX IF NOT EXISTS idx_polar_segment_grades_session
            ON polar_segment_grades(session_id, polar_source);
    """,
    68: """
        -- #532: Backfill imported-results races whose start_utc was written
        -- as a bare date by the Clubspot/STYC importer. Rewrite them to a
        -- real ISO-8601 UTC timestamp at midnight so _parse_utc returns a
        -- tz-aware datetime. The column is NOT NULL so we cannot use NULL,
        -- and get_current_race filters these placeholders via LIKE '%T%'.
        UPDATE races
           SET start_utc = start_utc || 'T00:00:00+00:00'
         WHERE length(start_utc) = 10;
        UPDATE races SET end_utc = NULL WHERE end_utc = '';
    """,
    69: """
        -- #532: Belt-and-suspenders re-run of the v68 backfill. On a live Pi
        -- the schema_version was observed to advance while the UPDATE payload
        -- never touched any rows (a partial apply of an earlier buggy text
        -- that sqlite rejected mid-migration). Re-running the same UPDATEs
        -- here guarantees any Pi in the same state self-heals on next deploy.
        UPDATE races
           SET start_utc = start_utc || 'T00:00:00+00:00'
         WHERE length(start_utc) = 10;
        UPDATE races SET end_utc = NULL WHERE end_utc = '';
    """,
    70: """
        -- #477 / #588 slice 1: Moments foundation (bookmarks + anchor schema).
        -- Shared anchor columns across bookmarks and threads lock in the
        -- primitive that slice 2 (#478) and slice 3 (#587) build on.

        CREATE TABLE IF NOT EXISTS bookmarks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id        INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            created_by        INTEGER REFERENCES users(id),
            name              TEXT NOT NULL,
            note              TEXT,
            anchor_kind       TEXT NOT NULL CHECK (anchor_kind = 'timestamp'),
            anchor_entity_id  INTEGER,
            anchor_t_start    TEXT NOT NULL,
            anchor_t_end      TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bookmarks_session
            ON bookmarks(session_id, anchor_t_start);
        CREATE INDEX IF NOT EXISTS idx_bookmarks_created_by
            ON bookmarks(created_by);

        ALTER TABLE tags ADD COLUMN usage_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE tags ADD COLUMN last_used_at TEXT;

        -- Polymorphic tag join. entity_type is validated at the app layer;
        -- session_tags and note_tags remain for this slice and fold in
        -- during slice 3 (#587).
        CREATE TABLE IF NOT EXISTS entity_tags (
            tag_id       INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            entity_type  TEXT NOT NULL,
            entity_id    INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            created_by   INTEGER REFERENCES users(id),
            PRIMARY KEY (tag_id, entity_type, entity_id)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_tags_entity
            ON entity_tags(entity_type, entity_id);

        ALTER TABLE comment_threads ADD COLUMN anchor_kind      TEXT;
        ALTER TABLE comment_threads ADD COLUMN anchor_entity_id INTEGER;
        ALTER TABLE comment_threads ADD COLUMN anchor_t_start   TEXT;
        ALTER TABLE comment_threads ADD COLUMN anchor_t_end     TEXT;
        UPDATE comment_threads
           SET anchor_kind    = 'timestamp',
               anchor_t_start = anchor_timestamp
         WHERE anchor_timestamp IS NOT NULL
           AND anchor_kind IS NULL;
    """,
    71: """
        -- #478 / #588 slice 2: clean cutover of legacy thread anchor columns.
        -- Rows with a mark_reference carry their label into the title so the
        -- human-readable info isn't lost, then the legacy columns are dropped.

        UPDATE comment_threads
           SET title = TRIM('[' || REPLACE(mark_reference, '_', ' ') || '] '
                          || COALESCE(title, ''))
         WHERE mark_reference IS NOT NULL;

        ALTER TABLE comment_threads DROP COLUMN anchor_timestamp;
        ALTER TABLE comment_threads DROP COLUMN mark_reference;
    """,
    72: """
        -- #587 / #588 slice 3. Folds the legacy tag join tables into the
        -- polymorphic entity_tags introduced in slice 1. session_tags and
        -- note_tags rows get copied across, tags.usage_count is backfilled
        -- from the combined counts, and the old tables are dropped.

        INSERT OR IGNORE INTO entity_tags (tag_id, entity_type, entity_id, created_at)
        SELECT tag_id, 'session', session_id, '2024-01-01T00:00:00+00:00'
          FROM session_tags;

        INSERT OR IGNORE INTO entity_tags (tag_id, entity_type, entity_id, created_at)
        SELECT tag_id, 'session_note', note_id, '2024-01-01T00:00:00+00:00'
          FROM note_tags;

        UPDATE tags
           SET usage_count = (
                 SELECT COUNT(*) FROM entity_tags WHERE entity_tags.tag_id = tags.id
               );

        DROP TABLE session_tags;
        DROP TABLE note_tags;
    """,
    73: """
        -- #594: web response cache. Content-addressed by (key_family, race_id)
        -- with a data_hash derived from the race row. The storage layer
        -- notifies the cache on any race mutation so entries invalidate
        -- in-transaction.
        -- No FK to races(id): the cache is best-effort, and the storage
        -- invalidation hook drops entries in-transaction on race mutations.
        -- Orphans from external DELETEs are harmless and swept via cap eviction.
        CREATE TABLE IF NOT EXISTS web_cache (
            key_family  TEXT NOT NULL,
            race_id     INTEGER NOT NULL,
            data_hash   TEXT NOT NULL,
            blob        TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            PRIMARY KEY (key_family, race_id)
        );
        CREATE INDEX IF NOT EXISTS idx_web_cache_race ON web_cache(race_id);
        CREATE INDEX IF NOT EXISTS idx_web_cache_created ON web_cache(created_utc);
    """,
    74: """
        -- #610: TTL support for non-race-keyed T2 entries (external API
        -- fetches). Existing race-keyed rows leave expires_utc NULL and
        -- rely on the race-mutation invalidation hook as before. Global
        -- entries use race_id=0 as a sentinel (no race ever has id 0).
        ALTER TABLE web_cache ADD COLUMN expires_utc TEXT;
        CREATE INDEX IF NOT EXISTS idx_web_cache_expires ON web_cache(expires_utc);
    """,
    75: """
        -- #622: attitude (heel/trim) from PGN 127257 / SK navigation.attitude.
        -- heel_deg = roll (positive = starboard down),
        -- trim_deg = pitch (positive = bow up). Yaw is already covered by
        -- headings (navigation.headingTrue) and not stored here.
        CREATE TABLE IF NOT EXISTS attitudes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            source_addr INTEGER NOT NULL,
            heel_deg    REAL    NOT NULL,
            trim_deg    REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attitudes_ts ON attitudes(ts);
    """,
    76: """
        -- #613: store the head-to-wind timestamp per maneuver. Tacks use
        -- the signed-TWA zero crossing. Gybes use the ±180° crossing.
        -- Populated by enrichment (analysis/maneuvers.py), not by the
        -- detector. Null for rounding and other types.
        ALTER TABLE maneuvers ADD COLUMN head_to_wind_ts TEXT;
    """,
    77: """
        -- #648: per-segment speaker override. Label-wide assignments via
        -- speaker_map re-label every segment that shares the label. This
        -- column lets a single segment point at a different crew member
        -- without mutating the label or touching other segments. Null
        -- means "use the label-wide speaker_map lookup".
        ALTER TABLE transcript_segments ADD COLUMN override_user_id INTEGER
            REFERENCES users(id) ON DELETE SET NULL;
    """,
    78: """
        -- #652: seed starter tag vocabulary for human-judgment racing
        -- moments (close crossings, protests, places gained/lost, etc.).
        -- Conditions like light/heavy air are NOT seeded — they are
        -- queries against wind data, not tags.
        --
        -- Color convention, load-bearing for UX:
        --   green  (#16a34a) — us-good outcome
        --   red    (#b91c1c) — us-bad outcome
        --   amber  (#eab308 / #f59e0b) — severity (close-crossing / near-miss)
        --   red    (#dc2626) — severe incident (collision, hit-mark, ocs)
        --   purple (#7c3aed) — rules bucket
        --   grey   (#6b7280) — gear/crew incident
        --
        -- Directional pairs (rolled-them / got-rolled, etc.) are kept
        -- SEPARATE on purpose — collapsing them would destroy the
        -- "every moment we got rolled" query. Do not merge in admin UI.
        INSERT OR IGNORE INTO tags (name, color, created_at) VALUES
            ('close-crossing',  '#eab308', datetime('now')),
            ('collision',       '#dc2626', datetime('now')),
            ('near-miss',       '#f59e0b', datetime('now')),
            ('rolled-them',     '#16a34a', datetime('now')),
            ('got-rolled',      '#b91c1c', datetime('now')),
            ('lee-bowed-them',  '#16a34a', datetime('now')),
            ('got-lee-bowed',   '#b91c1c', datetime('now')),
            ('pinned-them',     '#16a34a', datetime('now')),
            ('got-pinned',      '#b91c1c', datetime('now')),
            ('places-gained',   '#16a34a', datetime('now')),
            ('places-lost',     '#b91c1c', datetime('now')),
            ('passed-boat',     '#16a34a', datetime('now')),
            ('got-passed',      '#b91c1c', datetime('now')),
            ('we-protested',    '#7c3aed', datetime('now')),
            ('got-protested',   '#7c3aed', datetime('now')),
            ('took-penalty',    '#7c3aed', datetime('now')),
            ('hit-mark',        '#dc2626', datetime('now')),
            ('ocs',             '#dc2626', datetime('now')),
            ('gear-failure',    '#6b7280', datetime('now')),
            ('crew-incident',   '#6b7280', datetime('now')),
            ('grounded',        '#6b7280', datetime('now'));
    """,
    79: """
        -- #650: provenance for auto-tagged moments. Future auto-taggers
        -- (transcript keyword match, detector heuristics) write entity_tag
        -- rows with source='auto:transcript' / 'auto:detector' and
        -- confirmed_at=NULL. A human reviewer then accepts the tag, which
        -- stamps confirmed_at / confirmed_by. Source is preserved forever
        -- so we can always distinguish "a person put this here" from
        -- "a machine guessed and a person accepted".
        --
        -- Default list queries hide unconfirmed auto-tags so they don't
        -- pollute aggregation ("all collisions this season"). Explicit
        -- review flows pass include_unconfirmed=True.
        ALTER TABLE entity_tags ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
        ALTER TABLE entity_tags ADD COLUMN confirmed_at TEXT;
        ALTER TABLE entity_tags ADD COLUMN confirmed_by INTEGER REFERENCES users(id);
        CREATE INDEX IF NOT EXISTS idx_entity_tags_source ON entity_tags(source);
    """,
    80: """
        -- #662: moments unification. Collapses bookmarks, comment_threads,
        -- and session_notes into one primitive -- a moment -- that is a
        -- time range on a session with optional subject, tags, comments,
        -- and attachments.
        --
        -- This migration creates the new tables. The DATA copy and DROP
        -- of old tables happens in _migrate_v80_moments so the old
        -- tables remain readable while the data is being migrated.
        --
        -- anchor_kind: 'session' | 'timestamp' | 'maneuver' | 'transcript_segment'.
        -- For session anchors, t_start is NULL (moment is session-scope).
        -- For maneuver / transcript_segment anchors, t_start / t_end are
        -- derived from the referenced entity at read time -- annotations
        -- follow the event across detector re-runs. If the underlying
        -- entity disappears, storage downgrades the anchor to 'timestamp'
        -- at the last-known t_start so nothing is ever lost.
        --
        -- source / confirmed_at / confirmed_by mirror the entity_tags
        -- provenance so auto-tagger-created moments can be reviewed
        -- before counting toward aggregation queries.
        CREATE TABLE IF NOT EXISTS moments (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id         INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            subject            TEXT,
            counterparty       TEXT,
            anchor_kind        TEXT NOT NULL,
            anchor_entity_id   INTEGER,
            anchor_t_start     TEXT,
            anchor_t_end       TEXT,
            resolved           INTEGER NOT NULL DEFAULT 0,
            resolved_at        TEXT,
            resolved_by        INTEGER REFERENCES users(id),
            resolution_summary TEXT,
            source             TEXT NOT NULL DEFAULT 'manual',
            confirmed_at       TEXT,
            confirmed_by       INTEGER REFERENCES users(id),
            created_by         INTEGER REFERENCES users(id),
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_moments_session_ts
            ON moments(session_id, anchor_t_start);
        CREATE INDEX IF NOT EXISTS idx_moments_session_kind
            ON moments(session_id, anchor_kind);
        CREATE INDEX IF NOT EXISTS idx_moments_counterparty
            ON moments(counterparty) WHERE counterparty IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_moments_source
            ON moments(source);
        CREATE INDEX IF NOT EXISTS idx_moments_open
            ON moments(session_id) WHERE resolved = 0;

        -- Attachments table: 1:many with moments. 'kind' is open-ended so
        -- future extractors (audio clips, LLM-generated transcript
        -- summaries, etc.) plug in without a schema change. 'path' is
        -- for filesystem artifacts (photos, extracted audio). 'body' is
        -- for inline text (transcript snippets, LLM output).
        CREATE TABLE IF NOT EXISTS moment_attachments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            moment_id  INTEGER NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL,
            path       TEXT,
            body       TEXT,
            created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_moment_attachments_moment
            ON moment_attachments(moment_id);

        -- Rescue note_type='settings' from the collapsing session_notes
        -- table. These rows are config snapshots (JSON objects), not
        -- human notes, and belong in their own table. list_settings_keys
        -- now scans here.
        CREATE TABLE IF NOT EXISTS session_settings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       INTEGER REFERENCES races(id) ON DELETE CASCADE,
            audio_session_id INTEGER REFERENCES audio_sessions(id) ON DELETE CASCADE,
            ts               TEXT NOT NULL,
            body             TEXT NOT NULL,
            created_by       INTEGER REFERENCES users(id),
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_settings_session
            ON session_settings(session_id);

        -- Staging tables for the rename. _migrate_v80_moments copies data
        -- from the old comments / comment_read_state into these (with
        -- thread_id remapped to moment_id), then drops the old tables
        -- and renames these. Created here so the DDL is visible in the
        -- migration string rather than hidden in Python.
        CREATE TABLE IF NOT EXISTS comments_new (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            moment_id  INTEGER NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
            author     INTEGER REFERENCES users(id),
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            edited_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_comments_new_moment
            ON comments_new(moment_id);

        CREATE TABLE IF NOT EXISTS comment_read_state_new (
            user_id   INTEGER REFERENCES users(id) ON DELETE CASCADE,
            moment_id INTEGER NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
            last_read TEXT NOT NULL,
            PRIMARY KEY (user_id, moment_id)
        );

        -- Notifications FK'd comment_threads via source_thread_id. Rename the
        -- column to source_moment_id and repoint the FK at moments. The data
        -- migration copies rows across mapping thread_id -> moment_id.
        CREATE TABLE IF NOT EXISTS notifications_new (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type              TEXT    NOT NULL,
            source_moment_id  INTEGER REFERENCES moments(id) ON DELETE CASCADE,
            source_comment_id INTEGER,
            session_id        INTEGER REFERENCES races(id) ON DELETE CASCADE,
            actor_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
            message           TEXT,
            created_at        TEXT NOT NULL,
            read              INTEGER NOT NULL DEFAULT 0,
            dismissed         INTEGER NOT NULL DEFAULT 0
        );
    """,
    81: """
        -- #670: hot-path index gaps.
        -- audio_sessions is joined to races on race_id from the race list
        -- page (EXISTS subquery per row), race detail, and session cleanup.
        -- Previously a full table scan per lookup.
        CREATE INDEX IF NOT EXISTS idx_audio_sessions_race_id
            ON audio_sessions(race_id);
        -- latest_instruments() and per-session wind aggregates filter winds
        -- by reference (IN (0,4) for true wind, =2 for apparent) before an
        -- ORDER BY ts / ts-range filter. The existing idx_winds_ts has no
        -- reference column, so SQLite scanned rows in ts order checking
        -- reference for each. This composite lets it seek straight to the
        -- matching reference partition and take the newest row in O(log n).
        CREATE INDEX IF NOT EXISTS idx_winds_reference_ts
            ON winds(reference, ts);
    """,
    82: """
        -- #644: race-start management. Two tables.
        -- race_start_state is a singleton row (id=1) holding the live FSM
        -- state. Only one start sequence is active at a time per boat.
        -- start_line_pings is an append-only log of boat-end and pin-end
        -- pings, preserving history for re-pings and audit.
        CREATE TABLE IF NOT EXISTS race_start_state (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            phase             TEXT NOT NULL,
            kind              TEXT,
            t0_utc            TEXT,
            sync_offset_s     REAL NOT NULL DEFAULT 0,
            last_sync_at_utc  TEXT,
            started_at_utc    TEXT,
            classes_json      TEXT NOT NULL DEFAULT '[]',
            updated_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS start_line_pings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id       INTEGER REFERENCES races(id) ON DELETE CASCADE,
            end_kind      TEXT NOT NULL CHECK (end_kind IN ('boat', 'pin')),
            latitude_deg  REAL NOT NULL,
            longitude_deg REAL NOT NULL,
            captured_at   TEXT NOT NULL,
            captured_by   INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_start_line_pings_race
            ON start_line_pings(race_id, end_kind, captured_at);
    """,
    83: """
        -- Dedupe race_videos and enforce one link per (race_id, video_id).
        -- Double-submits and re-paste-to-edit attempts created duplicate
        -- rows (no UNIQUE constraint, no idempotency in add_race_video).
        -- Per-group, keep the row with the latest created_at — that's the
        -- one most likely to carry the user's most recent sync calibration.
        DELETE FROM race_videos
        WHERE id NOT IN (
            SELECT id FROM race_videos rv
            WHERE rv.created_at = (
                SELECT MAX(created_at) FROM race_videos
                WHERE race_id = rv.race_id AND video_id = rv.video_id
            )
            AND rv.id = (
                SELECT MAX(id) FROM race_videos
                WHERE race_id = rv.race_id AND video_id = rv.video_id
                  AND created_at = rv.created_at
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_race_videos_unique_race_video
            ON race_videos(race_id, video_id);
    """,
    84: """
        -- Add data_hash to the web_cache PK so global (race_id=0) entries
        -- with different content hashes can coexist. Previously every
        -- distinct cross-session overlay URL evicted the previous one
        -- because the single (key_family, race_id) slot only held one
        -- row per family. Cache content is fully regenerable, so we
        -- drop the table outright rather than rewrite columns in place.
        DROP TABLE IF EXISTS web_cache;
        CREATE TABLE web_cache (
            key_family  TEXT NOT NULL,
            race_id     INTEGER NOT NULL,
            data_hash   TEXT NOT NULL,
            blob        TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            expires_utc TEXT,
            PRIMARY KEY (key_family, race_id, data_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_web_cache_race ON web_cache(race_id);
        CREATE INDEX IF NOT EXISTS idx_web_cache_created ON web_cache(created_utc);
        CREATE INDEX IF NOT EXISTS idx_web_cache_expires ON web_cache(expires_utc);
    """,
    85: """
        -- #697 LLM-powered transcript Q&A and verbal callback detection.
        -- Diarized transcripts are routed to a hosted LLM (a new external
        -- data flow). The consent flag below gates everything until an
        -- admin acknowledges the policy. Per-race tables so deleting a
        -- race wipes its LLM artefacts in the same transaction.

        CREATE TABLE IF NOT EXISTS llm_qa (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id             INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
            question            TEXT    NOT NULL,
            answer              TEXT,
            citations_json      TEXT    NOT NULL DEFAULT '[]',
            model               TEXT    NOT NULL,
            input_tokens        INTEGER NOT NULL DEFAULT 0,
            output_tokens       INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
            cache_create_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd            REAL    NOT NULL DEFAULT 0,
            status              TEXT    NOT NULL DEFAULT 'complete',
            error_msg           TEXT,
            created_at          TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_llm_qa_race_created
            ON llm_qa(race_id, created_at);

        CREATE TABLE IF NOT EXISTS llm_callbacks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id         INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
            speaker_label   TEXT,
            anchor_ts       TEXT NOT NULL,
            source_excerpt  TEXT NOT NULL,
            rationale       TEXT,
            moment_id       INTEGER REFERENCES moments(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_llm_callbacks_race_ts
            ON llm_callbacks(race_id, anchor_ts);
        CREATE INDEX IF NOT EXISTS idx_llm_callbacks_speaker
            ON llm_callbacks(race_id, speaker_label);

        -- One row per race tracking the detection-job lifecycle (state
        -- diagram §2 of the spec). cost_usd is the LLM spend for the job
        -- itself. Per-query Q&A spend lives on llm_qa.
        CREATE TABLE IF NOT EXISTS llm_callback_jobs (
            race_id     INTEGER PRIMARY KEY REFERENCES races(id) ON DELETE CASCADE,
            status      TEXT    NOT NULL DEFAULT 'NotRun',
            error_msg   TEXT,
            cost_usd    REAL    NOT NULL DEFAULT 0,
            started_at  TEXT,
            finished_at TEXT
        );

        -- Per-race cap overrides. Absent row = use config defaults.
        CREATE TABLE IF NOT EXISTS llm_race_caps (
            race_id        INTEGER PRIMARY KEY REFERENCES races(id) ON DELETE CASCADE,
            soft_warn_usd  REAL,
            hard_cap_usd   REAL,
            updated_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_at     TEXT NOT NULL
        );

        -- Boat-wide consent acknowledgement (admin one-time, per spec).
        -- Singleton row with id fixed at 1. INSERT OR IGNORE guarantees
        -- the row exists so updates are unconditional.
        CREATE TABLE IF NOT EXISTS llm_consent (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            acknowledged INTEGER NOT NULL DEFAULT 0,
            by_user      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            at           TEXT
        );
        INSERT OR IGNORE INTO llm_consent (id, acknowledged) VALUES (1, 0);
    """,
}

# Retention window for retired slugs (#449). Requests for a retired slug 301
# to the current slug while the retirement is within this window; beyond it
# they 404.
RACE_SLUG_RETENTION_DAYS: int = 30


def _split_migration_sql(sql: str) -> list[str]:
    """Split a migration string into individual SQL statements.

    Strips comments and whitespace, splits on ``;``, and returns only
    non-empty statements.  This lets us execute each statement via
    ``db.execute()`` instead of ``db.executescript()``, keeping everything
    in one transaction.
    """
    stmts: list[str] = []
    for raw in sql.split(";"):
        # Strip SQL comments (-- to end of line) and whitespace
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            # Remove inline comments
            idx = stripped.find("--")
            if idx >= 0:
                stripped = stripped[:idx].rstrip()
            if stripped:
                lines.append(stripped)
        stmt = " ".join(lines).strip()
        if stmt:
            stmts.append(stmt)
    return stmts


# Default positions — used only for seeding crew_positions table in migration v38.
# Runtime code should use get_crew_positions() instead.
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
        self._read_db: aiosqlite.Connection | None = None
        self._pending: int = 0
        self._last_flush: float = 0.0
        self._session_active: bool = False
        # Active race id, if any. Maintained by start_race / end_race. Used
        # to tag WS position broadcasts so clients can filter by race.
        self._active_race_id: int | None = None
        self._live: dict[str, float | None] = dict.fromkeys(_LIVE_KEYS)
        self._live_tw_ref: int | None = None
        self._live_tw_angle_raw: float | None = None
        self._on_live_update: Callable[[dict[str, float | None]], None] | None = None
        # Position broadcasts go on a separate callback because they're a
        # different message shape (per-fix lat/lon/ts) and we throttle them
        # to 1 Hz on the wire — GPS can arrive at 5–10 Hz and we don't need
        # that granularity for the live polyline.
        self._on_position_update: Callable[[dict[str, Any]], None] | None = None
        # Per-second position bucket (#732). The SK reader records every fix
        # with source_addr=0 even when Signal K is multiplexing two physical
        # GPS antennas (~3 m apart on Corvo), so consecutive raw fixes
        # zig-zag between antennas. We accumulate every fix in the current
        # whole-second bucket; when the second ticks over, the bucket's
        # mean lat/lon is broadcast as one position. Mirrors the per-second
        # mean averaging in routes.sessions._compute_session_track so the
        # live polyline reads the same as the historical track.
        self._position_bucket_key: str | None = None
        self._position_bucket_lats: list[float] = []
        self._position_bucket_lons: list[float] = []
        self._position_bucket_first_ts: str | None = None
        # Per-channel smoothing applied to live broadcast values (#727).
        # Loaded from app_settings during connect(); refresh_smoothing()
        # picks up admin tau changes without restarting the service.
        from helmlog.smoothing import DEFAULT_TAUS, SmoothingConfig

        self._smoothing: SmoothingConfig = SmoothingConfig.from_taus(DEFAULT_TAUS)
        # Leeway coefficient for the live current compute (#730). Loaded from
        # boat_settings.leeway_coefficient on connect; refresh_leeway_k() picks
        # up admin changes. Default 10 is a reasonable starting point for a
        # 30–40 ft sailboat — admin can tune via /admin/boats.
        self._leeway_k: float = 10.0
        # Per-tack compass offsets for the live current compute (#711). Loaded
        # from boat_settings on connect; refresh_compass_offsets() picks up
        # admin changes. Both default to 0.0 so non-tuned boats keep current
        # behavior.
        self._compass_offset_port: float = 0.0
        self._compass_offset_stbd: float = 0.0
        self._last_rudder_write: float = 0.0
        self._last_attitude_write: float = 0.0
        # Web response cache (#594). Optional; web.py binds a WebCache
        # instance after Storage is connected so any race mutation flows
        # through cache.invalidate(race_id) in-transaction.
        self._race_cache: RaceCache | None = None

    # ------------------------------------------------------------------
    # Race-cache hook (#594)
    # ------------------------------------------------------------------

    def bind_race_cache(self, cache: RaceCache | None) -> None:
        """Register a cache object with an async ``invalidate(race_id)`` method.

        The cache is best-effort — exceptions raised by ``invalidate`` are
        logged and swallowed so cache failures cannot fail a write.
        """
        self._race_cache = cache

    async def _invalidate_race_cache(self, race_id: int) -> None:
        cache = self._race_cache
        if cache is None:
            return
        try:
            await cache.invalidate(race_id)
        except Exception as exc:
            logger.warning("race cache invalidate failed id={}: {}", race_id, exc)

    def _invalidate_sessions_list_cache(self) -> None:
        """Drop T1 `sessions_list` family — used by tag-mutation paths (#608).

        Tag writes (attach/detach/update/delete/merge) change the chip row
        and per-session tag summary that the ``/api/sessions`` response
        carries, but don't flow through the race-mutation path. Calling
        this keeps the list cache coherent without the cost of touching
        per-race T2 entries (which are unaffected by tag writes today).
        """
        cache = self._race_cache
        if cache is None:
            return
        try:
            cache.t1_invalidate_family("sessions_list")
        except Exception as exc:
            logger.warning("sessions_list cache invalidate failed: {}", exc)

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

    def _recompute_set_drift(self) -> None:
        """Derive set / drift from the (already-smoothed) sog, cog, stw, hdg
        + heel in ``self._live``. The heading is leeway-corrected (#730)
        so the result doesn't flip direction on every tack. The output
        runs through its own EMA so a few-degree heading wobble doesn't
        bounce drift by a knot every tick.
        """
        from helmlog.current import compute_set_drift

        sog = self._live["sog_kts"]
        cog = self._live["cog_deg"]
        stw = self._live["bsp_kts"]
        hdg = self._live["heading_deg"]
        heel = self._live.get("heel_deg")
        result = compute_set_drift(
            sog,
            cog,
            stw,
            hdg,
            heel_deg=heel,
            leeway_k=self._leeway_k,
            compass_offset_port=self._compass_offset_port,
            compass_offset_stbd=self._compass_offset_stbd,
        )
        if result is None:
            return
        set_raw, drift_raw = result
        sm = self._smoothing
        self._live["set_deg"] = round(sm.update("set_deg", set_raw), 1)
        self._live["drift_kts"] = round(sm.update("drift_kts", drift_raw), 2)

    def update_live(self, record: PGNRecord) -> None:
        """Update the in-memory live cache from a decoded record (no DB write).

        Wind / speed / heading values pass through ``self._smoothing`` so
        the gauges don't jitter on every CAN tick. The smoothed value is
        what gets stored in ``self._live`` and broadcast over the WS;
        SQLite still records the raw record (see ``write``).
        """
        sm = self._smoothing
        match record:
            case HeadingRecord():
                self._live["heading_deg"] = round(sm.update("heading_deg", record.heading_deg), 1)
                self._recompute_true_wind()
                self._recompute_set_drift()
            case SpeedRecord():
                self._live["bsp_kts"] = round(sm.update("bsp_kts", record.speed_kts), 2)
                self._recompute_set_drift()
            case COGSOGRecord():
                self._live["cog_deg"] = round(sm.update("cog_deg", record.cog_deg), 1)
                self._live["sog_kts"] = round(sm.update("sog_kts", record.sog_kts), 2)
                self._recompute_set_drift()
            case WindRecord() if record.reference == 2:  # apparent
                self._live["aws_kts"] = round(sm.update("aws_kts", record.wind_speed_kts), 1)
                self._live["awa_deg"] = round(sm.update("awa_deg", record.wind_angle_deg), 1)
            case WindRecord() if record.reference in (0, 4):  # true
                self._live["tws_kts"] = round(sm.update("tws_kts", record.wind_speed_kts), 1)
                self._live_tw_ref = record.reference
                # Smoothed angle feeds the recompute below so TWD/TWA come
                # out smoothed too. Vector EMA handles 0/360 wrap.
                channel = "twd_deg" if record.reference == 4 else "twa_deg"
                self._live_tw_angle_raw = sm.update(channel, record.wind_angle_deg)
                self._recompute_true_wind()
            case RudderRecord():
                self._live["rudder_deg"] = round(record.rudder_angle_deg, 1)
            case AttitudeRecord():
                self._live["heel_deg"] = round(record.heel_deg, 1)
                self._live["trim_deg"] = round(record.trim_deg, 1)
                # Heel is an input to the leeway correction, so recompute
                # set/drift whenever it changes.
                self._recompute_set_drift()
            case PositionRecord():
                # Position records bypass the instruments callback and go
                # on the separate position channel. We bucket all fixes
                # within each whole-second key and broadcast the mean
                # lat/lon when the bucket rolls over (see __init__ for
                # rationale — collapses dual-antenna zig-zag).
                ts_iso = record.timestamp.isoformat()
                key = ts_iso[:19]  # truncate to whole-second precision
                if self._position_bucket_key is None:
                    self._position_bucket_key = key
                    self._position_bucket_first_ts = ts_iso
                if key != self._position_bucket_key:
                    # Bucket rolled over — emit the previous second's mean.
                    if self._position_bucket_lats and self._on_position_update is not None:
                        n = len(self._position_bucket_lats)
                        avg_lat = sum(self._position_bucket_lats) / n
                        avg_lon = sum(self._position_bucket_lons) / n
                        self._on_position_update(
                            {
                                "ts": self._position_bucket_first_ts,
                                "lat": avg_lat,
                                "lon": avg_lon,
                                "race_id": self._active_race_id,
                            }
                        )
                    self._position_bucket_key = key
                    self._position_bucket_first_ts = ts_iso
                    self._position_bucket_lats = []
                    self._position_bucket_lons = []
                self._position_bucket_lats.append(record.latitude_deg)
                self._position_bucket_lons.append(record.longitude_deg)
                return
        if self._on_live_update is not None:
            self._on_live_update(dict(self._live))

    def set_live_callback(self, cb: Callable[[dict[str, float | None]], None]) -> None:
        """Register a callback invoked on every live instrument update."""
        self._on_live_update = cb

    def set_position_callback(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked on each new GPS fix (1 Hz throttled)."""
        self._on_position_update = cb

    async def refresh_leeway_k(self) -> float:
        """Reload ``boat_settings.leeway_coefficient`` into the cached
        ``self._leeway_k``. Returns the effective value. Called during
        ``connect()`` and whenever an admin updates boat settings."""
        try:
            rows = await self.current_boat_settings(race_id=None)
        except Exception:
            return self._leeway_k
        import contextlib

        for row in rows:
            if row["parameter"] == "leeway_coefficient":
                with contextlib.suppress(TypeError, ValueError):
                    self._leeway_k = float(row["value"])
                break
        return self._leeway_k

    async def refresh_compass_offsets(self) -> tuple[float, float]:
        """Reload ``boat_settings.compass_offset_port`` /
        ``compass_offset_stbd`` into the cached attributes (#711). Returns
        ``(port, stbd)``. Called from ``connect()`` and on every settings
        write that touches either parameter."""
        try:
            rows = await self.current_boat_settings(race_id=None)
        except Exception:
            return (self._compass_offset_port, self._compass_offset_stbd)
        import contextlib

        for row in rows:
            param = row["parameter"]
            if param not in ("compass_offset_port", "compass_offset_stbd"):
                continue
            with contextlib.suppress(TypeError, ValueError):
                val = float(row["value"])
                if param == "compass_offset_port":
                    self._compass_offset_port = val
                else:
                    self._compass_offset_stbd = val
        return (self._compass_offset_port, self._compass_offset_stbd)

    async def refresh_smoothing(self) -> dict[str, float]:
        """Reload per-channel time constants from ``app_settings`` and apply
        them to the live smoothers without losing accumulated state.

        Returns the effective tau map so the caller (admin API) can echo
        back what's now in force. Settings keys are
        ``smoothing.<channel>.tau_s``; missing or malformed values fall
        back to ``DEFAULT_TAUS``.
        """
        from helmlog.smoothing import DEFAULT_TAUS, parse_tau

        out: dict[str, float] = {}
        for channel, default in DEFAULT_TAUS.items():
            raw = await self.get_setting(f"smoothing.{channel}.tau_s")
            tau = parse_tau(raw, default)
            self._smoothing.set_tau(channel, tau)
            out[channel] = tau
        return out

    def live_instruments(self) -> dict[str, float | None]:
        """Return a snapshot of the current in-memory instrument cache."""
        return dict(self._live)

    async def connect(self) -> None:
        """Open the database connection."""
        import os
        import stat

        db_path = self._config.db_path
        is_new = not os.path.exists(db_path)
        self._db = await aiosqlite.connect(db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        if is_new and os.path.exists(db_path):
            # Python sqlite3 creates files with 0644 regardless of umask.
            # Add group-write so both the helmlog service account and the
            # SSH login user (which setup.sh places in the helmlog group)
            # can read/write the DB.
            st = os.stat(db_path)
            os.chmod(db_path, st.st_mode | stat.S_IWGRP)
        self._last_flush = time.monotonic()
        logger.info("Storage connected: {}", self._config.db_path)
        await self.migrate()
        # Open a separate read-only connection for web queries so the write
        # path (instrument data at 1 Hz) never blocks page loads.  :memory:
        # databases are per-connection, so skip the read connection there.
        if db_path != ":memory:":
            try:
                self._read_db = await aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True)
                self._read_db.row_factory = aiosqlite.Row
                await self._read_db.execute("PRAGMA foreign_keys = ON")
            except Exception:
                logger.warning("Failed to open read connection; falling back to single connection")
                self._read_db = None
        current = await self.get_current_race()
        self._session_active = current is not None
        self._active_race_id = current.id if current is not None else None
        await self.refresh_smoothing()
        await self.refresh_leeway_k()
        await self.refresh_compass_offsets()

    async def close(self) -> None:
        """Flush any buffered writes and close the database connections."""
        if self._read_db is not None:
            await self._read_db.close()
            self._read_db = None
            logger.debug("Read connection closed")
        if self._db is not None:
            await self._flush()
            await self._db.close()
            self._db = None
            logger.info("Storage closed")

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not connected; call connect() first")
        return self._db

    def _read_conn(self) -> aiosqlite.Connection:
        """Return the read connection, falling back to the write connection."""
        if self._read_db is not None:
            return self._read_db
        return self._conn()

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def migrate(self) -> None:
        """Apply any pending schema migrations.

        Each migration's DDL and its version record are executed in the same
        transaction so they are atomic — either both persist or neither does.
        We avoid ``executescript()`` because it issues an implicit COMMIT
        before running, which breaks atomicity with the version insert and can
        leave the schema_version table out of sync with the actual schema.
        """
        db = self._conn()

        # Ensure version table exists
        await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        await db.commit()

        cur = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        current = row[0] if row and row[0] is not None else 0

        # Repair pass: re-run idempotent DDL from already-applied migrations.
        # The old executescript()-based migrate() could record a version in
        # schema_version without the DDL actually persisting.  We re-run:
        #  - CREATE TABLE/INDEX IF NOT EXISTS (always safe)
        #  - ALTER TABLE ADD COLUMN (catch "duplicate column" and move on)
        # DROP/RENAME/INSERT patterns are skipped — they are not idempotent.
        repaired = 0
        for version in sorted(_MIGRATIONS):
            if version > current:
                break
            for stmt in _split_migration_sql(_MIGRATIONS[version]):
                upper = stmt.lstrip().upper()
                is_create = upper.startswith("CREATE TABLE IF NOT EXISTS") or upper.startswith(
                    "CREATE INDEX IF NOT EXISTS"
                )
                is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
                if not (is_create or is_alter_add):
                    continue
                # Skip internal temp tables from DROP/RENAME migration patterns
                if "_new " in stmt or "_new(" in stmt:
                    continue
                try:
                    await db.execute(stmt)
                    repaired += 1
                except Exception:  # noqa: BLE001
                    pass  # Table/column already exists — expected
        if repaired:
            await db.commit()
            logger.info("Schema repair: re-applied {} DDL statements", repaired)

        for version in sorted(_MIGRATIONS):
            if version <= current:
                continue
            logger.info("Applying schema migration v{}", version)
            # Split migration text into individual statements and execute each
            # one via db.execute() so they share a transaction with the version
            # record insert.  This keeps DDL + version tracking atomic.
            for stmt in _split_migration_sql(_MIGRATIONS[version]):
                upper = stmt.lstrip().upper()
                is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
                if is_alter_add:
                    try:  # noqa: SIM105
                        await db.execute(stmt)
                    except Exception:  # noqa: BLE001
                        pass  # Column already exists — partial prior migration
                else:
                    await db.execute(stmt)
            await db.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,)
            )
            await db.commit()

        # Post-DDL data migration for v38 (crew overhaul)
        if current < 38:
            await self._migrate_v38_data()

        # Post-DDL data migration for v41 (sail_changes from race_sails)
        if current < 41:
            await self._migrate_v41_sail_changes()

        # Post-DDL data migration for v55 (unified controls).
        if current < 55:
            await self._migrate_v55_controls()

        # PARAMETERS-as-source-of-truth sync (#711 follow-up). Always called;
        # idempotent INSERT OR IGNORE means new ParameterDef additions to
        # boat_settings.PARAMETERS auto-flow into the controls table on the
        # next service start without needing a new migration each time.
        await self._sync_controls_from_parameters()

        # Post-DDL data migration for v56 (seed categories)
        if current < 56:
            await self._migrate_v56_categories()

        # Post-DDL data migration for v58 (backfill race slugs). Always
        # called — the method is idempotent (no-op when every row has a
        # slug) so a previously-failed partial backfill gets repaired on
        # the next boot instead of leaving rows with NULL slugs forever.
        await self._migrate_v58_slugs()

        # Post-DDL data migration for v80 (moments unification #662).
        if current < 80:
            await self._migrate_v80_moments()

        logger.debug("Schema is at version {}", _CURRENT_VERSION)

    async def _migrate_v38_data(self) -> None:
        """Data migration for v38: seed positions, migrate race_crew → crew_defaults."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()

        # 1. Seed crew_positions (only if empty — idempotent)
        cur = await db.execute("SELECT COUNT(*) FROM crew_positions")
        row = await cur.fetchone()
        if row is not None and row[0] == 0:
            for order, name in enumerate(("helm", "main", "pit", "bow", "tactician", "guest")):
                await db.execute(
                    "INSERT INTO crew_positions (name, display_order, created_at) VALUES (?, ?, ?)",
                    (name, order, now),
                )
            await db.commit()
            logger.info("Seeded crew_positions with 6 default positions")

        # Build position name → id map
        cur = await db.execute("SELECT id, name FROM crew_positions")
        pos_map: dict[str, int] = {r["name"]: r["id"] for r in await cur.fetchall()}

        # 2. Check if race_crew table exists (it won't on fresh DBs)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='race_crew'"
        )
        if await cur.fetchone() is None:
            return  # Fresh DB, no data to migrate

        # 3. Migrate race_crew rows into crew_defaults
        # First, collect distinct sailor names and match to users
        cur = await db.execute("SELECT DISTINCT sailor FROM race_crew")
        sailor_rows = await cur.fetchall()
        sailor_to_user: dict[str, int] = {}
        for sr in sailor_rows:
            name = sr["sailor"]
            # Try to match to existing user by name
            ucur = await db.execute("SELECT id FROM users WHERE name = ?", (name,))
            urow = await ucur.fetchone()
            if urow:
                sailor_to_user[name] = urow["id"]
            else:
                # Create placeholder user (no email/credentials)
                placeholder_email = f"placeholder+{name.lower().replace(' ', '.')}@helmlog.local"
                try:
                    pcur = await db.execute(
                        "INSERT INTO users (email, name, role, created_at)"
                        " VALUES (?, ?, 'viewer', ?)",
                        (placeholder_email, name, now),
                    )
                    sailor_to_user[name] = pcur.lastrowid or 0
                except Exception:  # noqa: BLE001
                    # Email conflict — find existing
                    ecur = await db.execute(
                        "SELECT id FROM users WHERE email = ?", (placeholder_email,)
                    )
                    erow = await ecur.fetchone()
                    if erow:
                        sailor_to_user[name] = erow["id"]
        await db.commit()

        # 4. Insert race_crew rows into crew_defaults
        cur = await db.execute("SELECT race_id, position, sailor FROM race_crew")
        rc_rows = await cur.fetchall()
        for rc in rc_rows:
            position_id = pos_map.get(rc["position"])
            if position_id is None:
                continue  # Unknown position, skip
            user_id = sailor_to_user.get(rc["sailor"])
            await db.execute(
                "INSERT OR IGNORE INTO crew_defaults"
                " (race_id, position_id, user_id, attributed, created_at)"
                " VALUES (?, ?, ?, 1, ?)",
                (rc["race_id"], position_id, user_id, now),
            )
        await db.commit()

        # 5. Migrate crew_consents sailor_name → user_id
        cur = await db.execute("SELECT id, sailor_name FROM crew_consents WHERE user_id IS NULL")
        consent_rows = await cur.fetchall()
        for cr in consent_rows:
            uid = sailor_to_user.get(cr["sailor_name"])
            if uid is not None:
                await db.execute(
                    "UPDATE crew_consents SET user_id = ? WHERE id = ?",
                    (uid, cr["id"]),
                )
        await db.commit()

        # 6. Rebuild crew_consents without sailor_name column
        await db.execute(
            "CREATE TABLE IF NOT EXISTS crew_consents_new ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  user_id      INTEGER REFERENCES users(id),"
            "  consent_type TEXT    NOT NULL,"
            "  granted      INTEGER NOT NULL DEFAULT 1,"
            "  granted_at   TEXT    NOT NULL,"
            "  revoked_at   TEXT,"
            "  UNIQUE(user_id, consent_type)"
            ")"
        )
        await db.execute(
            "INSERT OR IGNORE INTO crew_consents_new"
            " (id, user_id, consent_type, granted, granted_at, revoked_at)"
            " SELECT id, user_id, consent_type, granted, granted_at, revoked_at"
            " FROM crew_consents"
        )
        await db.execute("DROP TABLE crew_consents")
        await db.execute("ALTER TABLE crew_consents_new RENAME TO crew_consents")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crew_consents_user ON crew_consents(user_id)"
        )
        await db.commit()

        # 7. Drop old tables
        await db.execute("DROP TABLE IF EXISTS race_crew")
        await db.execute("DROP TABLE IF EXISTS recent_sailors")
        await db.commit()
        logger.info("v38 data migration complete: crew_defaults populated, old tables dropped")

    async def _migrate_v41_sail_changes(self) -> None:
        """Data migration for v41: convert race_sails rows into sail_changes."""
        db = self._conn()

        # Check if sail_changes is already populated (idempotent)
        cur = await db.execute("SELECT COUNT(*) FROM sail_changes")
        row = await cur.fetchone()
        if row is not None and row[0] > 0:
            return

        # For each race that has race_sails entries, create a sail_changes row
        # using the race start_utc as the timestamp.
        cur = await db.execute(
            "SELECT DISTINCT rs.race_id, r.start_utc"
            " FROM race_sails rs"
            " JOIN races r ON r.id = rs.race_id"
            " WHERE r.start_utc IS NOT NULL"
        )
        races = list(await cur.fetchall())
        for race in races:
            race_id = race["race_id"]
            ts = race["start_utc"]
            # Collect sail_ids by type for this race
            scur = await db.execute(
                "SELECT s.type, s.id"
                " FROM race_sails rs JOIN sails s ON s.id = rs.sail_id"
                " WHERE rs.race_id = ?",
                (race_id,),
            )
            sails_by_type: dict[str, int | None] = {"main": None, "jib": None, "spinnaker": None}
            for srow in await scur.fetchall():
                if srow["type"] in sails_by_type:
                    sails_by_type[srow["type"]] = srow["id"]
            await db.execute(
                "INSERT INTO sail_changes (race_id, ts, main_id, jib_id, spinnaker_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    race_id,
                    ts,
                    sails_by_type["main"],
                    sails_by_type["jib"],
                    sails_by_type["spinnaker"],
                ),
            )
        await db.commit()
        if races:
            logger.info(
                "v41 data migration: converted {} race_sails rows to sail_changes", len(races)
            )

    async def _migrate_v55_controls(self) -> None:
        """Data migration for v55: seed controls from canonical parameters + ArUco data."""
        import json as _json

        from helmlog.boat_settings import (
            PARAMETERS,
            WEIGHT_DISTRIBUTION_PRESETS,
        )

        db = self._conn()

        # Idempotent check
        cur = await db.execute("SELECT COUNT(*) FROM controls")
        row = await cur.fetchone()
        if row is not None and row[0] > 0:
            return

        # 1. Seed all 37 canonical parameters
        for order, p in enumerate(PARAMETERS):
            preset_json = None
            if p.name == "weight_distribution":
                preset_json = _json.dumps(list(WEIGHT_DISTRIBUTION_PRESETS))
            await db.execute(
                "INSERT OR IGNORE INTO controls"
                " (name, label, unit, input_type, category, sort_order, preset_values)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (p.name, p.label, p.unit, p.input_type, p.category, order, preset_json),
            )

        # 2. Migrate ArUco controls → unified controls + control_aruco
        cur = await db.execute(
            "SELECT id, name, camera_id, marker_id_a, marker_id_b, tolerance_mm FROM aruco_controls"
        )
        aruco_rows = list(await cur.fetchall())
        for ac in aruco_rows:
            # Insert control if it doesn't already exist (e.g., "vang" already seeded)
            await db.execute(
                "INSERT OR IGNORE INTO controls (name, label, unit, category, sort_order)"
                " VALUES (?, ?, 'cm', 'sail_controls', 100)",
                (ac["name"], ac["name"].replace("_", " ").title()),
            )
            # Get the control ID
            ccur = await db.execute("SELECT id FROM controls WHERE name = ?", (ac["name"],))
            ctrl_row = await ccur.fetchone()
            if ctrl_row:
                await db.execute(
                    "INSERT OR IGNORE INTO control_aruco"
                    " (control_id, camera_id, marker_id_a, marker_id_b, tolerance_mm)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        ctrl_row["id"],
                        ac["camera_id"],
                        ac["marker_id_a"],
                        ac["marker_id_b"],
                        ac["tolerance_mm"],
                    ),
                )

        # 3. Migrate aruco_trigger_words → control_trigger_words
        cur = await db.execute(
            "SELECT tw.phrase, ac.name AS control_name"
            " FROM aruco_trigger_words tw"
            " JOIN aruco_controls ac ON ac.id = tw.control_id"
        )
        for tw in await cur.fetchall():
            ccur = await db.execute("SELECT id FROM controls WHERE name = ?", (tw["control_name"],))
            ctrl_row = await ccur.fetchone()
            if ctrl_row:
                await db.execute(
                    "INSERT OR IGNORE INTO control_trigger_words (control_id, phrase)"
                    " VALUES (?, ?)",
                    (ctrl_row["id"], tw["phrase"]),
                )

        # 4. Seed Whisper aliases as trigger words
        whisper_aliases: dict[str, list[str]] = {
            "main_halyard": ["main higher", "main hires", "main hired", "main halyards"],
            "jib_halyard": [
                "jib higher",
                "jib hires",
                "jib hired",
                "jib howard",
                "jib halyards",
            ],
            "vang": ["bang", "boom bang", "van"],
            "cunningham": ["cunninghams"],
            "main_sheet_tension": [
                "main cheat tension",
                "main cheap tension",
                "main sheat tension",
            ],
            "jib_sheet_tension_port": [
                "gybsheet tension port",
                "gyb sheet tension port",
                "jibsheet tension port",
            ],
            "jib_sheet_tension_starboard": [
                "gybsheet tension starboard",
                "gyb sheet tension starboard",
                "jibsheet tension starboard",
            ],
        }
        for param_name, aliases in whisper_aliases.items():
            ccur = await db.execute("SELECT id FROM controls WHERE name = ?", (param_name,))
            ctrl_row = await ccur.fetchone()
            if ctrl_row:
                for alias in aliases:
                    await db.execute(
                        "INSERT OR IGNORE INTO control_trigger_words (control_id, phrase)"
                        " VALUES (?, ?)",
                        (ctrl_row["id"], alias),
                    )

        await db.commit()
        logger.info(
            "v55 data migration: seeded {} controls + {} ArUco mappings",
            len(PARAMETERS) + len(aruco_rows),
            len(aruco_rows),
        )

    async def _sync_controls_from_parameters(self) -> None:
        """Ensure every ParameterDef in ``boat_settings.PARAMETERS`` is
        present in the ``controls`` table (#711 follow-up).

        Runs every connect. ``INSERT OR IGNORE`` against the UNIQUE(name)
        constraint makes it idempotent — existing rows aren't touched
        (admins may have customized label/sort_order), and new
        PARAMETERS entries appear on /admin/controls without needing a
        per-addition migration.
        """
        from helmlog.boat_settings import PARAMETERS

        db = self._conn()
        added = 0
        for order, p in enumerate(PARAMETERS):
            cur = await db.execute(
                "INSERT OR IGNORE INTO controls"
                " (name, label, unit, input_type, category, sort_order)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (p.name, p.label, p.unit, p.input_type, p.category, order),
            )
            if cur.rowcount > 0:
                added += 1
        if added:
            await db.commit()
            logger.info("Synced {} new ParameterDef(s) into controls table", added)

    async def _migrate_v56_categories(self) -> None:
        """Data migration for v56: seed control_categories from CATEGORY_ORDER."""
        from helmlog.boat_settings import CATEGORY_ORDER

        db = self._conn()

        cur = await db.execute("SELECT COUNT(*) FROM control_categories")
        row = await cur.fetchone()
        if row is not None and row[0] > 0:
            return

        for order, (name, label) in enumerate(CATEGORY_ORDER):
            await db.execute(
                "INSERT OR IGNORE INTO control_categories (name, label, sort_order)"
                " VALUES (?, ?, ?)",
                (name, label, order),
            )
        await db.commit()
        logger.info("v56 data migration: seeded {} categories", len(CATEGORY_ORDER))

    async def _migrate_v80_moments(self) -> None:
        """Data migration for v80 (#662): collapse bookmarks, comment_threads,
        and session_notes into the unified ``moments`` primitive.

        Idempotent: if ``moments`` already has rows, or if the old tables have
        already been dropped, the method is a no-op. Runs with foreign keys
        disabled so dropping comment_threads does not cascade-delete the
        notifications we are remapping.
        """
        db = self._conn()

        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='comment_threads'"
        )
        if await cur.fetchone() is None:
            return  # old tables already gone (re-entrant run after completion)

        cur = await db.execute("SELECT COUNT(*) FROM moments")
        row = await cur.fetchone()
        if row is not None and row[0] > 0:
            return  # partial prior run populated moments; abort to avoid dupes

        # PRAGMA foreign_keys is a no-op inside a transaction; flush first.
        await db.commit()
        await db.execute("PRAGMA foreign_keys = OFF")

        # --- 1. comment_threads -> moments (build thread_id -> moment_id map) ---
        thread_to_moment: dict[int, int] = {}
        cur = await db.execute(
            "SELECT id, session_id, title, anchor_kind, anchor_entity_id,"
            " anchor_t_start, anchor_t_end, resolved, resolved_at, resolved_by,"
            " resolution_summary, created_by, created_at, updated_at"
            " FROM comment_threads ORDER BY id"
        )
        thread_rows = list(await cur.fetchall())
        for tr in thread_rows:
            # Threads with bookmark-kind anchors: we no longer have bookmarks.
            # Downgrade to session-scope so the moment survives (t_start is
            # unknown post-bookmark-drop).
            ak = tr["anchor_kind"]
            ae = tr["anchor_entity_id"]
            ats = tr["anchor_t_start"]
            ate = tr["anchor_t_end"]
            if ak == "bookmark":
                ak, ae, ats, ate = "session", None, None, None
            elif ak in {"race", "start"}:
                # race/start anchors collapse to session-scope: the moment's
                # session_id already identifies the race.
                ak, ae, ats, ate = "session", None, None, None
            elif ak == "segment":
                ak = "timestamp"  # point-or-range is the timestamp kind now
            elif ak is None:
                ak = "session"
            ins = await db.execute(
                "INSERT INTO moments"
                " (session_id, subject, counterparty, anchor_kind, anchor_entity_id,"
                "  anchor_t_start, anchor_t_end, resolved, resolved_at, resolved_by,"
                "  resolution_summary, source, created_by, created_at, updated_at)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?)",
                (
                    tr["session_id"],
                    tr["title"],
                    ak,
                    ae,
                    ats,
                    ate,
                    tr["resolved"] or 0,
                    tr["resolved_at"],
                    tr["resolved_by"],
                    tr["resolution_summary"],
                    tr["created_by"],
                    tr["created_at"],
                    tr["updated_at"],
                ),
            )
            thread_to_moment[int(tr["id"])] = int(ins.lastrowid or 0)

        # --- 2. comments -> comments_new (with thread_id remapped) ---
        cur = await db.execute(
            "SELECT id, thread_id, author, body, created_at, edited_at FROM comments ORDER BY id"
        )
        for cr in await cur.fetchall():
            moment_id = thread_to_moment.get(int(cr["thread_id"]))
            if moment_id is None:
                continue  # orphan comment — thread vanished somehow, drop
            await db.execute(
                "INSERT INTO comments_new"
                " (id, moment_id, author, body, created_at, edited_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    cr["id"],
                    moment_id,
                    cr["author"],
                    cr["body"],
                    cr["created_at"],
                    cr["edited_at"],
                ),
            )

        # --- 3. comment_read_state -> comment_read_state_new ---
        cur = await db.execute("SELECT user_id, thread_id, last_read FROM comment_read_state")
        for rr in await cur.fetchall():
            moment_id = thread_to_moment.get(int(rr["thread_id"]))
            if moment_id is None:
                continue
            await db.execute(
                "INSERT OR IGNORE INTO comment_read_state_new"
                " (user_id, moment_id, last_read) VALUES (?, ?, ?)",
                (rr["user_id"], moment_id, rr["last_read"]),
            )

        # --- 4. notifications -> notifications_new (source_thread_id remapped) ---
        cur = await db.execute(
            "SELECT id, user_id, type, source_thread_id, source_comment_id,"
            " session_id, actor_id, message, created_at, read, dismissed"
            " FROM notifications ORDER BY id"
        )
        for nr in await cur.fetchall():
            src_thread = nr["source_thread_id"]
            src_moment = thread_to_moment.get(int(src_thread)) if src_thread is not None else None
            await db.execute(
                "INSERT INTO notifications_new"
                " (id, user_id, type, source_moment_id, source_comment_id,"
                "  session_id, actor_id, message, created_at, read, dismissed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    nr["id"],
                    nr["user_id"],
                    nr["type"],
                    src_moment,
                    nr["source_comment_id"],
                    nr["session_id"],
                    nr["actor_id"],
                    nr["message"],
                    nr["created_at"],
                    nr["read"],
                    nr["dismissed"],
                ),
            )

        # --- 5. session_notes -> moments / moment_attachments / session_settings ---
        note_to_moment: dict[int, int] = {}
        cur = await db.execute(
            "SELECT id, race_id, audio_session_id, ts, note_type, body,"
            " photo_path, created_at, user_id FROM session_notes ORDER BY id"
        )
        skipped_audio_only = 0
        for nr in await cur.fetchall():
            note_type = nr["note_type"]
            if note_type == "settings":
                await db.execute(
                    "INSERT INTO session_settings"
                    " (session_id, audio_session_id, ts, body, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        nr["race_id"],
                        nr["audio_session_id"],
                        nr["ts"],
                        nr["body"] or "{}",
                        nr["user_id"],
                        nr["created_at"],
                    ),
                )
                continue

            race_id = nr["race_id"]
            if race_id is None:
                # Audio-only notes have no session to anchor on. Skip for now
                # (the user confirmed this is acceptable — see #662 rationale).
                skipped_audio_only += 1
                continue

            ts = nr["ts"]
            ins = await db.execute(
                "INSERT INTO moments"
                " (session_id, subject, counterparty, anchor_kind, anchor_entity_id,"
                "  anchor_t_start, anchor_t_end, resolved, source, created_by,"
                "  created_at, updated_at)"
                " VALUES (?, NULL, NULL, 'timestamp', NULL, ?, NULL, 0,"
                "         'manual', ?, ?, ?)",
                (race_id, ts, nr["user_id"], nr["created_at"], nr["created_at"]),
            )
            moment_id = int(ins.lastrowid or 0)
            note_to_moment[int(nr["id"])] = moment_id

            if note_type == "photo" or nr["photo_path"]:
                await db.execute(
                    "INSERT INTO moment_attachments"
                    " (moment_id, kind, path, body, created_by, created_at)"
                    " VALUES (?, 'photo', ?, NULL, ?, ?)",
                    (moment_id, nr["photo_path"], nr["user_id"], nr["created_at"]),
                )
                # A photo note may still carry a caption in body — keep it
                # as a comment on the moment.
                if nr["body"]:
                    await db.execute(
                        "INSERT INTO comments_new"
                        " (moment_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                        (moment_id, nr["user_id"], nr["body"], nr["created_at"]),
                    )
            elif nr["body"]:
                await db.execute(
                    "INSERT INTO comments_new"
                    " (moment_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                    (moment_id, nr["user_id"], nr["body"], nr["created_at"]),
                )

        # --- 6. entity_tags remap ---
        # thread -> moment
        cur = await db.execute(
            "SELECT rowid, entity_id FROM entity_tags WHERE entity_type = 'thread'"
        )
        for er in await cur.fetchall():
            moment_id = thread_to_moment.get(int(er["entity_id"]))
            if moment_id is None:
                await db.execute("DELETE FROM entity_tags WHERE rowid = ?", (er["rowid"],))
            else:
                await db.execute(
                    "UPDATE entity_tags SET entity_type = 'moment', entity_id = ? WHERE rowid = ?",
                    (moment_id, er["rowid"]),
                )
        # session_note -> moment
        cur = await db.execute(
            "SELECT rowid, entity_id FROM entity_tags WHERE entity_type = 'session_note'"
        )
        for er in await cur.fetchall():
            moment_id = note_to_moment.get(int(er["entity_id"]))
            if moment_id is None:
                await db.execute("DELETE FROM entity_tags WHERE rowid = ?", (er["rowid"],))
            else:
                await db.execute(
                    "UPDATE entity_tags SET entity_type = 'moment', entity_id = ? WHERE rowid = ?",
                    (moment_id, er["rowid"]),
                )
        # bookmark -> gone
        await db.execute("DELETE FROM entity_tags WHERE entity_type = 'bookmark'")

        # --- 7. drop old tables ---
        # Order matters only to keep dependent indexes from lingering; with
        # foreign_keys=OFF, SQLite doesn't check FK references on DROP.
        await db.execute("DROP TABLE IF EXISTS comment_read_state")
        await db.execute("DROP TABLE IF EXISTS comments")
        await db.execute("DROP TABLE IF EXISTS notifications")
        await db.execute("DROP TABLE IF EXISTS comment_threads")
        await db.execute("DROP TABLE IF EXISTS session_notes")
        await db.execute("DROP TABLE IF EXISTS bookmarks")

        # --- 8. rename staging tables to final names ---
        await db.execute("ALTER TABLE comments_new RENAME TO comments")
        await db.execute("ALTER TABLE comment_read_state_new RENAME TO comment_read_state")
        await db.execute("ALTER TABLE notifications_new RENAME TO notifications")

        # --- 9. (re)create indexes under the final table names ---
        # idx_comments_new_moment survives the rename (it still attaches to
        # the renamed table) but keep the tidy name too.
        await db.execute("DROP INDEX IF EXISTS idx_comments_new_moment")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_comments_moment ON comments(moment_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_session ON notifications(session_id)"
        )

        await db.commit()
        await db.execute("PRAGMA foreign_keys = ON")
        logger.info(
            "v80 data migration complete: {} moments from threads, {} from notes,"
            " {} audio-only notes skipped",
            len(thread_to_moment),
            len(note_to_moment),
            skipped_audio_only,
        )

    async def _migrate_v58_slugs(self) -> None:
        """Data migration for v58: backfill ``races.slug`` from ``name`` (#449).

        Slug is the slugified ``name``; when two rows collide, the row with the
        lowest ``id`` keeps the bare slug and higher-id rows get ``-2``, ``-3``
        suffixes. Empty slugs (e.g. a name that's all punctuation) fall back to
        ``race-{id}``.
        """
        from helmlog.races import slugify

        db = self._conn()
        cur = await db.execute("SELECT id, name FROM races WHERE slug IS NULL ORDER BY id ASC")
        rows = list(await cur.fetchall())
        if not rows:
            return

        # Seed the used-set with any slugs already present on rows we're not
        # touching (defensive — a prior partial backfill).
        used_cur = await db.execute("SELECT slug FROM races WHERE slug IS NOT NULL")
        used: set[str] = {r["slug"] for r in await used_cur.fetchall()}

        for row in rows:
            base = slugify(row["name"]) or f"race-{row['id']}"
            slug = base
            n = 2
            while slug in used:
                slug = f"{base}-{n}"
                n += 1
            used.add(slug)
            await db.execute("UPDATE races SET slug = ? WHERE id = ?", (slug, row["id"]))
        await db.commit()
        logger.info("v58 data migration: backfilled slugs for {} races", len(rows))

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
            case RudderRecord():
                await self._write_rudder(record)
            case AttitudeRecord():
                await self._write_attitude(record)
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
            "INSERT INTO headings (ts, source_addr, heading_deg, deviation_deg, variation_deg,"
            " race_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _ts(r.timestamp),
                r.source_addr,
                r.heading_deg,
                r.deviation_deg,
                r.variation_deg,
                self._active_race_id,
            ),
        )

    async def _write_speed(self, r: SpeedRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts, race_id) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.speed_kts, self._active_race_id),
        )

    async def _write_depth(self, r: DepthRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO depths (ts, source_addr, depth_m, offset_m, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.depth_m, r.offset_m, self._active_race_id),
        )

    async def _write_position(self, r: PositionRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                _ts(r.timestamp),
                r.source_addr,
                r.latitude_deg,
                r.longitude_deg,
                self._active_race_id,
            ),
        )

    async def _write_cogsog(self, r: COGSOGRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.cog_deg, r.sog_kts, self._active_race_id),
        )

    async def _write_wind(self, r: WindRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference,"
            " race_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _ts(r.timestamp),
                r.source_addr,
                r.wind_speed_kts,
                r.wind_angle_deg,
                r.reference,
                self._active_race_id,
            ),
        )

    async def _write_environmental(self, r: EnvironmentalRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO environmental (ts, source_addr, water_temp_c) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.water_temp_c),
        )

    async def _write_rudder(self, r: RudderRecord) -> None:
        now = time.monotonic()
        try:
            hz = float(os.environ.get("RUDDER_STORAGE_HZ", "2"))
        except ValueError:
            hz = 2.0
        if hz > 0 and (now - self._last_rudder_write) < (1.0 / hz):
            return
        self._last_rudder_write = now
        db = self._conn()
        await db.execute(
            "INSERT INTO rudder_angles (ts, source_addr, rudder_angle_deg) VALUES (?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.rudder_angle_deg),
        )

    async def _write_attitude(self, r: AttitudeRecord) -> None:
        now = time.monotonic()
        try:
            hz = float(os.environ.get("ATTITUDE_STORAGE_HZ", "2"))
        except ValueError:
            hz = 2.0
        if hz > 0 and (now - self._last_attitude_write) < (1.0 / hz):
            return
        self._last_attitude_write = now
        db = self._conn()
        await db.execute(
            "INSERT INTO attitudes (ts, source_addr, heel_deg, trim_deg) VALUES (?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.heel_deg, r.trim_deg),
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
            "attitudes",
            "environmental",
        }
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Unknown table: {table!r}")

        db = self._read_conn()
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
        db = self._read_conn()
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

        db = self._read_conn()
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
        db = self._read_conn()
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
        db = self._read_conn()
        cur = await db.execute(
            "SELECT * FROM tides WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (_ts(start), _ts(end)),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def latest_position(self) -> dict[str, Any] | None:
        """Return the most recent row from the positions table, or None."""
        db = self._read_conn()
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

        conn = self._read_conn()

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
        rdr = await _q("rudder_angles", "rudder_angle_deg")

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
            "rudder_deg": round(rdr["rudder_angle_deg"], 1) if rdr else None,
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
            "  race_id, session_type, name, channel_map,"
            "  vendor_id, product_id, serial, usb_port_path,"
            "  capture_group_id, capture_ordinal)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                json.dumps(session.channel_map) if session.channel_map else None,
                # USB identity carried through from AudioRecorder.start(detected=...) —
                # previously dropped on insert so sibling/multi-channel sessions
                # landed with NULL identity and could not resolve the admin-default
                # channel_map via the v63 composite key. Per-session overrides still
                # worked because they are keyed by audio_session_id, so this bug was
                # masked in tests that explicitly seeded overrides.
                session.vendor_id or None,
                session.product_id or None,
                session.serial or None,
                session.usb_port_path or None,
                session.capture_group_id,
                session.capture_ordinal,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.debug("Audio session stored: id={} file={}", cur.lastrowid, session.file_path)
        return cur.lastrowid

    async def set_audio_session_device(
        self,
        session_id: int,
        *,
        vendor_id: int,
        product_id: int,
        serial: str,
        usb_port_path: str,
    ) -> None:
        """Persist the active USB device identity for an audio session (#494)."""
        db = self._conn()
        await db.execute(
            "UPDATE audio_sessions"
            " SET vendor_id=?, product_id=?, serial=?, usb_port_path=?"
            " WHERE id=?",
            (vendor_id, product_id, serial, usb_port_path, session_id),
        )
        await db.commit()

    async def get_channel_map_for_audio_session(self, session_id: int) -> dict[int, str]:
        """Return the channel→position map for an audio session.

        Looks up the session's stored device identity then chains through
        ``get_channel_map`` so the per-session override (if any) takes
        precedence over the admin default.
        """
        row = await self.get_audio_session_row(session_id)
        if not row:
            return {}
        vendor_id = row.get("vendor_id")
        product_id = row.get("product_id")
        if vendor_id is None or product_id is None:
            return {}
        return await self.get_channel_map(
            vendor_id=int(vendor_id),
            product_id=int(product_id),
            serial=row.get("serial") or "",
            usb_port_path=row.get("usb_port_path") or "",
            audio_session_id=session_id,
        )

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
        cur = await self._read_conn().execute(
            "SELECT id, file_path, device_name, start_utc, end_utc, sample_rate, channels,"
            " race_id, session_type, name, channel_map,"
            " vendor_id, product_id, serial, usb_port_path,"
            " capture_group_id, capture_ordinal"
            " FROM audio_sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("channel_map"):
            try:
                cmap = json.loads(res["channel_map"])
                res["channel_map"] = {int(k): v for k, v in cmap.items()}
            except (json.JSONDecodeError, ValueError):
                pass
        return res

    async def get_race_primary_audio_session(self, race_id: int) -> dict[str, Any] | None:
        """Return the race/practice primary audio_session row for a race, if any.

        Used at debrief-start to recover the race's ``capture_group_id`` so
        the debrief can detect whether the USB device set has changed (#648).
        """
        cur = await self._read_conn().execute(
            "SELECT id, channels, channel_map, capture_group_id, capture_ordinal,"
            " vendor_id, product_id, serial, usb_port_path"
            " FROM audio_sessions"
            " WHERE race_id = ? AND session_type IN ('race', 'practice')"
            " ORDER BY capture_ordinal ASC, id ASC LIMIT 1",
            (race_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_capture_group_siblings(self, capture_group_id: str) -> list[dict[str, Any]]:
        """Return all audio_sessions rows sharing a capture_group_id, in ordinal order.

        Used by the sibling-card playback path (#509) to discover every WAV
        that belongs to a single start/stop cycle across multiple mono USB
        receivers. Returns an empty list if the group is unknown.
        """
        cur = await self._read_conn().execute(
            "SELECT id, file_path, device_name, start_utc, end_utc, sample_rate, channels,"
            " race_id, session_type, name, channel_map,"
            " vendor_id, product_id, serial, usb_port_path,"
            " capture_group_id, capture_ordinal"
            " FROM audio_sessions WHERE capture_group_id = ?"
            " ORDER BY capture_ordinal ASC",
            (capture_group_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_audio_sessions(self) -> list[AudioSession]:
        """Return all audio sessions ordered by start_utc descending."""
        from datetime import datetime as _datetime

        from helmlog.audio import AudioSession as _AudioSession

        db = self._read_conn()
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

    async def _allocate_slug(
        self,
        base: str,
        *,
        exclude_race_id: int | None = None,
    ) -> str:
        """Return an unused slug derived from *base*.

        Collides against both live ``races.slug`` and non-expired
        ``race_slug_history`` entries (same race_id is treated as free so
        this race's own retired slugs can be reused). When the base itself
        is free we return it unchanged; otherwise we append ``-2``, ``-3``, …

        If *exclude_race_id* matches a row currently holding ``base``, that
        row is ignored so a no-op slug check on the same race doesn't force
        an unnecessary suffix.
        """
        db = self._conn()
        n = 1
        while True:
            candidate = base if n == 1 else f"{base}-{n}"
            live_cur = await db.execute(
                "SELECT id FROM races WHERE slug = ? AND (? IS NULL OR id != ?)",
                (candidate, exclude_race_id, exclude_race_id),
            )
            if await live_cur.fetchone() is not None:
                n += 1
                continue
            hist_cur = await db.execute(
                "SELECT race_id FROM race_slug_history WHERE slug = ?"
                " AND (? IS NULL OR race_id != ?)",
                (candidate, exclude_race_id, exclude_race_id),
            )
            if await hist_cur.fetchone() is not None:
                n += 1
                continue
            return candidate

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
        from helmlog.races import slugify

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

        # Insert with NULL slug first; we compute and assign the final slug
        # after the insert so the empty-name fallback can use the row id.
        cur = await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc, session_type, slug)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
            (name, event, race_num, date_str, start_utc.isoformat(), session_type),
        )
        assert cur.lastrowid is not None
        base = slugify(name) or f"race-{cur.lastrowid}"
        slug = await self._allocate_slug(base, exclude_race_id=cur.lastrowid)
        await db.execute("UPDATE races SET slug = ? WHERE id = ?", (slug, cur.lastrowid))
        await db.commit()
        await self._invalidate_race_cache(cur.lastrowid)
        logger.info("Race started: {} (id={}) type={}", name, cur.lastrowid, session_type)
        self._session_active = True
        self._active_race_id = cur.lastrowid
        return _Race(
            id=cur.lastrowid,
            name=name,
            event=event,
            race_num=race_num,
            date=date_str,
            start_utc=start_utc,
            end_utc=None,
            session_type=session_type,
            slug=slug,
        )

    async def end_race(self, race_id: int, end_utc: datetime) -> None:
        """Set end_utc on the given race row."""
        db = self._conn()
        await db.execute(
            "UPDATE races SET end_utc = ? WHERE id = ?",
            (end_utc.isoformat(), race_id),
        )
        await db.commit()
        await self._invalidate_race_cache(race_id)
        self._session_active = False
        if self._active_race_id == race_id:
            self._active_race_id = None
        logger.info("Race {} ended at {}", race_id, end_utc.isoformat())

    async def set_race_start_utc(self, race_id: int, start_utc: datetime) -> None:
        """Update the start_utc of a race (#644 — gun anchors session start).

        Used when the race-start FSM transitions to ``started`` while a
        race row is already in progress. Anchors the session start time
        to the actual start gun rather than whenever the user clicked
        "Start race" in the UI.
        """
        db = self._conn()
        await db.execute(
            "UPDATE races SET start_utc = ? WHERE id = ?",
            (start_utc.isoformat(), race_id),
        )
        await db.commit()
        await self._invalidate_race_cache(race_id)
        logger.info("Race {} start_utc anchored to gun at {}", race_id, start_utc.isoformat())

    async def rename_race(
        self,
        race_id: int,
        *,
        new_name: str | None = None,
        new_event: str | None = None,
        new_race_num: int | None = None,
    ) -> tuple[Race, str | None]:
        """Rename a race and regenerate its slug (#449).

        At least one of *new_name*, *new_event*, *new_race_num* must be set.
        When *new_name* is ``None`` but event/race_num change, the name is
        regenerated via ``build_race_name``. Returns ``(updated_race,
        retired_slug_or_None)`` — the retired slug is ``None`` when the
        rename was a no-op or didn't change the slug.

        Raises:
            LookupError: if the race id doesn't exist.
            ValueError: with message ``"name_taken"`` if the resulting name
                collides with another race's ``name``.
        """
        from datetime import UTC as _UTC
        from datetime import date as _date
        from datetime import datetime as _datetime

        from helmlog.races import build_race_name, slugify

        current = await self.get_race(race_id)
        if current is None:
            raise LookupError(f"race {race_id} not found")

        target_event = new_event if new_event is not None else current.event
        target_race_num = new_race_num if new_race_num is not None else current.race_num
        if new_name is not None:
            target_name = new_name.strip()
            if not target_name:
                raise ValueError("name_blank")
        elif new_event is not None or new_race_num is not None:
            d = _date.fromisoformat(current.date)
            target_name = build_race_name(target_event, d, target_race_num, current.session_type)
        else:
            target_name = current.name

        no_op = (
            target_name == current.name
            and target_event == current.event
            and target_race_num == current.race_num
        )
        if no_op:
            return current, None

        db = self._conn()

        # Name collision on another race?
        collide_cur = await db.execute(
            "SELECT id FROM races WHERE name = ? AND id != ?",
            (target_name, race_id),
        )
        if await collide_cur.fetchone() is not None:
            raise ValueError("name_taken")

        base = slugify(target_name) or f"race-{race_id}"
        new_slug = await self._allocate_slug(base, exclude_race_id=race_id)
        old_slug = current.slug
        now = _datetime.now(_UTC).isoformat()

        slug_changed = new_slug != old_slug

        await db.execute(
            "UPDATE races"
            " SET name = ?, event = ?, race_num = ?, slug = ?, renamed_at = ?"
            " WHERE id = ?",
            (target_name, target_event, target_race_num, new_slug, now, race_id),
        )

        retired: str | None = None
        if slug_changed and old_slug:
            # If the new slug matches a retired entry this race previously
            # owned (rename-churn back to a prior name), clean up that entry.
            await db.execute(
                "DELETE FROM race_slug_history WHERE race_id = ? AND slug = ?",
                (race_id, new_slug),
            )
            await db.execute(
                "INSERT OR REPLACE INTO race_slug_history (slug, race_id, retired_at)"
                " VALUES (?, ?, ?)",
                (old_slug, race_id, now),
            )
            retired = old_slug

        await db.commit()
        await self._invalidate_race_cache(race_id)
        logger.info(
            "Race {} renamed: {!r} → {!r} (slug {!r} → {!r})",
            race_id,
            current.name,
            target_name,
            old_slug,
            new_slug,
        )
        updated = await self.get_race(race_id)
        assert updated is not None
        return updated, retired

    async def purge_expired_slug_history(self, retention_days: int) -> int:
        """Delete ``race_slug_history`` rows older than *retention_days* (#449).

        Returns the number of rows deleted. Callers typically invoke this
        from a periodic maintenance task; the slug resolution logic also
        treats rows older than the retention window as 404 at read time so
        purging is a housekeeping optimisation rather than a correctness
        requirement.
        """
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        from datetime import timedelta as _timedelta

        cutoff = (_datetime.now(_UTC) - _timedelta(days=retention_days)).isoformat()
        db = self._conn()
        cur = await db.execute(
            "DELETE FROM race_slug_history WHERE retired_at < ?",
            (cutoff,),
        )
        await db.commit()
        return cur.rowcount or 0

    # -- Scheduled starts (#345) ------------------------------------------

    async def schedule_start(
        self,
        scheduled_start_utc: datetime,
        event: str,
        session_type: str = "race",
    ) -> int:
        """Set (or replace) the scheduled start. Returns the row id."""
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        db = self._conn()
        await db.execute("DELETE FROM scheduled_starts")
        cur = await db.execute(
            "INSERT INTO scheduled_starts (scheduled_start_utc, event, session_type, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                scheduled_start_utc.isoformat(),
                event,
                session_type,
                _datetime.now(_UTC).isoformat(),
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info("Scheduled start set for {} ({})", scheduled_start_utc.isoformat(), event)
        return cur.lastrowid

    async def get_scheduled_start(self) -> dict[str, str] | None:
        """Return the pending scheduled start row, or None."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, scheduled_start_utc, event, session_type, created_at"
            " FROM scheduled_starts LIMIT 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "scheduled_start_utc": row["scheduled_start_utc"],
            "event": row["event"],
            "session_type": row["session_type"],
            "created_at": row["created_at"],
        }

    async def cancel_scheduled_start(self) -> bool:
        """Delete the pending scheduled start. Returns True if a row was deleted."""
        db = self._conn()
        cur = await db.execute("DELETE FROM scheduled_starts")
        await db.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("Scheduled start cancelled")
        return deleted

    async def has_source_id(self, source: str, source_id: str) -> bool:
        """Check if a race with this source/source_id already exists (dedup)."""
        db = self._read_conn()
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

        from helmlog.races import slugify

        db = self._conn()
        now = _datetime.now(_UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc,"
            "  session_type, source, source_id, imported_at,"
            "  peer_fingerprint, peer_co_op_id, slug)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
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
        race_id = cur.lastrowid
        assert race_id is not None
        base = slugify(name) or f"race-{race_id}"
        slug = await self._allocate_slug(base, exclude_race_id=race_id)
        await db.execute("UPDATE races SET slug = ? WHERE id = ?", (slug, race_id))
        await db.commit()
        await self._invalidate_race_cache(race_id)
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

    @staticmethod
    def _row_to_race(row: Any) -> Race:  # noqa: ANN401
        from helmlog.races import Race as _Race

        # Normalize at the storage boundary so downstream arithmetic against
        # datetime.now(UTC) never hits a naive/aware mismatch (#532). Imported
        # results rows carry a date-only or empty start_utc — coerce those to
        # midnight UTC rather than raise, so the bad row is merely inert
        # instead of 500-ing /api/state.
        start = _parse_utc(row["start_utc"])
        if start is None:
            start = datetime(1970, 1, 1, tzinfo=UTC)
        return _Race(
            id=row["id"],
            name=row["name"],
            event=row["event"],
            race_num=row["race_num"],
            date=row["date"],
            start_utc=start,
            end_utc=_parse_utc(row["end_utc"]),
            session_type=row["session_type"],
            slug=row["slug"] or "",
            renamed_at=(datetime.fromisoformat(row["renamed_at"]) if row["renamed_at"] else None),
        )

    _RACE_COLS = (
        "id, name, event, race_num, date, start_utc, end_utc, session_type, slug, renamed_at"
    )

    async def get_race(self, race_id: int) -> Race | None:
        """Return the race with the given id, or None if not found."""
        db = self._read_conn()
        cur = await db.execute(
            f"SELECT {self._RACE_COLS} FROM races WHERE id = ?",
            (race_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_race(row)

    async def get_race_by_slug(self, slug: str) -> Race | None:
        """Return the race whose *current* slug matches, or None (#449)."""
        db = self._read_conn()
        cur = await db.execute(
            f"SELECT {self._RACE_COLS} FROM races WHERE slug = ?",
            (slug,),
        )
        row = await cur.fetchone()
        return self._row_to_race(row) if row else None

    async def ensure_race_slug(self, race_id: int) -> str | None:
        """Allocate and persist a slug for *race_id* if it doesn't have one yet (#449).

        Returns the slug (newly assigned or already present), or ``None`` if
        the race doesn't exist. Used as a lazy repair for rows that missed
        the v58 backfill — e.g. a race row whose backfill transaction
        crashed on a prior boot.
        """
        from helmlog.races import slugify

        db = self._conn()
        cur = await db.execute("SELECT id, name, slug FROM races WHERE id = ?", (race_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        if row["slug"]:
            return str(row["slug"])
        base = slugify(row["name"]) or f"race-{race_id}"
        slug = await self._allocate_slug(base, exclude_race_id=race_id)
        await db.execute("UPDATE races SET slug = ? WHERE id = ?", (slug, race_id))
        await db.commit()
        logger.info("Lazy slug allocation for race {}: {}", race_id, slug)
        return slug

    async def get_debrief_session(self, audio_session_id: int) -> dict[str, Any] | None:
        """Return a minimal debrief session record by audio_sessions id (#449).

        Debriefs live in ``audio_sessions`` with ``session_type='debrief'`` and
        have no slug. The session detail page renders them under
        ``/session/{audio_id}`` so links from the history list keep working.
        """
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, COALESCE(name, file_path) AS name, session_type, start_utc"
            " FROM audio_sessions WHERE id = ? AND session_type = 'debrief'",
            (audio_session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def lookup_retired_slug(self, slug: str) -> tuple[int, datetime] | None:
        """Return ``(race_id, retired_at)`` for a retired slug, or None (#449)."""
        from datetime import datetime as _datetime

        db = self._read_conn()
        cur = await db.execute(
            "SELECT race_id, retired_at FROM race_slug_history WHERE slug = ?",
            (slug,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return int(row["race_id"]), _datetime.fromisoformat(row["retired_at"])

    async def get_current_race(self) -> Race | None:
        """Return the most recent race with no end_utc, or None.

        Imported race-result rows set ``end_utc`` at insert time so they
        never appear here — see ``results/importer.py``. Earlier code
        (#532) tried to filter them via ``start_utc LIKE '%T%'``, but the
        placeholder ISO string matched that pattern, producing ghost "open"
        races on the home page for every imported row.
        """
        db = self._read_conn()
        cur = await db.execute(
            f"SELECT {self._RACE_COLS}"
            " FROM races WHERE end_utc IS NULL"
            " ORDER BY start_utc DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return self._row_to_race(row) if row else None

    async def get_latest_completed_race(self) -> Race | None:
        """Return the race with the most recent ``end_utc``, or None (#635).

        Used by the home page to surface the last finished race when no race
        is currently in progress. Excludes open races (``end_utc IS NULL``)
        so the home view never shows a "completed" race that is actually
        still running — ``get_current_race`` handles that case.
        """
        db = self._read_conn()
        cur = await db.execute(
            f"SELECT {self._RACE_COLS}"
            " FROM races WHERE end_utc IS NOT NULL"
            " ORDER BY end_utc DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return self._row_to_race(row) if row else None

    # ------------------------------------------------------------------
    # Race start state (#644) — singleton row
    # ------------------------------------------------------------------

    async def get_race_start_state(self) -> dict[str, Any] | None:
        """Return the singleton race-start state row as a dict, or None.

        Caller is responsible for hydrating it into a ``SequenceState`` via
        ``race_start.SequenceState`` — storage stays domain-agnostic.
        """
        db = self._read_conn()
        cur = await db.execute(
            "SELECT phase, kind, t0_utc, sync_offset_s, last_sync_at_utc,"
            "       started_at_utc, classes_json, updated_at"
            "  FROM race_start_state WHERE id = 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "phase": row[0],
            "kind": row[1],
            "t0_utc": row[2],
            "sync_offset_s": row[3],
            "last_sync_at_utc": row[4],
            "started_at_utc": row[5],
            "classes_json": row[6],
            "updated_at": row[7],
        }

    async def upsert_race_start_state(
        self,
        *,
        phase: str,
        kind: str | None,
        t0_utc: datetime | None,
        sync_offset_s: float,
        last_sync_at_utc: datetime | None,
        started_at_utc: datetime | None,
        classes_json: str,
        now_utc: datetime,
    ) -> None:
        """Upsert the singleton race-start state row."""
        db = self._conn()
        await db.execute(
            "INSERT INTO race_start_state"
            " (id, phase, kind, t0_utc, sync_offset_s, last_sync_at_utc,"
            "  started_at_utc, classes_json, updated_at)"
            " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   phase=excluded.phase,"
            "   kind=excluded.kind,"
            "   t0_utc=excluded.t0_utc,"
            "   sync_offset_s=excluded.sync_offset_s,"
            "   last_sync_at_utc=excluded.last_sync_at_utc,"
            "   started_at_utc=excluded.started_at_utc,"
            "   classes_json=excluded.classes_json,"
            "   updated_at=excluded.updated_at",
            (
                phase,
                kind,
                t0_utc.isoformat() if t0_utc else None,
                sync_offset_s,
                last_sync_at_utc.isoformat() if last_sync_at_utc else None,
                started_at_utc.isoformat() if started_at_utc else None,
                classes_json,
                now_utc.isoformat(),
            ),
        )
        await db.commit()

    async def clear_race_start_state(self) -> None:
        """Drop the singleton state row (back to idle)."""
        db = self._conn()
        await db.execute("DELETE FROM race_start_state WHERE id = 1")
        await db.commit()

    # ------------------------------------------------------------------
    # Start-line pings (#644) — append-only, race-scoped history
    # ------------------------------------------------------------------

    async def add_start_line_ping(
        self,
        *,
        race_id: int | None,
        end_kind: str,
        latitude_deg: float,
        longitude_deg: float,
        captured_at: datetime,
        captured_by: int | None,
    ) -> int:
        """Append a boat-end or pin-end ping. Returns the new row id."""
        if end_kind not in {"boat", "pin"}:
            raise ValueError(f"end_kind must be 'boat' or 'pin', got {end_kind!r}")
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO start_line_pings"
            " (race_id, end_kind, latitude_deg, longitude_deg, captured_at, captured_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                race_id,
                end_kind,
                latitude_deg,
                longitude_deg,
                captured_at.isoformat(),
                captured_by,
            ),
        )
        await db.commit()
        return int(cur.lastrowid or 0)

    async def get_latest_start_line(self, race_id: int | None) -> dict[str, Any] | None:
        """Return the latest boat-end and pin-end ping for *race_id*.

        Carry-over (#702): when *race_id* is given and a particular end has
        no ping for this race, fall back to the most recent ping of that
        end_kind from any prior race on the same UTC date. The carried-over
        end is tagged with ``{end}_end_race_id`` so callers can render a
        "from race N" hint when the line wasn't explicitly re-pinged.

        When *race_id* is ``None`` (pre-arm flow before a race row exists),
        returns the latest unscoped pings only — no cross-race fallback.
        """
        db = self._read_conn()
        if race_id is None:
            cur = await db.execute(
                "SELECT end_kind, latitude_deg, longitude_deg, captured_at, race_id"
                "  FROM start_line_pings"
                " WHERE race_id IS NULL"
                " AND id IN ("
                "   SELECT MAX(id) FROM start_line_pings"
                "   WHERE race_id IS NULL GROUP BY end_kind"
                " )"
            )
            rows = await cur.fetchall()
            if not rows:
                return None
            out: dict[str, Any] = {}
            for end_kind, lat, lon, ts, rid in rows:
                out[f"{end_kind}_end_lat"] = lat
                out[f"{end_kind}_end_lon"] = lon
                out[f"{end_kind}_end_captured_at"] = ts
                out[f"{end_kind}_end_race_id"] = rid
            return out

        # Race-scoped path with carry-over fallback per end.
        race_cur = await db.execute("SELECT date FROM races WHERE id = ?", (race_id,))
        race_row = await race_cur.fetchone()
        date_str = race_row["date"] if race_row else None

        out = {}
        for end_kind in ("boat", "pin"):
            # Prefer a ping for this exact race.
            cur = await db.execute(
                "SELECT latitude_deg, longitude_deg, captured_at, race_id"
                "  FROM start_line_pings"
                " WHERE race_id = ? AND end_kind = ?"
                " ORDER BY captured_at DESC LIMIT 1",
                (race_id, end_kind),
            )
            row = await cur.fetchone()

            # Carry-over: most recent ping of this end_kind on the same
            # UTC date, from a different race. Same-date keeps the helm
            # safe across consecutive starts but won't reach into older
            # sessions where the line is irrelevant.
            if row is None and date_str is not None:
                cur = await db.execute(
                    "SELECT slp.latitude_deg, slp.longitude_deg,"
                    "       slp.captured_at, slp.race_id"
                    "  FROM start_line_pings slp"
                    "  JOIN races r ON r.id = slp.race_id"
                    " WHERE slp.end_kind = ? AND r.date = ? AND r.id != ?"
                    " ORDER BY slp.captured_at DESC LIMIT 1",
                    (end_kind, date_str, race_id),
                )
                row = await cur.fetchone()

            if row is None:
                continue
            out[f"{end_kind}_end_lat"] = row["latitude_deg"]
            out[f"{end_kind}_end_lon"] = row["longitude_deg"]
            out[f"{end_kind}_end_captured_at"] = row["captured_at"]
            out[f"{end_kind}_end_race_id"] = row["race_id"]

        return out or None

    async def list_start_line_pings(self, race_id: int | None) -> list[dict[str, Any]]:
        """Return full ping history for *race_id*, ordered oldest → newest."""
        db = self._read_conn()
        if race_id is None:
            cur = await db.execute(
                "SELECT id, race_id, end_kind, latitude_deg, longitude_deg,"
                " captured_at, captured_by"
                "  FROM start_line_pings WHERE race_id IS NULL"
                " ORDER BY captured_at ASC"
            )
        else:
            cur = await db.execute(
                "SELECT id, race_id, end_kind, latitude_deg, longitude_deg,"
                " captured_at, captured_by"
                "  FROM start_line_pings WHERE race_id = ?"
                " ORDER BY captured_at ASC",
                (race_id,),
            )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "race_id": r[1],
                "end_kind": r[2],
                "latitude_deg": r[3],
                "longitude_deg": r[4],
                "captured_at": r[5],
                "captured_by": r[6],
            }
            for r in rows
        ]

    async def list_races_for_date(self, date_str: str) -> list[Race]:
        """Return all races for a UTC date string, ordered by start_utc ASC."""
        db = self._read_conn()
        cur = await db.execute(
            f"SELECT {self._RACE_COLS} FROM races WHERE date = ? ORDER BY start_utc ASC",
            (date_str,),
        )
        rows = await cur.fetchall()
        return [self._row_to_race(row) for row in rows]

    async def list_races_in_range(self, start_utc: datetime, end_utc: datetime) -> list[Race]:
        """Return all races whose time window overlaps ``[start_utc, end_utc]``."""
        db = self._read_conn()
        cur = await db.execute(
            f"SELECT {self._RACE_COLS}"
            " FROM races"
            " WHERE start_utc < ? AND (end_utc IS NULL OR end_utc > ?)"
            " ORDER BY start_utc ASC",
            (end_utc.isoformat(), start_utc.isoformat()),
        )
        rows = await cur.fetchall()
        return [self._row_to_race(row) for row in rows]

    async def count_sessions_for_date(self, date_str: str, session_type: str) -> int:
        """Return the count of sessions of the given type for a UTC date string."""
        db = self._read_conn()
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
        db = self._read_conn()

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
            race_where.append("(r.source IS NULL OR r.source IN ('live', 'synthesized'))")
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
                f" r.slug AS slug,"
                f" r.event AS event, r.race_num AS race_num, r.date AS date,"
                f" r.start_utc AS start_utc, r.end_utc AS end_utc,"
                f" (SELECT COUNT(*) > 0 FROM audio_sessions a"
                f"   WHERE a.race_id = r.id"
                f"   AND a.session_type IN ('race', 'practice')"
                f" ) AS has_audio,"
                f" (SELECT a.id FROM audio_sessions a"
                f"   WHERE a.race_id = r.id"
                f"   AND a.session_type IN ('race', 'practice')"
                f"   ORDER BY a.id LIMIT 1"
                f" ) AS audio_session_id,"
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
                f" (SELECT COUNT(*) > 0 FROM crew_defaults cd"
                f"   WHERE cd.race_id = r.id) AS has_crew,"
                f" (SELECT COUNT(*) > 0 FROM race_sails rs"
                f"   WHERE rs.race_id = r.id) AS has_sails,"
                f" (SELECT COUNT(*) > 0 FROM moments m"
                f"   WHERE m.session_id = r.id) AS has_notes,"
                f" r.shared_name AS shared_name,"
                f" r.match_group_id AS match_group_id,"
                f" r.match_confirmed AS match_confirmed"
                f" FROM races r"
                f" {where}"
            )
            params.extend(race_params)

        if include_debriefs:
            deb_where: list[str] = ["a.session_type = 'debrief'"]
            deb_params: list[Any] = []
            # #546: debriefs attached to a race are reachable from the race's
            # session page (audio + transcript surfaced there), so hide them
            # from the default history view. Explicit type='debrief' filter
            # still returns them along with any orphan debriefs.
            if session_type is None:
                deb_where.append("a.race_id IS NULL")
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
                f" NULL AS slug,"
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
                f" 0 AS has_results, 0 AS has_crew, 0 AS has_sails, 0 AS has_notes,"
                f" NULL AS shared_name, NULL AS match_group_id, 0 AS match_confirmed"
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
                session["crew"] = await self.resolve_crew(session["id"])
            elif session["type"] == "debrief" and session.get("parent_race_id"):
                session["crew"] = await self.resolve_crew(session["parent_race_id"])
            else:
                session["crew"] = []

        return (total, result)

    async def get_daily_event(self, date_str: str) -> str | None:
        """Look up a stored custom event name for the given UTC date."""
        db = self._read_conn()
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
        db = self._read_conn()
        cur = await db.execute("SELECT weekday, event_name FROM event_rules ORDER BY weekday")
        return [dict(row) for row in await cur.fetchall()]

    async def get_event_rule(self, weekday: int) -> str | None:
        """Return the event name for a weekday (0=Mon … 6=Sun), or None."""
        db = self._read_conn()
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
    # Crew positions & defaults (#305)
    # ------------------------------------------------------------------

    async def get_crew_positions(self) -> list[CrewPosition]:
        """Return configured crew positions ordered by display_order."""
        cur = await self._read_conn().execute(
            "SELECT id, name, display_order, created_at FROM crew_positions ORDER BY display_order"
        )
        rows: list[CrewPosition] = [dict(r) for r in await cur.fetchall()]  # type: ignore[misc]
        return rows

    async def set_crew_positions(self, positions: list[dict[str, Any]]) -> None:
        """Admin: replace crew positions list.

        Each entry must have ``name`` and ``display_order`` keys.
        Existing positions not in the new list are deleted (cascades to
        crew_defaults via FK).
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()

        # Upsert by name, delete missing
        new_names = {p["name"] for p in positions}
        cur = await db.execute("SELECT id, name FROM crew_positions")
        existing = {r["name"]: r["id"] for r in await cur.fetchall()}

        for name in existing:
            if name not in new_names:
                await db.execute("DELETE FROM crew_positions WHERE name = ?", (name,))

        for p in positions:
            if p["name"] in existing:
                await db.execute(
                    "UPDATE crew_positions SET display_order = ? WHERE id = ?",
                    (p["display_order"], existing[p["name"]]),
                )
            else:
                await db.execute(
                    "INSERT INTO crew_positions (name, display_order, created_at) VALUES (?, ?, ?)",
                    (p["name"], p["display_order"], now),
                )
        await db.commit()

    async def set_crew_defaults(self, race_id: int | None, crew: list[dict[str, Any]]) -> None:
        """Set boat-level (race_id=None) or race-level crew (full-replace).

        Each entry must have ``position_id``.  Optional keys: ``user_id``,
        ``attributed`` (default True), ``body_weight``, ``gear_weight``.

        Raises ``ValueError`` if the same user_id appears in multiple entries.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        # Validate no duplicate user_ids
        seen_user_ids: set[int] = set()
        for entry in crew:
            uid = entry.get("user_id")
            if uid is not None:
                if uid in seen_user_ids:
                    raise ValueError(f"Duplicate user_id {uid} in crew list")
                seen_user_ids.add(uid)

        db = self._conn()
        now = _datetime.now(UTC).isoformat()

        if race_id is None:
            await db.execute("DELETE FROM crew_defaults WHERE race_id IS NULL")
        else:
            await db.execute("DELETE FROM crew_defaults WHERE race_id = ?", (race_id,))

        for entry in crew:
            await db.execute(
                "INSERT INTO crew_defaults"
                " (race_id, position_id, user_id, attributed, body_weight,"
                "  gear_weight, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    race_id,
                    entry["position_id"],
                    entry.get("user_id"),
                    int(entry.get("attributed", True)),
                    entry.get("body_weight"),
                    entry.get("gear_weight"),
                    now,
                ),
            )
        await db.commit()
        logger.debug("Crew defaults set for race_id={}: {} entries", race_id, len(crew))

    async def get_crew_defaults(self, race_id: int | None) -> list[CrewDefault]:
        """Return crew entries for a specific level (boat or race).

        Results include joined position name and user name/email.
        """
        db = self._read_conn()
        params: tuple[()] | tuple[int]
        if race_id is None:
            where, params = "cd.race_id IS NULL", ()
        else:
            where, params = "cd.race_id = ?", (race_id,)
        cur = await db.execute(
            "SELECT cd.id, cd.race_id, cd.position_id, cd.user_id,"
            "       cd.attributed, cd.body_weight, cd.gear_weight, cd.created_at,"
            "       cp.name AS position, cp.display_order,"
            "       COALESCE(u.name, u.email) AS user_name, u.email AS user_email"
            " FROM crew_defaults cd"
            " JOIN crew_positions cp ON cp.id = cd.position_id"
            " LEFT JOIN users u ON u.id = cd.user_id"
            f" WHERE {where}"
            " ORDER BY cp.display_order",
            params,
        )
        rows: list[CrewDefault] = [dict(r) for r in await cur.fetchall()]  # type: ignore[misc]
        return rows

    async def resolve_crew(self, race_id: int) -> list[ResolvedCrew]:
        """Merge boat-level defaults with race-level overrides.

        For each configured position, the race-level entry wins if present;
        otherwise the boat-level default is used.  Returns position info,
        user info, attributed flag, weights, and override tracking.
        """
        boat_entries = await self.get_crew_defaults(None)
        race_entries = await self.get_crew_defaults(race_id)

        boat_by_pos: dict[int, CrewDefault] = {e["position_id"]: e for e in boat_entries}
        race_by_pos: dict[int, CrewDefault] = {e["position_id"]: e for e in race_entries}

        positions = await self.get_crew_positions()
        result: list[ResolvedCrew] = []

        for pos in positions:
            pid = pos["id"]
            race_row = race_by_pos.get(pid)
            boat_row = boat_by_pos.get(pid)

            if race_row:
                entry: ResolvedCrew = {
                    **race_row,
                    "source": "race",
                    "supersedes_user_id": (
                        boat_row.get("user_id")
                        if boat_row and boat_row.get("user_id") != race_row.get("user_id")
                        else None
                    ),
                    "supersedes_user_name": (
                        boat_row.get("user_name")
                        if boat_row and boat_row.get("user_id") != race_row.get("user_id")
                        else None
                    ),
                }
                result.append(entry)
            elif boat_row:
                entry = {
                    **boat_row,
                    "source": "boat",
                    "supersedes_user_id": None,
                    "supersedes_user_name": None,
                }
                result.append(entry)

        return result

    async def create_placeholder_user(self, name: str) -> int:
        """Create a user with no real credentials for non-system crew.

        Returns the user id (existing or newly created).
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        email = f"placeholder+{name.lower().replace(' ', '.')}@helmlog.local"
        db = self._conn()

        # Check if placeholder already exists
        cur = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = await cur.fetchone()
        if row:
            return int(row["id"])

        cur = await db.execute(
            "INSERT INTO users (email, name, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (email, name, now),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_race_crew(self, race_id: int) -> list[dict[str, str]]:
        """Return crew for *race_id* in the legacy format for backward compat.

        Returns list of ``{"position": str, "sailor": str}`` dicts.
        """
        resolved = await self.resolve_crew(race_id)
        result: list[dict[str, str]] = []
        for entry in resolved:
            if entry.get("user_id") and entry.get("attributed"):
                result.append(
                    {
                        "position": entry["position"],
                        "sailor": entry.get("user_name") or entry.get("user_email") or "—",
                    }
                )
        return result

    async def update_user_weight(self, user_id: int, weight_lbs: float | None) -> None:
        """Update a user's body weight."""
        db = self._conn()
        await db.execute("UPDATE users SET weight_lbs = ? WHERE id = ?", (weight_lbs, user_id))
        await db.commit()

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
        """Return results for *race_id* ordered by place.

        If an imported race is linked to this session via
        ``local_session_id``, its results are returned instead — they are
        typically more complete (full fleet from the scoring system vs
        manually entered finishes).
        """
        db = self._read_conn()
        imported_cur = await db.execute(
            "SELECT id FROM races WHERE local_session_id = ? AND source IS NOT NULL",
            (race_id,),
        )
        imported = await imported_cur.fetchone()
        effective_id = imported["id"] if imported else race_id

        cur = await db.execute(
            "SELECT rr.id, rr.race_id, rr.place, rr.boat_id,"
            " b.sail_number, b.name AS boat_name, b.class AS boat_class,"
            " rr.finish_time, rr.dnf, rr.dns, rr.notes, rr.created_at,"
            " rr.points, rr.status_code, rr.elapsed_seconds, rr.corrected_seconds"
            " FROM race_results rr"
            " JOIN boats b ON b.id = rr.boat_id"
            " WHERE rr.race_id = ?"
            " ORDER BY rr.place ASC",
            (effective_id,),
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
                "points": row["points"],
                "status_code": row["status_code"],
                "elapsed_seconds": row["elapsed_seconds"],
                "corrected_seconds": row["corrected_seconds"],
                "imported": effective_id != race_id,
            }
            for row in rows
        ]

    async def set_race_local_session(self, race_id: int, local_session_id: int | None) -> None:
        """Set or clear the ``local_session_id`` link on a race row."""
        db = self._conn()
        await db.execute(
            "UPDATE races SET local_session_id = ? WHERE id = ?",
            (local_session_id, race_id),
        )
        await db.commit()
        logger.debug("Race {} local_session_id set to {}", race_id, local_session_id)

    async def list_local_session_candidates(
        self,
        date_iso: str,
    ) -> list[dict[str, Any]]:
        """Return non-imported races (local sessions) within ±1 day of *date_iso*.

        The caller filters by venue local date — this method only narrows
        the row set down to a small candidate window. Imported races
        (``source IS NOT NULL``) are excluded.
        """
        from datetime import date as _date
        from datetime import timedelta as _td

        try:
            d = _date.fromisoformat(date_iso)
        except ValueError:
            return []
        lo = (d - _td(days=1)).isoformat()
        hi = (d + _td(days=1)).isoformat()

        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, start_utc, name FROM races"
            " WHERE (source IS NULL OR source = 'live')"
            " AND date >= ? AND date <= ?"
            " ORDER BY start_utc",
            (lo, hi),
        )
        rows = await cur.fetchall()
        return [
            {"id": row["id"], "start_utc": row["start_utc"], "name": row["name"]} for row in rows
        ]

    async def delete_race_result(self, result_id: int) -> None:
        """Delete a single race result row by id."""
        db = self._conn()
        await db.execute("DELETE FROM race_results WHERE id = ?", (result_id,))
        await db.commit()
        logger.debug("Race result {} deleted", result_id)

    # ------------------------------------------------------------------
    # Session settings (was note_type='settings' — migrated by v80)
    # ------------------------------------------------------------------

    async def list_settings_keys(self) -> list[str]:
        """Return all distinct keys used in saved session_settings, sorted.

        Parses the JSON body of every saved config snapshot and collects the
        union of keys. Used to populate the typeahead datalist on the
        settings entry form.
        """
        cur = await self._read_conn().execute(
            "SELECT body FROM session_settings WHERE body IS NOT NULL"
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
        """Add or replace a YouTube video link on a race.

        Idempotent on ``(race_id, video_id)``: if a link already exists for
        the same video on the same race, the mutable fields (label,
        sync_utc, sync_offset_s, duration_s, title, youtube_url) are
        updated and the existing row id is returned. Prevents duplicate
        links from double-submits or re-paste-to-edit attempts.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now_str = _datetime.now(UTC).isoformat()
        existing_cur = await db.execute(
            "SELECT id FROM race_videos WHERE race_id = ? AND video_id = ?",
            (race_id, video_id),
        )
        existing_row = await existing_cur.fetchone()
        if existing_row is not None:
            row_id = int(existing_row["id"])
            await db.execute(
                "UPDATE race_videos SET"
                " youtube_url = ?, title = ?, label = ?,"
                " sync_utc = ?, sync_offset_s = ?, duration_s = ?"
                " WHERE id = ?",
                (
                    youtube_url,
                    title,
                    label,
                    sync_utc.isoformat(),
                    sync_offset_s,
                    duration_s,
                    row_id,
                ),
            )
            await db.execute("DELETE FROM maneuver_cache WHERE session_id = ?", (race_id,))
            await db.commit()
            logger.info(
                "Race video re-linked (idempotent): id={} race_id={} video_id={}",
                row_id,
                race_id,
                video_id,
            )
            return row_id
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
        await db.execute("DELETE FROM maneuver_cache WHERE session_id = ?", (race_id,))
        await db.commit()
        assert cur.lastrowid is not None
        logger.info(
            "Race video added: id={} race_id={} video_id={}", cur.lastrowid, race_id, video_id
        )  # noqa: E501
        return cur.lastrowid

    async def list_race_videos(self, race_id: int) -> list[dict[str, Any]]:
        """Return all videos linked to a race, ordered by created_at ASC."""
        db = self._read_conn()
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
        race_cur = await db.execute("SELECT race_id FROM race_videos WHERE id = ?", (video_row_id,))
        race_row = await race_cur.fetchone()
        cur = await db.execute(
            f"UPDATE race_videos SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
            params,
        )
        if race_row is not None:
            await db.execute(
                "DELETE FROM maneuver_cache WHERE session_id = ?",
                (race_row["race_id"],),
            )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_race_video(self, video_row_id: int) -> bool:
        """Delete a race video by id.  Returns True if deleted."""
        db = self._conn()
        race_cur = await db.execute("SELECT race_id FROM race_videos WHERE id = ?", (video_row_id,))
        race_row = await race_cur.fetchone()
        cur = await db.execute("DELETE FROM race_videos WHERE id = ?", (video_row_id,))
        if race_row is not None:
            await db.execute(
                "DELETE FROM maneuver_cache WHERE session_id = ?",
                (race_row["race_id"],),
            )
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
        db = self._read_conn()
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
        db = self._read_conn()
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
        db = self._read_conn()
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

        Also applies CAMERA_WIFI_SSID and CAMERA_WIFI_PASSWORD from env if set.
        Returns the number of cameras seeded.
        """
        import os as _os

        db = self._conn()
        cur = await db.execute("SELECT COUNT(*) FROM cameras")
        row = await cur.fetchone()
        assert row is not None
        if row[0] > 0:
            return 0

        from helmlog.cameras import parse_cameras_config

        wifi_ssid = _os.environ.get("CAMERA_WIFI_SSID")
        wifi_password = _os.environ.get("CAMERA_WIFI_PASSWORD")

        cameras = parse_cameras_config(cameras_str)
        count = 0
        for cam in cameras:
            await db.execute(
                "INSERT OR IGNORE INTO cameras (name, ip, model, wifi_ssid, wifi_password)"
                " VALUES (?, ?, ?, ?, ?)",
                (cam.name, cam.ip, cam.model, wifi_ssid, wifi_password),
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
        point_of_sail: str | None = None,
    ) -> int:
        """Insert a sail into the inventory and return its id.

        If *point_of_sail* is ``None`` a sensible default is chosen based on
        *sail_type*: ``jib`` → ``'upwind'``, ``spinnaker`` → ``'downwind'``,
        everything else → ``'both'``.

        Raises ``ValueError`` on duplicate (type, name).
        """
        if point_of_sail is None:
            point_of_sail = {"jib": "upwind", "spinnaker": "downwind"}.get(sail_type, "both")
        db = self._conn()
        try:
            cur = await db.execute(
                "INSERT INTO sails (type, name, notes, point_of_sail) VALUES (?, ?, ?, ?)",
                (sail_type, name.strip(), notes, point_of_sail),
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
        db = self._read_conn()
        where = "" if include_inactive else "WHERE active = 1"
        cur = await db.execute(
            "SELECT id, type, name, notes, active, point_of_sail"
            f" FROM sails {where} ORDER BY type, name"
        )
        rows = await cur.fetchall()
        return [
            {
                "id": row["id"],
                "type": row["type"],
                "name": row["name"],
                "notes": row["notes"],
                "active": bool(row["active"]),
                "point_of_sail": row["point_of_sail"],
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
        point_of_sail: str | None = None,
    ) -> bool:
        """Update sail fields.  Returns True if the sail was found and updated."""
        if name is None and notes is None and active is None and point_of_sail is None:
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
        if point_of_sail is not None:
            parts.append("point_of_sail = ?")
            params.append(point_of_sail)
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

        Delegates to :meth:`insert_sail_change` with ``ts = now()``,
        which also syncs the legacy ``race_sails`` table.
        """
        from datetime import UTC as _utc
        from datetime import datetime as _dt

        ts = _dt.now(_utc).isoformat()
        await self.insert_sail_change(
            race_id,
            ts,
            main_id=main_id,
            jib_id=jib_id,
            spinnaker_id=spinnaker_id,
        )

    async def get_race_sails(self, race_id: int) -> dict[str, Any]:
        """Return the sail selection for *race_id*.

        Delegates to :meth:`get_current_sails` which reads from
        ``sail_changes`` instead of the legacy ``race_sails`` table.
        """
        return await self.get_current_sails(race_id)

    # ------------------------------------------------------------------
    # Sail changes (timestamped)
    # ------------------------------------------------------------------

    async def insert_sail_change(
        self,
        race_id: int,
        ts: str,
        *,
        main_id: int | None,
        jib_id: int | None,
        spinnaker_id: int | None,
    ) -> int:
        """Insert a timestamped sail-change snapshot.

        Also syncs the legacy ``race_sails`` table for backward compat.
        Returns the new ``sail_changes`` row id.
        """
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO sail_changes (race_id, ts, main_id, jib_id, spinnaker_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (race_id, ts, main_id, jib_id, spinnaker_id),
        )
        change_id = cur.lastrowid or 0

        # Sync legacy race_sails table
        await db.execute("DELETE FROM race_sails WHERE race_id = ?", (race_id,))
        for sail_id in (main_id, jib_id, spinnaker_id):
            if sail_id is not None:
                await db.execute(
                    "INSERT OR IGNORE INTO race_sails (race_id, sail_id) VALUES (?, ?)",
                    (race_id, sail_id),
                )
        await db.commit()
        logger.debug("Sail change recorded for race {} at {}", race_id, ts)
        return change_id

    async def get_current_sails(self, race_id: int) -> dict[str, Any]:
        """Return the latest sail selection for *race_id* from ``sail_changes``.

        Returns a dict with keys ``main``, ``jib``, ``spinnaker``, each
        containing the full sail row dict or ``None`` if not set.
        Falls back to empty if no rows exist.
        """
        db = self._read_conn()
        cur = await db.execute(
            "SELECT sc.main_id, sc.jib_id, sc.spinnaker_id"
            " FROM sail_changes sc"
            " WHERE sc.race_id = ?"
            " ORDER BY sc.ts DESC LIMIT 1",
            (race_id,),
        )
        row = await cur.fetchone()
        result: dict[str, dict[str, Any] | None] = dict.fromkeys(_SAIL_TYPES)
        if row is None:
            return result

        for sail_type, col in (
            ("main", "main_id"),
            ("jib", "jib_id"),
            ("spinnaker", "spinnaker_id"),
        ):
            sail_id = row[col]
            if sail_id is None:
                continue
            scur = await db.execute(
                "SELECT id, type, name, notes, active, point_of_sail FROM sails WHERE id = ?",
                (sail_id,),
            )
            srow = await scur.fetchone()
            if srow is not None:
                result[sail_type] = {
                    "id": srow["id"],
                    "type": srow["type"],
                    "name": srow["name"],
                    "notes": srow["notes"],
                    "active": bool(srow["active"]),
                    "point_of_sail": srow["point_of_sail"],
                }
        return result

    async def get_sail_change_history(self, race_id: int) -> list[dict[str, Any]]:
        """Return all sail changes for *race_id* ordered by ts ASC, with resolved sail names."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT sc.id, sc.ts, sc.main_id, sc.jib_id, sc.spinnaker_id"
            " FROM sail_changes sc"
            " WHERE sc.race_id = ?"
            " ORDER BY sc.ts ASC",
            (race_id,),
        )
        rows = await cur.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {"id": row["id"], "ts": row["ts"]}
            for sail_type, col in (
                ("main", "main_id"),
                ("jib", "jib_id"),
                ("spinnaker", "spinnaker_id"),
            ):
                sail_id = row[col]
                if sail_id is None:
                    entry[sail_type] = None
                    continue
                scur = await db.execute(
                    "SELECT id, type, name FROM sails WHERE id = ?",
                    (sail_id,),
                )
                srow = await scur.fetchone()
                entry[sail_type] = {"id": srow["id"], "name": srow["name"]} if srow else None
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Sail defaults (boat-level)
    # ------------------------------------------------------------------

    async def get_sail_defaults(self) -> dict[str, Any]:
        """Return the boat-level default sail selection.

        Returns a dict with keys ``main``, ``jib``, ``spinnaker``, each
        containing the full sail row dict or ``None`` if not set.
        """
        db = self._read_conn()
        cur = await db.execute(
            "SELECT s.id, s.type, s.name, s.notes, s.active, s.point_of_sail"
            " FROM sail_defaults sd"
            " JOIN sails s ON s.id = sd.sail_id",
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
                    "point_of_sail": row["point_of_sail"],
                }
        return result

    async def set_sail_defaults(
        self,
        *,
        main_id: int | None,
        jib_id: int | None,
        spinnaker_id: int | None,
    ) -> None:
        """Replace the boat-level default sail selection.

        Existing defaults are deleted and the provided non-None sail ids
        are re-inserted.  Validates that each sail exists and matches the
        expected type.
        """
        db = self._conn()
        slot_map: dict[str, int | None] = {
            "main": main_id,
            "jib": jib_id,
            "spinnaker": spinnaker_id,
        }
        for slot_type, sail_id in slot_map.items():
            if sail_id is None:
                continue
            cur = await db.execute("SELECT type FROM sails WHERE id = ?", (sail_id,))
            row = await cur.fetchone()
            if row is None:
                msg = f"Sail id={sail_id} not found"
                raise ValueError(msg)
            if row["type"] != slot_type:
                msg = (
                    f"Sail id={sail_id} has type {row['type']!r},"
                    f" expected {slot_type!r} for the {slot_type} slot"
                )
                raise ValueError(msg)
        await db.execute("DELETE FROM sail_defaults")
        for sail_id in (main_id, jib_id, spinnaker_id):
            if sail_id is not None:
                await db.execute(
                    "INSERT OR IGNORE INTO sail_defaults (sail_id) VALUES (?)",
                    (sail_id,),
                )
        await db.commit()
        logger.debug("Sail defaults updated")

    # ------------------------------------------------------------------
    # Sail stats (for sail management page #307)
    # ------------------------------------------------------------------

    async def get_sail_stats(self) -> list[dict[str, Any]]:
        """Return all sails (including inactive) with accumulated maneuver counts.

        Each dict contains the sail fields plus ``total_tacks``,
        ``total_gybes``, and ``total_sessions``.  Tack/gybe counts are
        attributed using ``sail_changes`` — the active sail at each
        maneuver's timestamp is determined by the latest ``sail_changes``
        row with ``ts <= maneuver.ts``.
        """
        db = self._read_conn()
        sails = await self.list_sails(include_inactive=True)
        for sail in sails:
            sid = sail["id"]
            pos = sail["point_of_sail"]
            # Count sessions this sail was used in (via sail_changes)
            cur = await db.execute(
                "SELECT COUNT(DISTINCT race_id) AS cnt FROM sail_changes"
                " WHERE main_id = ? OR jib_id = ? OR spinnaker_id = ?",
                (sid, sid, sid),
            )
            cnt_row = await cur.fetchone()
            sail["total_sessions"] = cnt_row["cnt"] if cnt_row else 0

            # Maneuver counts filtered by point_of_sail, attributed via sail_changes
            if pos == "upwind":
                type_filter = "('tack')"
            elif pos == "downwind":
                type_filter = "('gybe')"
            else:
                type_filter = "('tack', 'gybe')"

            cur = await db.execute(
                "SELECT m.type, COUNT(*) AS cnt"
                " FROM maneuvers m"
                " JOIN sail_changes sc ON sc.race_id = m.session_id"
                "   AND sc.ts = ("
                "     SELECT MAX(sc2.ts) FROM sail_changes sc2"
                "     WHERE sc2.race_id = m.session_id AND sc2.ts <= m.ts"
                "   )"
                f" WHERE (sc.main_id = ? OR sc.jib_id = ? OR sc.spinnaker_id = ?)"
                f"   AND m.type IN {type_filter}"
                " GROUP BY m.type",
                (sid, sid, sid),
            )
            counts = {row["type"]: row["cnt"] for row in await cur.fetchall()}
            sail["total_tacks"] = counts.get("tack", 0)
            sail["total_gybes"] = counts.get("gybe", 0)
        return sails

    async def get_sail_session_history(self, sail_id: int) -> list[dict[str, Any]]:
        """Return sessions that used *sail_id*, newest first.

        Each entry includes the session info, per-session tack/gybe
        counts, and wind summary (avg TWS, min/max TWS).
        Uses ``sail_changes`` instead of legacy ``race_sails``.
        """
        db = self._read_conn()
        # Get the sail's point_of_sail for filtering
        cur = await db.execute("SELECT point_of_sail FROM sails WHERE id = ?", (sail_id,))
        sail_row = await cur.fetchone()
        if sail_row is None:
            return []
        pos = sail_row["point_of_sail"]

        # Sessions this sail was used in (via sail_changes)
        cur = await db.execute(
            "SELECT DISTINCT r.id, r.name, r.event, r.date, r.start_utc, r.end_utc"
            " FROM sail_changes sc"
            " JOIN races r ON r.id = sc.race_id"
            " WHERE sc.main_id = ? OR sc.jib_id = ? OR sc.spinnaker_id = ?"
            " ORDER BY r.start_utc DESC",
            (sail_id, sail_id, sail_id),
        )
        sessions = [dict(row) for row in await cur.fetchall()]

        for sess in sessions:
            sid = sess["id"]
            # Maneuver counts for this session
            cur = await db.execute(
                "SELECT type, COUNT(*) AS cnt FROM maneuvers"
                " WHERE session_id = ? AND type IN ('tack', 'gybe')"
                " GROUP BY type",
                (sid,),
            )
            counts = {row["type"]: row["cnt"] for row in await cur.fetchall()}
            sess["tacks"] = counts.get("tack", 0) if pos in ("upwind", "both") else None
            sess["gybes"] = counts.get("gybe", 0) if pos in ("downwind", "both") else None

            # Wind summary (true wind only: reference IN (0, 4))
            start = sess.get("start_utc")
            end = sess.get("end_utc")
            if start and end:
                cur = await db.execute(
                    "SELECT AVG(wind_speed_kts) AS avg_tws,"
                    " MIN(wind_speed_kts) AS min_tws,"
                    " MAX(wind_speed_kts) AS max_tws"
                    " FROM winds"
                    " WHERE reference IN (0, 4) AND ts >= ? AND ts <= ?",
                    (start, end),
                )
                wind = await cur.fetchone()
                if wind and wind["avg_tws"] is not None:
                    sess["avg_tws"] = round(wind["avg_tws"], 1)
                    sess["min_tws"] = round(wind["min_tws"], 1)
                    sess["max_tws"] = round(wind["max_tws"], 1)
                else:
                    sess["avg_tws"] = sess["min_tws"] = sess["max_tws"] = None
            else:
                sess["avg_tws"] = sess["min_tws"] = sess["max_tws"] = None
        return sessions

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

    async def delete_transcript(self, audio_session_id: int) -> bool:
        """Delete the transcript (and linked extraction_runs) for an audio session.

        Returns True if found and deleted.
        """
        db = self._conn()
        # Delete extraction_runs first (no ON DELETE CASCADE on FK)
        await db.execute(
            "DELETE FROM extraction_runs WHERE transcript_id IN"
            " (SELECT id FROM transcripts WHERE audio_session_id = ?)",
            (audio_session_id,),
        )
        cur = await db.execute(
            "DELETE FROM transcripts WHERE audio_session_id = ?", (audio_session_id,)
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def get_transcript(self, audio_session_id: int) -> dict[str, Any] | None:
        """Return the transcript row for *audio_session_id*, or None if not found."""
        cur = await self._read_conn().execute(
            "SELECT id, audio_session_id, status, text, error_msg, model,"
            " created_utc, updated_utc, segments_json, speaker_anon_map, speaker_map"
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
        cur = await self._read_conn().execute(
            "SELECT tws_bin, twa_bin, mean_bsp, p90_bsp, session_count, sample_count"
            " FROM polar_baseline WHERE tws_bin = ? AND twa_bin = ?",
            (tws_bin, twa_bin),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all_polar_points(self) -> dict[tuple[int, int], dict[str, Any]]:
        """Return every polar_baseline row keyed by (tws_bin, twa_bin).

        Used by ``grade_session_segments`` to batch-load the whole baseline
        before dispatching CPU-bound segmentation to a worker thread — avoids
        one await-per-segment on the event loop for long sessions.
        """
        cur = await self._read_conn().execute(
            "SELECT tws_bin, twa_bin, mean_bsp, p90_bsp, session_count, sample_count"
            " FROM polar_baseline"
        )
        rows = await cur.fetchall()
        return {(int(r["tws_bin"]), int(r["twa_bin"])): dict(r) for r in rows}

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
    # Polar segment grades (#469)
    # ------------------------------------------------------------------

    async def get_polar_segment_grades(
        self, session_id: int, polar_source: str, baseline_version: int
    ) -> list[dict[str, Any]] | None:
        """Return cached graded segments, or None if cache is missing/stale."""
        cur = await self._read_conn().execute(
            "SELECT segment_index, t_start, t_end, lat, lon, tws_kts, twa_deg,"
            " bsp_kts, target_bsp_kts, pct_target, delta_kts, grade, baseline_version"
            " FROM polar_segment_grades"
            " WHERE session_id = ? AND polar_source = ?"
            " ORDER BY segment_index",
            (session_id, polar_source),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return None
        if any(int(r["baseline_version"]) != baseline_version for r in rows):
            return None
        return rows

    async def upsert_polar_segment_grades(
        self,
        session_id: int,
        polar_source: str,
        rows: list[dict[str, Any]],
        baseline_version: int,
    ) -> None:
        """Replace cached segments for *(session_id, polar_source)* in one txn."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "DELETE FROM polar_segment_grades WHERE session_id = ? AND polar_source = ?",
            (session_id, polar_source),
        )
        for r in rows:
            await db.execute(
                "INSERT INTO polar_segment_grades"
                " (session_id, polar_source, segment_index, t_start, t_end, lat, lon,"
                "  tws_kts, twa_deg, bsp_kts, target_bsp_kts, pct_target, delta_kts,"
                "  grade, baseline_version, computed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    polar_source,
                    r["segment_index"],
                    r["t_start"],
                    r["t_end"],
                    r["lat"],
                    r["lon"],
                    r["tws_kts"],
                    r["twa_deg"],
                    r["bsp_kts"],
                    r["target_bsp_kts"],
                    r["pct_target"],
                    r["delta_kts"],
                    r["grade"],
                    baseline_version,
                    now,
                ),
            )
        await db.commit()

    # ------------------------------------------------------------------
    # Maneuvers
    # ------------------------------------------------------------------

    async def write_maneuvers(self, session_id: int, maneuvers: list[Any]) -> None:
        """Replace all maneuvers for a session with the new list (idempotent).

        Any moments anchored to the replaced maneuvers are downgraded to
        ``anchor_kind='timestamp'`` at their last-known t_start so they
        survive the detector re-run.
        """
        import json

        db = self._conn()
        # Downgrade dependent moments before the FKs vanish. Copy the maneuver's
        # ts into anchor_t_start so the moment keeps its place on the timeline —
        # the read-time resolver can't recover it once the maneuvers row is gone.
        await db.execute(
            "UPDATE moments"
            " SET anchor_kind = 'timestamp',"
            "     anchor_t_start = COALESCE("
            "         anchor_t_start,"
            "         (SELECT ts FROM maneuvers WHERE id = moments.anchor_entity_id)"
            "     ),"
            "     anchor_t_end = COALESCE("
            "         anchor_t_end,"
            "         (SELECT end_ts FROM maneuvers WHERE id = moments.anchor_entity_id)"
            "     ),"
            "     anchor_entity_id = NULL"
            " WHERE anchor_kind = 'maneuver'"
            " AND anchor_entity_id IN (SELECT id FROM maneuvers WHERE session_id = ?)",
            (session_id,),
        )
        await db.execute("DELETE FROM maneuvers WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM maneuver_cache WHERE session_id = ?", (session_id,))
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
        cur = await self._read_conn().execute(
            "SELECT id, session_id, type, ts, end_ts, duration_sec, loss_kts,"
            " vmg_loss_kts, tws_bin, twa_bin, head_to_wind_ts, details"
            " FROM maneuvers WHERE session_id = ? ORDER BY ts",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def set_maneuver_head_to_wind_ts(
        self, maneuver_id: int, head_to_wind_ts: str | None
    ) -> None:
        """Persist a computed head-to-wind timestamp onto the maneuvers row."""
        db = self._conn()
        await db.execute(
            "UPDATE maneuvers SET head_to_wind_ts = ? WHERE id = ?",
            (head_to_wind_ts, maneuver_id),
        )
        await db.commit()

    async def session_ids_with_stale_maneuver_cache(self, current_code_version: int) -> list[int]:
        """Return session_ids whose maneuver_cache.code_version is behind current.

        Used by the eager-backfill worker (#613): any session with stored
        maneuvers but a missing or older cached payload is re-enriched on
        service start so downstream analysis sees the latest field shape
        without waiting for a user to view the session.
        """
        cur = await self._read_conn().execute(
            """
            SELECT DISTINCT m.session_id AS session_id
              FROM maneuvers m
              LEFT JOIN maneuver_cache c ON c.session_id = m.session_id
             WHERE c.session_id IS NULL OR c.code_version < ?
             ORDER BY m.session_id
            """,
            (current_code_version,),
        )
        rows = await cur.fetchall()
        return [int(r["session_id"]) for r in rows]

    async def get_cached_enriched_maneuvers(
        self, session_id: int, code_version: int
    ) -> dict[str, Any] | None:
        """Return the cached enriched maneuver payload for ``session_id``.

        Returns ``None`` if there is no cache row or the stored
        ``code_version`` doesn't match — in both cases the caller should
        recompute and write through :meth:`put_cached_enriched_maneuvers`.
        """
        import json

        cur = await self._read_conn().execute(
            "SELECT payload, code_version FROM maneuver_cache WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None or int(row["code_version"]) != code_version:
            return None
        try:
            return json.loads(row["payload"])  # type: ignore[no-any-return]
        except (TypeError, ValueError):
            return None

    async def put_cached_enriched_maneuvers(
        self, session_id: int, code_version: int, payload: dict[str, Any]
    ) -> None:
        """Persist the enriched maneuver payload for ``session_id``."""
        import json
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        await db.execute(
            "INSERT OR REPLACE INTO maneuver_cache"
            " (session_id, payload, code_version, computed_at) VALUES (?, ?, ?, ?)",
            (session_id, json.dumps(payload), code_version, _datetime.now(UTC).isoformat()),
        )
        await db.commit()

    async def invalidate_session_maneuver_cache(self, session_id: int) -> None:
        """Drop any cached enriched payload for ``session_id``."""
        db = self._conn()
        await db.execute("DELETE FROM maneuver_cache WHERE session_id = ?", (session_id,))
        await db.commit()

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
        cur = await self._read_conn().execute(
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
        cur = await self._read_conn().execute(
            "SELECT mark_key, mark_name, lat, lon"
            " FROM synth_course_marks WHERE session_id = ? ORDER BY mark_key",
            (session_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Auth: users, invite tokens, sessions
    # ------------------------------------------------------------------

    async def create_user(
        self,
        email: str,
        name: str | None,
        role: str,
        *,
        is_developer: bool = False,
        is_active: bool = True,
    ) -> int:
        """Insert a new user and return the new id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO users (email, name, role, created_at, is_developer, is_active)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (email.lower().strip(), name, role, now, int(is_developer), int(is_active)),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    _USER_COLS = (
        "id, email, name, role, created_at, last_seen,"
        " avatar_path, is_developer, is_active, weight_lbs, color_scheme"
    )

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            f"SELECT {self._USER_COLS} FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            f"SELECT {self._USER_COLS} FROM users WHERE email = ?",
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

    async def update_user_developer(self, user_id: int, is_developer: bool) -> None:
        db = self._conn()
        await db.execute(
            "UPDATE users SET is_developer = ? WHERE id = ?", (int(is_developer), user_id)
        )
        await db.commit()

    async def update_user_profile(self, user_id: int, name: str | None, email: str | None) -> None:
        """Update a user's name and/or email."""
        db = self._conn()
        if email is not None:
            await db.execute(
                "UPDATE users SET email = ? WHERE id = ?",
                (email.lower().strip(), user_id),
            )
        if name is not None:
            await db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        await db.commit()

    async def list_users(self) -> list[dict[str, Any]]:
        cur = await self._read_conn().execute(
            "SELECT id, email, name, role, created_at, last_seen, is_developer,"
            " weight_lbs"
            " FROM users WHERE email NOT LIKE 'deleted_%@redacted'"
            " ORDER BY created_at"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Invitations (#268)
    # ------------------------------------------------------------------

    async def create_invitation(
        self,
        token: str,
        email: str,
        role: str,
        name: str | None,
        is_developer: bool,
        invited_by: int | None,
        expires_at: str,
    ) -> int:
        """Insert a new invitation and return the new id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO invitations"
            " (token, email, role, name, is_developer, invited_by, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                email.lower().strip(),
                role,
                name,
                int(is_developer),
                invited_by,
                now,
                expires_at,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_invitation(self, token: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, token, email, role, name, is_developer, invited_by,"
            " created_at, expires_at, accepted_at, revoked_at"
            " FROM invitations WHERE token = ?",
            (token,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def accept_invitation(self, token: str) -> None:
        """Mark the invitation as accepted (sets accepted_at to now)."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE invitations SET accepted_at = ? WHERE token = ?", (now, token))
        await db.commit()

    async def revoke_invitation(self, invitation_id: int) -> None:
        """Mark the invitation as revoked (sets revoked_at to now)."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE invitations SET revoked_at = ? WHERE id = ?", (now, invitation_id))
        await db.commit()

    async def list_pending_invitations(self) -> list[dict[str, Any]]:
        """Return invitations that are pending (not accepted, not revoked, not expired)."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        cur = await self._read_conn().execute(
            "SELECT id, token, email, role, name, is_developer, invited_by,"
            " created_at, expires_at"
            " FROM invitations"
            " WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ?"
            " ORDER BY created_at DESC",
            (now,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_pending_invitation_emails(self) -> set[str]:
        """Return the set of emails with a pending (unaccepted) invitation."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        cur = await self._read_conn().execute(
            "SELECT DISTINCT email FROM invitations"
            " WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
            (now,),
        )
        rows = await cur.fetchall()
        return {r["email"] for r in rows}

    # ------------------------------------------------------------------
    # User credentials (#268)
    # ------------------------------------------------------------------

    async def create_credential(
        self,
        user_id: int,
        provider: str,
        provider_uid: str | None,
        password_hash: str | None,
    ) -> int:
        """Insert a new user credential and return the new id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO user_credentials"
            " (user_id, provider, provider_uid, password_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, provider, provider_uid, password_hash, now),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_credential(self, user_id: int, provider: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, user_id, provider, provider_uid, password_hash, created_at"
            " FROM user_credentials WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_credential_by_provider_uid(
        self, provider: str, provider_uid: str
    ) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, user_id, provider, provider_uid, password_hash, created_at"
            " FROM user_credentials WHERE provider = ? AND provider_uid = ?",
            (provider, provider_uid),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_password_hash(self, user_id: int, password_hash: str) -> None:
        db = self._conn()
        await db.execute(
            "UPDATE user_credentials SET password_hash = ?"
            " WHERE user_id = ? AND provider = 'password'",
            (password_hash, user_id),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # Password reset tokens (#268)
    # ------------------------------------------------------------------

    async def create_password_reset_token(self, token: str, user_id: int, expires_at: str) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at),
        )
        await db.commit()

    async def get_password_reset_token(self, token: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, token, user_id, expires_at, used_at"
            " FROM password_reset_tokens WHERE token = ?",
            (token,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def use_password_reset_token(self, token: str) -> None:
        """Mark the reset token as used (sets used_at to now)."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE token = ?", (now, token)
        )
        await db.commit()

    # ------------------------------------------------------------------
    # User activation (#268)
    # ------------------------------------------------------------------

    async def deactivate_user(self, user_id: int) -> None:
        db = self._conn()
        await db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        await db.commit()

    async def activate_user(self, user_id: int) -> None:
        db = self._conn()
        await db.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
        await db.commit()

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
        cur = await self._read_conn().execute(
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
            cur = await self._read_conn().execute(
                "SELECT session_id, user_id, created_at, expires_at, ip, user_agent"
                " FROM auth_sessions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            cur = await self._read_conn().execute(
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
        cur = await self._read_conn().execute(
            "SELECT a.id, a.ts, a.action, a.detail, a.ip_address, a.user_agent,"
            " a.user_id, u.name AS user_name, u.email AS user_email"
            " FROM audit_log a LEFT JOIN users u ON a.user_id = u.id"
            " ORDER BY a.ts DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Multi-channel audio: channel_map + transcript_segments (#462 pt.1)
    # ------------------------------------------------------------------

    async def set_channel_map(
        self,
        *,
        vendor_id: int,
        product_id: int,
        serial: str,
        usb_port_path: str,
        mapping: dict[int, str],
        audio_session_id: int | None = None,
        created_by: int | None = None,
    ) -> None:
        """Replace the channel→position map for a USB device.

        ``audio_session_id=None`` writes the admin default; passing a session id
        writes a per-session override that takes precedence over the default
        when read with the same session id.
        """
        from datetime import UTC
        from datetime import datetime as _dt

        now = _dt.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "DELETE FROM channel_map"
            " WHERE vendor_id=? AND product_id=? AND serial=? AND usb_port_path=?"
            " AND IFNULL(audio_session_id, -1) = IFNULL(?, -1)",
            (vendor_id, product_id, serial, usb_port_path, audio_session_id),
        )
        for ch_idx, position in mapping.items():
            await db.execute(
                "INSERT INTO channel_map"
                " (vendor_id, product_id, serial, usb_port_path,"
                "  channel_index, position_name, audio_session_id,"
                "  created_utc, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    vendor_id,
                    product_id,
                    serial,
                    usb_port_path,
                    ch_idx,
                    position,
                    audio_session_id,
                    now,
                    created_by,
                ),
            )
        await db.commit()

    async def get_channel_map(
        self,
        *,
        vendor_id: int,
        product_id: int,
        serial: str,
        usb_port_path: str,
        audio_session_id: int | None = None,
    ) -> dict[int, str]:
        """Return the channel→position map for a device.

        If ``audio_session_id`` is given and a per-session override exists,
        return it; otherwise fall back to the admin default. Empty dict if
        neither is set.
        """
        db = self._read_conn()
        if audio_session_id is not None:
            cur = await db.execute(
                "SELECT channel_index, position_name FROM channel_map"
                " WHERE vendor_id=? AND product_id=? AND serial=? AND usb_port_path=?"
                " AND audio_session_id=?",
                (vendor_id, product_id, serial, usb_port_path, audio_session_id),
            )
            rows = await cur.fetchall()
            if rows:
                return {r["channel_index"]: r["position_name"] for r in rows}
        cur = await db.execute(
            "SELECT channel_index, position_name FROM channel_map"
            " WHERE vendor_id=? AND product_id=? AND serial=? AND usb_port_path=?"
            " AND audio_session_id IS NULL",
            (vendor_id, product_id, serial, usb_port_path),
        )
        return {r["channel_index"]: r["position_name"] for r in await cur.fetchall()}

    async def list_channel_map_devices(self) -> list[dict[str, Any]]:
        """Return one row per device that has an admin-default channel map.

        Each entry has the v63 identity tuple, the current ``mapping`` dict
        (channel_index → position_name), and ``last_updated_utc``. Used by
        the admin UI in #496 to render the device list.
        """
        cur = await self._read_conn().execute(
            "SELECT vendor_id, product_id, serial, usb_port_path,"
            " channel_index, position_name, created_utc"
            " FROM channel_map"
            " WHERE audio_session_id IS NULL"
            " ORDER BY vendor_id, product_id, serial, usb_port_path, channel_index"
        )
        rows = await cur.fetchall()
        grouped: dict[tuple[int, int, str, str], dict[str, Any]] = {}
        for r in rows:
            key = (r["vendor_id"], r["product_id"], r["serial"], r["usb_port_path"])
            entry = grouped.setdefault(
                key,
                {
                    "vendor_id": r["vendor_id"],
                    "product_id": r["product_id"],
                    "serial": r["serial"],
                    "usb_port_path": r["usb_port_path"],
                    "mapping": {},
                    "last_updated_utc": r["created_utc"],
                },
            )
            entry["mapping"][r["channel_index"]] = r["position_name"]
            if r["created_utc"] > entry["last_updated_utc"]:
                entry["last_updated_utc"] = r["created_utc"]
        return list(grouped.values())

    async def insert_transcript_segments(
        self, transcript_id: int, segments: list[dict[str, Any]]
    ) -> None:
        """Bulk-insert relational transcript segments with channel tags."""
        db = self._conn()
        for seg in segments:
            await db.execute(
                "INSERT INTO transcript_segments"
                " (transcript_id, segment_index, start_time, end_time,"
                "  text, speaker, channel_index, position_name)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transcript_id,
                    seg["segment_index"],
                    seg["start_time"],
                    seg["end_time"],
                    seg["text"],
                    seg.get("speaker"),
                    seg.get("channel_index"),
                    seg.get("position_name"),
                ),
            )
        await db.commit()

    async def list_transcript_segments(self, transcript_id: int) -> list[dict[str, Any]]:
        """Return relational transcript segments for a transcript, ordered."""
        cur = await self._read_conn().execute(
            "SELECT id, transcript_id, segment_index, start_time, end_time,"
            " text, speaker, channel_index, position_name, override_user_id"
            " FROM transcript_segments WHERE transcript_id=?"
            " ORDER BY segment_index",
            (transcript_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def set_segment_speaker_override(
        self, transcript_id: int, segment_index: int, user_id: int | None
    ) -> tuple[bool, str | None]:
        """Set or clear the per-segment speaker override (#648).

        Returns ``(found, name)`` — ``found`` is True if a segment row was
        matched; ``name`` is the user's display name if ``user_id`` is set and
        valid, else None (clearing the override). Passing ``user_id=None``
        clears the override.
        """
        db = self._conn()
        name: str | None = None
        if user_id is not None:
            ucur = await db.execute("SELECT name FROM users WHERE id = ?", (user_id,))
            urow = await ucur.fetchone()
            if urow is None:
                return (False, None)
            name = urow["name"] or f"User {user_id}"
        cur = await db.execute(
            "UPDATE transcript_segments SET override_user_id = ?"
            " WHERE transcript_id = ? AND segment_index = ?",
            (user_id, transcript_id, segment_index),
        )
        await db.commit()
        return ((cur.rowcount or 0) > 0, name)

    async def get_segment_overrides(self, transcript_id: int) -> dict[int, dict[str, Any]]:
        """Return {segment_index: {"user_id", "name"}} for every segment in a
        transcript that has an override set. Used by the transcript endpoint
        to attach override info to the merged response (#648).
        """
        cur = await self._read_conn().execute(
            "SELECT ts.segment_index, ts.override_user_id, u.name"
            " FROM transcript_segments ts"
            " LEFT JOIN users u ON u.id = ts.override_user_id"
            " WHERE ts.transcript_id = ? AND ts.override_user_id IS NOT NULL",
            (transcript_id,),
        )
        result: dict[int, dict[str, Any]] = {}
        for r in await cur.fetchall():
            result[int(r["segment_index"])] = {
                "user_id": int(r["override_user_id"]),
                "name": r["name"] or f"User {r['override_user_id']}",
            }
        return result

    async def log_voice_consent_ack(
        self,
        *,
        user_id: int | None,
        position_name: str,
        device: dict[str, Any] | None = None,
    ) -> int:
        """Record that a user acknowledged voice-biometric consent for a position.

        Writes a structured entry into the existing audit_log under the
        ``voice_consent_ack`` action so the data licensing review trail covers
        per-position diarisation. ``device`` may carry vendor/product/serial/
        port_path so the acknowledgement can be tied to a physical mic.
        """
        import json as _json

        payload: dict[str, Any] = {"position": position_name}
        if device is not None:
            payload["device"] = device
        return await self.log_action(
            "voice_consent_ack",
            detail=_json.dumps(payload, sort_keys=True),
            user_id=user_id,
        )

    # ------------------------------------------------------------------
    # Device API keys (#423)
    # ------------------------------------------------------------------

    async def create_device(
        self,
        name: str,
        key_hash: str,
        role: str,
        scope: str | None = None,
    ) -> int:
        """Insert a new device and return its id.

        *role* must be ``crew`` or ``viewer`` — devices cannot have admin role.
        *key_hash* is the SHA-256 hex digest of the plaintext bearer token.
        """
        if role == "admin":
            raise ValueError("Devices cannot be assigned the admin role")
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO devices (name, key_hash, role, scope, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, key_hash, role, scope, now),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    _DEVICE_COLS = "id, name, key_hash, role, scope, is_active, created_at, last_used"

    async def get_device(self, device_id: int) -> dict[str, Any] | None:
        """Return a device by id, or None."""
        cur = await self._read_conn().execute(
            f"SELECT {self._DEVICE_COLS} FROM devices WHERE id = ?",
            (device_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_device_by_key_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Return an active device matching the key hash, or None."""
        cur = await self._read_conn().execute(
            f"SELECT {self._DEVICE_COLS} FROM devices WHERE key_hash = ? AND is_active = 1",
            (key_hash,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return all devices ordered by creation time."""
        cur = await self._read_conn().execute(
            "SELECT id, name, role, scope, is_active, created_at, last_used"
            " FROM devices ORDER BY created_at"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def revoke_device(self, device_id: int) -> None:
        """Deactivate a device (soft-delete)."""
        db = self._conn()
        await db.execute("UPDATE devices SET is_active = 0 WHERE id = ?", (device_id,))
        await db.commit()

    async def rotate_device_key(self, device_id: int, new_key_hash: str) -> None:
        """Replace a device's key hash."""
        db = self._conn()
        await db.execute("UPDATE devices SET key_hash = ? WHERE id = ?", (new_key_hash, device_id))
        await db.commit()

    async def update_device_last_used(self, device_id: int) -> None:
        """Update the last_used timestamp to now."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE devices SET last_used = ? WHERE id = ?", (now, device_id))
        await db.commit()

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
        cur = await self._read_conn().execute(
            "SELECT id, name, color, created_at FROM tags WHERE name = ?",
            (name.strip().lower(),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_tags(self, *, order_by: str = "name") -> list[dict[str, Any]]:
        """Return all tags with usage counts, ordered by name or usage.

        order_by="usage" sorts by usage_count DESC, last_used_at DESC,
        name ASC — the picker's most-useful-first ordering.
        """
        if order_by not in {"name", "usage"}:
            raise ValueError(f"order_by must be 'name' or 'usage', got {order_by!r}")
        sql = "SELECT id, name, color, created_at, usage_count, last_used_at FROM tags ORDER BY "
        sql += (
            "usage_count DESC, COALESCE(last_used_at, '') DESC, name"
            if order_by == "usage"
            else "name"
        )
        cur = await self._read_conn().execute(sql)
        return [dict(r) for r in await cur.fetchall()]

    async def attach_tag(
        self,
        entity_type: str,
        entity_id: int,
        tag_id: int,
        *,
        user_id: int | None,
        source: str = "manual",
    ) -> None:
        """Attach a tag to an entity. Idempotent; duplicate call is a no-op.

        On a new attachment, increments tags.usage_count and stamps
        tags.last_used_at to the current UTC time.

        ``source`` records provenance. ``'manual'`` (default) for human
        action; ``'auto:transcript'`` or ``'auto:detector'`` for machine
        inference. Auto-sourced rows are hidden from the default list
        queries until ``confirm_tag_attachment`` is called.
        """
        if entity_type not in ENTITY_TYPES:
            raise ValueError(
                f"unknown entity_type {entity_type!r} (allowed: {sorted(ENTITY_TYPES)})"
            )
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT OR IGNORE INTO entity_tags "
            "(tag_id, entity_type, entity_id, created_at, created_by, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tag_id, entity_type, entity_id, now, user_id, source),
        )
        changed = cur.rowcount > 0
        if changed:
            await db.execute(
                "UPDATE tags SET usage_count = usage_count + 1, last_used_at = ? WHERE id = ?",
                (now, tag_id),
            )
        await db.commit()
        if changed:
            self._invalidate_sessions_list_cache()

    async def confirm_tag_attachment(
        self,
        entity_type: str,
        entity_id: int,
        tag_id: int,
        *,
        user_id: int | None,
    ) -> bool:
        """Accept an auto-generated tag. Returns True if a row was updated.

        Sets confirmed_at / confirmed_by. Preserves source — provenance is
        forever, so we can distinguish "manually tagged" from "auto-tagged
        and confirmed" long after the fact. No-op (returns False) if no
        matching attachment exists.
        """
        if entity_type not in ENTITY_TYPES:
            raise ValueError(
                f"unknown entity_type {entity_type!r} (allowed: {sorted(ENTITY_TYPES)})"
            )
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "UPDATE entity_tags SET confirmed_at = ?, confirmed_by = ?"
            " WHERE tag_id = ? AND entity_type = ? AND entity_id = ?",
            (now, user_id, tag_id, entity_type, entity_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def detach_tag(self, entity_type: str, entity_id: int, tag_id: int) -> bool:
        """Remove a tag from an entity. Returns True if a row was removed."""
        if entity_type not in ENTITY_TYPES:
            raise ValueError(
                f"unknown entity_type {entity_type!r} (allowed: {sorted(ENTITY_TYPES)})"
            )
        db = self._conn()
        cur = await db.execute(
            "DELETE FROM entity_tags WHERE tag_id = ? AND entity_type = ? AND entity_id = ?",
            (tag_id, entity_type, entity_id),
        )
        changed = cur.rowcount > 0
        if changed:
            await db.execute(
                "UPDATE tags SET usage_count = MAX(0, usage_count - 1) WHERE id = ?",
                (tag_id,),
            )
        await db.commit()
        if changed:
            self._invalidate_sessions_list_cache()
        return changed

    async def sessions_matching_tags(self, tag_ids: list[int], mode: str = "and") -> list[int]:
        """Return session ids whose session row OR any constituent entity
        (maneuver / moment) carries the given tag set.

        AND = every tag must appear somewhere under the session.
        OR  = at least one tag must appear somewhere under the session.
        Unknown tag ids are silently dropped, matching the stale-filter
        tolerance rule used by list_entities_with_tags.
        """
        if mode not in {"and", "or"}:
            raise ValueError(f"mode must be 'and' or 'or', got {mode!r}")
        if not tag_ids:
            return []
        placeholders = ",".join("?" * len(tag_ids))
        cur = await self._read_conn().execute(
            f"SELECT id FROM tags WHERE id IN ({placeholders})",  # noqa: S608
            tag_ids,
        )
        known = {r["id"] for r in await cur.fetchall()}
        filtered = [t for t in tag_ids if t in known]
        if not filtered:
            return []
        placeholders = ",".join("?" * len(filtered))
        # Union of (session_id, tag_id) pairs across each tagged layer.
        union = (
            f"SELECT et.entity_id AS session_id, et.tag_id FROM entity_tags et "
            f" WHERE et.entity_type = 'session' AND et.tag_id IN ({placeholders}) "
            f"UNION "
            f"SELECT m.session_id, et.tag_id FROM entity_tags et "
            f" JOIN maneuvers m ON m.id = et.entity_id "
            f" WHERE et.entity_type = 'maneuver' AND et.tag_id IN ({placeholders}) "
            f"UNION "
            f"SELECT mo.session_id, et.tag_id FROM entity_tags et "
            f" JOIN moments mo ON mo.id = et.entity_id "
            f" WHERE et.entity_type = 'moment' AND et.tag_id IN ({placeholders})"
        )
        if mode == "and":
            sql = (
                f"SELECT session_id FROM ({union}) "  # noqa: S608
                f"GROUP BY session_id HAVING COUNT(DISTINCT tag_id) = ?"
            )
            params = (*filtered, *filtered, *filtered, len(filtered))
        else:
            sql = f"SELECT DISTINCT session_id FROM ({union})"  # noqa: S608
            params = (*filtered, *filtered, *filtered)
        cur = await self._read_conn().execute(sql, params)
        return [r["session_id"] for r in await cur.fetchall()]

    async def list_session_tag_summary(
        self, session_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        """Return {session_id: [{tag_id, name, color, entity_type, count}, ...]}.

        One row per (session, tag, entity_type) combo with a count. Lets a
        history row render "weather (session) · good (3 maneuvers)".
        """
        out: dict[int, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
        if not session_ids:
            return out
        placeholders = ",".join("?" * len(session_ids))
        union = (
            f"SELECT et.tag_id, 'session' AS et_type, et.entity_id AS session_id "
            f" FROM entity_tags et "
            f" WHERE et.entity_type = 'session' AND et.entity_id IN ({placeholders}) "
            f"UNION ALL "
            f"SELECT et.tag_id, 'maneuver' AS et_type, m.session_id "
            f" FROM entity_tags et JOIN maneuvers m ON m.id = et.entity_id "
            f" WHERE et.entity_type = 'maneuver' AND m.session_id IN ({placeholders}) "
            f"UNION ALL "
            f"SELECT et.tag_id, 'moment' AS et_type, mo.session_id "
            f" FROM entity_tags et JOIN moments mo ON mo.id = et.entity_id "
            f" WHERE et.entity_type = 'moment' AND mo.session_id IN ({placeholders})"
        )
        sql = (
            f"SELECT u.session_id, u.tag_id, u.et_type, COUNT(*) AS n, "  # noqa: S608
            f"       t.name, t.color "
            f" FROM ({union}) u JOIN tags t ON t.id = u.tag_id "
            f" GROUP BY u.session_id, u.tag_id, u.et_type "
            f" ORDER BY t.name, u.et_type"
        )
        params = (*session_ids, *session_ids, *session_ids)
        cur = await self._read_conn().execute(sql, params)
        for r in await cur.fetchall():
            out.setdefault(r["session_id"], []).append(
                {
                    "id": r["tag_id"],
                    "name": r["name"],
                    "color": r["color"],
                    "entity_type": r["et_type"],
                    "count": r["n"],
                }
            )
        return out

    async def list_tags_for_entities(
        self,
        entity_type: str,
        entity_ids: list[int],
        *,
        include_unconfirmed: bool = False,
    ) -> dict[int, list[dict[str, Any]]]:
        """Return a {entity_id: [tag, ...]} map for the given ids in one query.

        Missing ids resolve to an empty list. Callers commonly pass "all
        maneuvers in session X" and want each row enriched with its tags
        without issuing N separate queries.

        Unconfirmed auto-tags (source LIKE 'auto:%' AND confirmed_at IS
        NULL) are hidden by default so they don't leak into normal UI
        surfaces. Review-queue callers pass ``include_unconfirmed=True``.
        """
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type {entity_type!r}")
        out: dict[int, list[dict[str, Any]]] = {eid: [] for eid in entity_ids}
        if not entity_ids:
            return out
        placeholders = ",".join("?" * len(entity_ids))
        confirm_filter = (
            ""
            if include_unconfirmed
            else " AND (et.source = 'manual' OR et.confirmed_at IS NOT NULL)"
        )
        cur = await self._read_conn().execute(
            f"SELECT et.entity_id, t.id, t.name, t.color,"  # noqa: S608
            f" et.source, et.confirmed_at"
            f" FROM entity_tags et JOIN tags t ON et.tag_id = t.id"
            f" WHERE et.entity_type = ? AND et.entity_id IN ({placeholders})"
            f"{confirm_filter}"
            f" ORDER BY t.name",
            (entity_type, *entity_ids),
        )
        for r in await cur.fetchall():
            out.setdefault(r["entity_id"], []).append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "color": r["color"],
                    "source": r["source"],
                    "confirmed_at": r["confirmed_at"],
                }
            )
        return out

    async def list_tags_for_entity(
        self,
        entity_type: str,
        entity_id: int,
        *,
        include_unconfirmed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return tags attached to a specific entity, ordered by name.

        Unconfirmed auto-tags are hidden by default; see
        ``list_tags_for_entities`` for the shared contract.
        """
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type {entity_type!r}")
        confirm_filter = (
            ""
            if include_unconfirmed
            else " AND (et.source = 'manual' OR et.confirmed_at IS NOT NULL)"
        )
        cur = await self._read_conn().execute(
            "SELECT t.id, t.name, t.color, et.source, et.confirmed_at FROM tags t"
            " JOIN entity_tags et ON t.id = et.tag_id"
            " WHERE et.entity_type = ? AND et.entity_id = ?"
            f"{confirm_filter}"
            " ORDER BY t.name",
            (entity_type, entity_id),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_entities_with_tags(
        self, entity_type: str, tag_ids: list[int], mode: str = "and"
    ) -> list[int]:
        """Return entity_ids of this type that match the tag filter.

        - Empty tag_ids → return every entity of this type.
        - mode="and": entity must carry *every* tag in the list.
        - mode="or":  entity must carry *any* tag in the list.
        - Unknown tag ids are silently dropped (stale-filter tolerance).
        """
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type {entity_type!r}")
        if mode not in {"and", "or"}:
            raise ValueError(f"mode must be 'and' or 'or', got {mode!r}")

        if not tag_ids:
            return await self._all_entity_ids(entity_type)

        # Drop unknown ids before querying so they don't skew AND matches.
        placeholders = ",".join("?" * len(tag_ids))
        cur = await self._read_conn().execute(
            f"SELECT id FROM tags WHERE id IN ({placeholders})",  # noqa: S608
            tag_ids,
        )
        known = {r["id"] for r in await cur.fetchall()}
        filtered = [t for t in tag_ids if t in known]
        if not filtered:
            return await self._all_entity_ids(entity_type)

        placeholders = ",".join("?" * len(filtered))
        if mode == "or":
            cur = await self._read_conn().execute(
                f"SELECT DISTINCT entity_id FROM entity_tags "  # noqa: S608
                f"WHERE entity_type = ? AND tag_id IN ({placeholders})",
                (entity_type, *filtered),
            )
        else:  # and
            cur = await self._read_conn().execute(
                f"SELECT entity_id FROM entity_tags "  # noqa: S608
                f"WHERE entity_type = ? AND tag_id IN ({placeholders}) "
                f"GROUP BY entity_id HAVING COUNT(DISTINCT tag_id) = ?",
                (entity_type, *filtered, len(filtered)),
            )
        return [r["entity_id"] for r in await cur.fetchall()]

    async def _all_entity_ids(self, entity_type: str) -> list[int]:
        """Return all entity ids of a given type. Source-of-truth per type."""
        table_col = {
            "session": ("races", "id"),
            "maneuver": ("maneuvers", "id"),
            "moment": ("moments", "id"),
        }[entity_type]
        table, col = table_col
        cur = await self._read_conn().execute(
            f"SELECT {col} AS entity_id FROM {table} ORDER BY {col}"  # noqa: S608
        )
        return [r["entity_id"] for r in await cur.fetchall()]

    async def merge_tags(self, source_id: int, target_id: int) -> None:
        """Merge `source` into `target`. Source is deleted; source's entity
        associations are reassigned to target (de-duping where an entity
        already had both), and target.usage_count is recomputed from the
        resulting entity_tags rows.
        """
        if source_id == target_id:
            raise ValueError("cannot merge a tag into itself (source == target)")
        db = self._conn()
        for check_id, label in ((source_id, "source"), (target_id, "target")):
            cur = await db.execute("SELECT id FROM tags WHERE id=?", (check_id,))
            if await cur.fetchone() is None:
                raise ValueError(f"{label} tag {check_id} does not exist")

        # Move source rows to target, ignoring duplicates that would violate
        # the (tag_id, entity_type, entity_id) PK.
        await db.execute(
            "INSERT OR IGNORE INTO entity_tags "
            "(tag_id, entity_type, entity_id, created_at, created_by) "
            "SELECT ?, entity_type, entity_id, created_at, created_by "
            "FROM entity_tags WHERE tag_id = ?",
            (target_id, source_id),
        )
        await db.execute("DELETE FROM entity_tags WHERE tag_id = ?", (source_id,))
        # Recompute usage_count on target from the entity_tags rows.
        await db.execute(
            "UPDATE tags SET usage_count = "
            "(SELECT COUNT(*) FROM entity_tags WHERE tag_id = ?) "
            "WHERE id = ?",
            (target_id, target_id),
        )
        await db.execute("DELETE FROM tags WHERE id = ?", (source_id,))
        await db.commit()
        self._invalidate_sessions_list_cache()

    async def get_or_create_tag(self, name: str, color: str | None = None) -> int:
        """Return the tag id for *name*, creating it if it doesn't exist."""
        tag = await self.get_tag_by_name(name)
        if tag:
            return tag["id"]  # type: ignore[no-any-return]
        return await self.create_tag(name, color)

    async def update_tag(
        self,
        tag_id: int,
        *,
        name: str | None = None,
        color: str | None = None,
        clear_color: bool = False,
    ) -> bool:
        """Update a tag's name or color. Returns True if found.

        Pass ``clear_color=True`` to explicitly set color to NULL; a
        ``color=None`` argument by itself is treated as "don't change".
        """
        parts: list[str] = []
        params: list[Any] = []
        if name is not None:
            parts.append("name = ?")
            params.append(name.strip().lower())
        if clear_color:
            parts.append("color = NULL")
        elif color is not None:
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
        changed = cur.rowcount > 0
        if changed:
            self._invalidate_sessions_list_cache()
        return changed

    async def delete_tag(self, tag_id: int) -> bool:
        """Delete a tag. entity_tags.tag_id has ON DELETE CASCADE, so any
        attachments are removed automatically (requires PRAGMA foreign_keys=ON
        on the connection; tests explicitly enable it where they assert the
        cascade). Returns True if the tag existed.
        """
        db = self._conn()
        cur = await db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        await db.commit()
        changed = cur.rowcount > 0
        if changed:
            self._invalidate_sessions_list_cache()
        return changed

    # ------------------------------------------------------------------
    # Moments, attachments, comments, read state (#662 moments unification)
    # ------------------------------------------------------------------

    _MOMENT_COLS_Q = (
        "m.id, m.session_id, m.subject, m.counterparty, m.anchor_kind,"
        " m.anchor_entity_id, m.anchor_t_start, m.anchor_t_end, m.resolved,"
        " m.resolved_at, m.resolved_by, m.resolution_summary, m.source,"
        " m.confirmed_at, m.confirmed_by, m.created_by, m.created_at,"
        " m.updated_at"
    )
    _MOMENT_COLS = (
        "id, session_id, subject, counterparty, anchor_kind, anchor_entity_id,"
        " anchor_t_start, anchor_t_end, resolved, resolved_at, resolved_by,"
        " resolution_summary, source, confirmed_at, confirmed_by,"
        " created_by, created_at, updated_at"
    )
    _MOMENT_ANCHOR_KINDS: frozenset[str] = frozenset(
        {"session", "timestamp", "maneuver", "transcript_segment"}
    )

    async def create_moment(
        self,
        *,
        session_id: int,
        anchor_kind: str,
        anchor_entity_id: int | None = None,
        anchor_t_start: str | None = None,
        anchor_t_end: str | None = None,
        subject: str | None = None,
        counterparty: str | None = None,
        user_id: int | None = None,
        source: str = "manual",
    ) -> int:
        """Create a moment on *session_id*. Returns the new id.

        Raises AnchorError for an anchor shape that doesn't satisfy
        ``{session, timestamp, maneuver, transcript_segment}`` rules.
        """
        if anchor_kind not in self._MOMENT_ANCHOR_KINDS:
            raise AnchorError(f"unknown anchor_kind {anchor_kind!r}")
        if anchor_kind == "session":
            if (
                anchor_entity_id is not None
                or anchor_t_start is not None
                or anchor_t_end is not None
            ):
                raise AnchorError("session anchor must not set entity_id or times")
        elif anchor_kind == "timestamp":
            if anchor_t_start is None:
                raise AnchorError("timestamp anchor requires anchor_t_start")
            if anchor_entity_id is not None:
                raise AnchorError("timestamp anchor must not set anchor_entity_id")
            if anchor_t_end is not None and anchor_t_end <= anchor_t_start:
                raise AnchorError("anchor_t_end must be after anchor_t_start")
        else:  # maneuver | transcript_segment
            if anchor_entity_id is None:
                raise AnchorError(f"{anchor_kind} anchor requires anchor_entity_id")

        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO moments"
            " (session_id, subject, counterparty, anchor_kind, anchor_entity_id,"
            "  anchor_t_start, anchor_t_end, resolved, source, created_by,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (
                session_id,
                subject,
                counterparty,
                anchor_kind,
                anchor_entity_id,
                anchor_t_start,
                anchor_t_end,
                source,
                user_id,
                now,
                now,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def _resolve_moment_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Derive t_start/t_end for maneuver / transcript_segment anchors.

        If the referenced entity is gone, downgrade to anchor_kind='timestamp'
        at the last-known t_start (persisted) so nothing is lost.
        """
        kind = row.get("anchor_kind")
        ent = row.get("anchor_entity_id")
        if kind not in {"maneuver", "transcript_segment"} or ent is None:
            return row

        db = self._conn()
        if kind == "maneuver":
            cur = await db.execute("SELECT ts, end_ts FROM maneuvers WHERE id = ?", (ent,))
            m = await cur.fetchone()
            if m is not None:
                row["anchor_t_start"] = m["ts"]
                row["anchor_t_end"] = m["end_ts"]
                return row
        else:  # transcript_segment
            cur = await db.execute(
                "SELECT start_time, end_time FROM transcript_segments WHERE id = ?",
                (ent,),
            )
            seg = await cur.fetchone()
            if seg is not None:
                row["anchor_t_start"] = seg["start_time"]
                row["anchor_t_end"] = seg["end_time"]
                return row

        # Underlying entity is gone — downgrade so the moment survives.
        await db.execute(
            "UPDATE moments SET anchor_kind = 'timestamp', anchor_entity_id = NULL WHERE id = ?",
            (row["id"],),
        )
        await db.commit()
        row["anchor_kind"] = "timestamp"
        row["anchor_entity_id"] = None
        return row

    async def get_moment(self, moment_id: int) -> dict[str, Any] | None:
        """Return a moment with anchor times resolved + embedded comments and
        attachments. ``None`` if not found."""
        db = self._conn()
        cur = await db.execute(
            f"SELECT {self._MOMENT_COLS} FROM moments WHERE id = ?",  # noqa: S608
            (moment_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        m = await self._resolve_moment_row(dict(row))
        m = _project_moment_anchor(m)
        cur = await db.execute(
            "SELECT c.id, c.moment_id, c.author, c.body, c.created_at, c.edited_at,"
            " u.name AS author_name, u.email AS author_email"
            " FROM comments c LEFT JOIN users u ON c.author = u.id"
            " WHERE c.moment_id = ? ORDER BY c.created_at",
            (moment_id,),
        )
        m["comments"] = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT id, moment_id, kind, path, body, created_by, created_at"
            " FROM moment_attachments WHERE moment_id = ? ORDER BY id",
            (moment_id,),
        )
        m["attachments"] = [dict(r) for r in await cur.fetchall()]
        return m

    async def list_moments_for_session(
        self, session_id: int, *, include_unconfirmed: bool = False
    ) -> list[dict[str, Any]]:
        """Return moments on a session with anchors resolved + comment/unread
        counts. Auto-sourced unconfirmed moments are hidden unless caller asks."""
        db = self._conn()
        where = "WHERE m.session_id = ?"
        params: tuple[Any, ...] = (session_id,)
        if not include_unconfirmed:
            where += " AND (m.source = 'manual' OR m.confirmed_at IS NOT NULL)"
        cur = await db.execute(
            f"SELECT {self._MOMENT_COLS_Q},"  # noqa: S608
            f" u.name AS author_name, u.email AS author_email,"
            f" (SELECT COUNT(*) FROM comments c WHERE c.moment_id = m.id)"
            f"    AS comment_count,"
            f" (SELECT c.body FROM comments c WHERE c.moment_id = m.id"
            f"    ORDER BY c.created_at LIMIT 1) AS first_comment_body"
            f" FROM moments m LEFT JOIN users u ON m.created_by = u.id"
            f" {where} ORDER BY COALESCE(m.anchor_t_start, m.created_at), m.id",
            params,
        )
        rows = [dict(r) for r in await cur.fetchall()]
        resolved = [await self._resolve_moment_row(r) for r in rows]
        return [_project_moment_anchor(r) for r in resolved]

    async def update_moment(
        self,
        moment_id: int,
        *,
        subject: str | None = None,
        counterparty: str | None = None,
        clear_subject: bool = False,
        clear_counterparty: bool = False,
    ) -> bool:
        parts: list[str] = []
        params: list[Any] = []
        if clear_subject:
            parts.append("subject = NULL")
        elif subject is not None:
            parts.append("subject = ?")
            params.append(subject)
        if clear_counterparty:
            parts.append("counterparty = NULL")
        elif counterparty is not None:
            parts.append("counterparty = ?")
            params.append(counterparty)
        if not parts:
            cur = await self._conn().execute("SELECT 1 FROM moments WHERE id = ?", (moment_id,))
            return await cur.fetchone() is not None
        parts.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(moment_id)
        db = self._conn()
        cur = await db.execute(
            f"UPDATE moments SET {', '.join(parts)} WHERE id = ?",  # noqa: S608
            params,
        )
        await db.commit()
        return cur.rowcount > 0

    async def delete_moment(self, moment_id: int) -> bool:
        db = self._conn()
        cur = await db.execute("DELETE FROM moments WHERE id = ?", (moment_id,))
        await db.commit()
        return cur.rowcount > 0

    async def resolve_moment(
        self,
        moment_id: int,
        user_id: int,
        resolution_summary: str | None = None,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "UPDATE moments SET resolved = 1, resolved_at = ?, resolved_by = ?,"
            " resolution_summary = ?, updated_at = ? WHERE id = ?",
            (now, user_id, resolution_summary, now, moment_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def unresolve_moment(self, moment_id: int) -> bool:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "UPDATE moments SET resolved = 0, resolved_at = NULL, resolved_by = NULL,"
            " resolution_summary = NULL, updated_at = ? WHERE id = ?",
            (now, moment_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def confirm_moment(self, moment_id: int, user_id: int) -> bool:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "UPDATE moments SET confirmed_at = ?, confirmed_by = ?, updated_at = ? WHERE id = ?",
            (now, user_id, now, moment_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def list_moment_counterparties(self) -> list[str]:
        """Distinct, non-null moment counterparties for typeahead."""
        cur = await self._read_conn().execute(
            "SELECT DISTINCT counterparty FROM moments"
            " WHERE counterparty IS NOT NULL AND counterparty != ''"
            " ORDER BY counterparty"
        )
        return [r["counterparty"] for r in await cur.fetchall()]

    async def downgrade_moments_anchored_to_maneuver(self, maneuver_id: int) -> int:
        """When a maneuver is deleted, anchor its moments to 'timestamp' at
        the last-known t_start instead of orphaning them. Returns count."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE moments SET anchor_kind = 'timestamp',"
            " anchor_entity_id = NULL"
            " WHERE anchor_kind = 'maneuver' AND anchor_entity_id = ?",
            (maneuver_id,),
        )
        await db.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Moment attachments
    # ------------------------------------------------------------------

    async def create_attachment(
        self,
        *,
        moment_id: int,
        kind: str,
        path: str | None = None,
        body: str | None = None,
        user_id: int | None = None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO moment_attachments"
            " (moment_id, kind, path, body, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (moment_id, kind, path, body, user_id, now),
        )
        await db.commit()
        return int(cur.lastrowid or 0)

    async def list_attachments_for_moment(self, moment_id: int) -> list[dict[str, Any]]:
        cur = await self._read_conn().execute(
            "SELECT id, moment_id, kind, path, body, created_by, created_at"
            " FROM moment_attachments WHERE moment_id = ? ORDER BY id",
            (moment_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def delete_attachment(self, attachment_id: int) -> bool:
        db = self._conn()
        cur = await db.execute("DELETE FROM moment_attachments WHERE id = ?", (attachment_id,))
        await db.commit()
        return cur.rowcount > 0

    async def delete_attachment_with_file(self, attachment_id: int) -> tuple[bool, str | None]:
        """Delete an attachment and return (found, path) for disk cleanup."""
        db = self._conn()
        cur = await db.execute("SELECT path FROM moment_attachments WHERE id = ?", (attachment_id,))
        row = await cur.fetchone()
        if row is None:
            return False, None
        path = row["path"]
        await db.execute("DELETE FROM moment_attachments WHERE id = ?", (attachment_id,))
        await db.commit()
        return True, path

    # ------------------------------------------------------------------
    # Session settings (config snapshots, was note_type='settings')
    # ------------------------------------------------------------------

    async def create_session_setting(
        self,
        *,
        session_id: int | None,
        body: str,
        user_id: int | None = None,
        audio_session_id: int | None = None,
        ts: str | None = None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        stamp = ts or now
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO session_settings"
            " (session_id, audio_session_id, ts, body, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, audio_session_id, stamp, body, user_id, now),
        )
        await db.commit()
        return int(cur.lastrowid or 0)

    async def list_session_settings(self, session_id: int) -> list[dict[str, Any]]:
        cur = await self._read_conn().execute(
            "SELECT id, session_id, audio_session_id, ts, body, created_by, created_at"
            " FROM session_settings WHERE session_id = ? ORDER BY ts",
            (session_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Anchor picker data source (#478 / #588 slice 2)
    # ------------------------------------------------------------------

    async def list_session_anchors(self, session_id: int) -> list[dict[str, Any]]:
        """Return pickable anchors for a session.

        Each entry is a dict with keys: kind, entity_id, label, t_start.
        ``kind`` is one of the moment anchor-kinds — ``session`` (session-wide),
        ``timestamp`` (e.g. the start line), or ``maneuver``.
        ``transcript_segment`` anchors are created from the transcript UI
        directly rather than surfaced through this picker.
        Moments themselves are NOT pickable (a moment cannot anchor to
        another moment).
        """
        race = await self.get_race(session_id)
        if race is None:
            return []

        start_utc = race.start_utc.isoformat() if race.start_utc else None
        race_label = race.name or f"Race {session_id}"

        out: list[dict[str, Any]] = []
        if start_utc is not None:
            # Session-wide anchor (everything about this race).
            out.append(
                {
                    "kind": "session",
                    "entity_id": session_id,
                    "label": race_label,
                    "t_start": start_utc,
                }
            )
            # Start-line timestamp anchor.
            out.append(
                {
                    "kind": "timestamp",
                    "entity_id": None,
                    "label": f"Start sequence · {_hms_from_iso(start_utc)}",
                    "t_start": start_utc,
                }
            )

        for mv in await self.get_session_maneuvers(session_id):
            out.append(
                {
                    "kind": "maneuver",
                    "entity_id": mv["id"],
                    "label": f"{(mv['type'] or 'Maneuver').title()} · {_hms_from_iso(mv['ts'])}",
                    "t_start": mv["ts"],
                }
            )

        out.sort(key=lambda a: (a["t_start"] or "", a["kind"]))
        return out

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
        cur = await self._read_conn().execute(
            "SELECT avatar_path FROM users WHERE id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row["avatar_path"] if row else None

    # ------------------------------------------------------------------
    # App settings (#146)
    # ------------------------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        """Return the stored value for *key*, or None if not set."""
        cur = await self._read_conn().execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        )
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
        cur = await self._read_conn().execute(
            "SELECT key, value, updated_at FROM app_settings ORDER BY key"
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # LLM transcript Q&A and callback detection (#697)
    # ------------------------------------------------------------------

    async def get_llm_consent(self) -> dict[str, Any] | None:
        """Return the LLM consent record, or None if not yet acknowledged.

        Diarized transcripts are PII; the consent flag must be set before
        any transcript is sent to a hosted LLM.
        """
        cur = await self._read_conn().execute(
            "SELECT acknowledged, by_user, at FROM llm_consent WHERE id = 1"
        )
        row = await cur.fetchone()
        if row is None or not row["acknowledged"]:
            return None
        return {"by_user": row["by_user"], "at": row["at"]}

    async def acknowledge_llm_consent(self, user_id: int | None) -> None:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        db = self._conn()
        # UPSERT — robust whether the singleton row was seeded by the
        # migration or not. (Pi DBs from before the seed-INSERT landed
        # have an empty table, so a plain UPDATE silently no-ops.)
        await db.execute(
            "INSERT INTO llm_consent (id, acknowledged, by_user, at)"
            " VALUES (1, 1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " acknowledged = 1, by_user = excluded.by_user, at = excluded.at",
            (user_id, _datetime.now(_UTC).isoformat()),
        )
        await db.commit()

    async def insert_llm_qa(
        self,
        *,
        race_id: int,
        user_id: int | None,
        question: str,
        answer: str | None,
        citations: list[dict[str, Any]],
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_create_tokens: int,
        cost_usd: float,
        status: str = "complete",
        error_msg: str | None = None,
    ) -> int:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        db = self._conn()
        cur = await db.execute(
            "INSERT INTO llm_qa (race_id, user_id, question, answer, citations_json,"
            " model, input_tokens, output_tokens, cache_read_tokens,"
            " cache_create_tokens, cost_usd, status, error_msg, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                race_id,
                user_id,
                question,
                answer,
                json.dumps(citations),
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_create_tokens,
                cost_usd,
                status,
                error_msg,
                _datetime.now(_UTC).isoformat(),
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def list_llm_qa(self, race_id: int) -> list[dict[str, Any]]:
        cur = await self._read_conn().execute(
            "SELECT id, race_id, user_id, question, answer, citations_json,"
            " model, input_tokens, output_tokens, cache_read_tokens,"
            " cache_create_tokens, cost_usd, status, error_msg, created_at"
            " FROM llm_qa WHERE race_id = ? ORDER BY created_at, id",
            (race_id,),
        )
        rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["citations"] = json.loads(d.pop("citations_json") or "[]")
            out.append(d)
        return out

    async def replace_llm_callbacks(
        self,
        *,
        race_id: int,
        callbacks: list[dict[str, Any]],
        job_cost_usd: float,
    ) -> None:
        """Replace all detected callbacks for a race in one transaction.

        Idempotent re-run: prior `llm_callbacks` rows for the race are
        deleted, the new set is inserted, and the per-race job row is
        updated to Complete with cumulative cost. Linked `moments` survive
        because moment rows are independent (the FK from llm_callbacks
        only flows the other way).
        """
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        now = _datetime.now(_UTC).isoformat()
        db = self._conn()
        await db.execute("BEGIN")
        try:
            await db.execute("DELETE FROM llm_callbacks WHERE race_id = ?", (race_id,))
            for cb in callbacks:
                await db.execute(
                    "INSERT INTO llm_callbacks (race_id, speaker_label, anchor_ts,"
                    " source_excerpt, rationale, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        race_id,
                        cb.get("speaker_label"),
                        cb["anchor_ts"],
                        cb["source_excerpt"],
                        cb.get("rationale"),
                        now,
                    ),
                )
            await db.execute(
                "INSERT INTO llm_callback_jobs (race_id, status, cost_usd,"
                " started_at, finished_at) VALUES (?, 'Complete', ?, ?, ?)"
                " ON CONFLICT(race_id) DO UPDATE SET"
                " status = 'Complete', error_msg = NULL,"
                " cost_usd = llm_callback_jobs.cost_usd + excluded.cost_usd,"
                " finished_at = excluded.finished_at",
                (race_id, job_cost_usd, now, now),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def list_llm_callbacks(
        self,
        race_id: int,
        *,
        speaker: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, race_id, speaker_label, anchor_ts, source_excerpt,"
            " rationale, moment_id, created_at FROM llm_callbacks"
            " WHERE race_id = ?"
        )
        params: list[Any] = [race_id]
        if speaker is not None:
            sql += " AND speaker_label = ?"
            params.append(speaker)
        sql += " ORDER BY anchor_ts, id"
        cur = await self._read_conn().execute(sql, params)
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def link_llm_callback_moment(
        self,
        *,
        callback_id: int,
        moment_id: int,
    ) -> None:
        db = self._conn()
        await db.execute(
            "UPDATE llm_callbacks SET moment_id = ? WHERE id = ?",
            (moment_id, callback_id),
        )
        await db.commit()

    async def get_callback_job(self, race_id: int) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT race_id, status, error_msg, cost_usd, started_at, finished_at"
            " FROM llm_callback_jobs WHERE race_id = ?",
            (race_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_callback_job(
        self,
        race_id: int,
        *,
        status: str,
        error_msg: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        if status not in {"NotRun", "Running", "Complete", "Failed"}:
            raise ValueError(f"invalid callback job status: {status!r}")
        now = _datetime.now(_UTC).isoformat()
        started = now if status == "Running" else None
        finished = now if status in {"Complete", "Failed"} else None
        db = self._conn()
        await db.execute(
            "INSERT INTO llm_callback_jobs (race_id, status, error_msg, cost_usd,"
            " started_at, finished_at) VALUES (?, ?, ?, COALESCE(?, 0), ?, ?)"
            " ON CONFLICT(race_id) DO UPDATE SET"
            " status = excluded.status,"
            " error_msg = excluded.error_msg,"
            " cost_usd = COALESCE(?, llm_callback_jobs.cost_usd),"
            " started_at = COALESCE(excluded.started_at, llm_callback_jobs.started_at),"
            " finished_at = COALESCE(excluded.finished_at, llm_callback_jobs.finished_at)",
            (race_id, status, error_msg, cost_usd, started, finished, cost_usd),
        )
        await db.commit()

    async def get_race_caps(self, race_id: int) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT race_id, soft_warn_usd, hard_cap_usd, updated_by, updated_at"
            " FROM llm_race_caps WHERE race_id = ?",
            (race_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_race_caps(
        self,
        *,
        race_id: int,
        soft_warn_usd: float | None,
        hard_cap_usd: float | None,
        by_user: int | None,
    ) -> None:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        now = _datetime.now(_UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO llm_race_caps (race_id, soft_warn_usd, hard_cap_usd,"
            " updated_by, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(race_id) DO UPDATE SET"
            " soft_warn_usd = excluded.soft_warn_usd,"
            " hard_cap_usd = excluded.hard_cap_usd,"
            " updated_by = excluded.updated_by,"
            " updated_at = excluded.updated_at",
            (race_id, soft_warn_usd, hard_cap_usd, by_user, now),
        )
        await db.commit()

    async def race_llm_cost(self, race_id: int) -> float:
        """Sum of per-race LLM spend across Q&A queries and the callback job.

        Includes failed Q&A rows because tokens may have been billed before
        the failure — counting them prevents repeated failures from
        bypassing the per-race hard cap.
        """
        cur = await self._read_conn().execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS qa FROM llm_qa WHERE race_id = ?",
            (race_id,),
        )
        qa_row = await cur.fetchone()
        assert qa_row is not None  # COALESCE in aggregate query — always one row
        cur = await self._read_conn().execute(
            "SELECT COALESCE(cost_usd, 0) AS job FROM llm_callback_jobs WHERE race_id = ?",
            (race_id,),
        )
        job_row = await cur.fetchone()
        return float(qa_row["qa"]) + float(job_row["job"] if job_row else 0)

    # ------------------------------------------------------------------
    # Color schemes (#347)
    # ------------------------------------------------------------------

    async def create_color_scheme(
        self,
        name: str,
        bg: str,
        text_color: str,
        accent: str,
        created_by: int | None,
    ) -> int:
        """Insert a new custom color scheme. Returns the new row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO color_schemes (name, bg, text_color, accent, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, bg, text_color, accent, created_by, now),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def update_color_scheme(
        self, scheme_id: int, name: str, bg: str, text_color: str, accent: str
    ) -> bool:
        """Update an existing custom color scheme. Returns True if found."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE color_schemes SET name = ?, bg = ?, text_color = ?, accent = ? WHERE id = ?",
            (name, bg, text_color, accent, scheme_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def delete_color_scheme(self, scheme_id: int) -> bool:
        """Delete a custom color scheme. Returns True if found."""
        db = self._conn()
        cur = await db.execute("DELETE FROM color_schemes WHERE id = ?", (scheme_id,))
        await db.commit()
        return cur.rowcount > 0

    async def get_color_scheme(self, scheme_id: int) -> dict[str, Any] | None:
        """Return a custom color scheme row by id, or None."""
        cur = await self._read_conn().execute(
            "SELECT id, name, bg, text_color, accent, created_by, created_at"
            " FROM color_schemes WHERE id = ?",
            (scheme_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_color_schemes(self) -> list[dict[str, Any]]:
        """Return all custom color schemes ordered by name."""
        cur = await self._read_conn().execute(
            "SELECT id, name, bg, text_color, accent, created_by, created_at"
            " FROM color_schemes ORDER BY name"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def set_user_color_scheme(self, user_id: int, scheme: str | None) -> None:
        """Set or clear a user's personal color scheme override."""
        db = self._conn()
        await db.execute(
            "UPDATE users SET color_scheme = ? WHERE id = ?",
            (scheme, user_id),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # WLAN profiles (#256)
    # ------------------------------------------------------------------

    async def list_wlan_profiles(self) -> list[dict[str, Any]]:
        """Return all saved WLAN profiles, ordered by name."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, ssid, password, is_default, created_at"
            " FROM wlan_profiles ORDER BY name ASC"
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_wlan_profile(self, profile_id: int) -> dict[str, Any] | None:
        """Return a single WLAN profile by id, or None."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, ssid, password, is_default, created_at"
            " FROM wlan_profiles WHERE id = ?",
            (profile_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_wlan_profile(
        self,
        name: str,
        ssid: str,
        password: str | None = None,
        is_default: bool = False,
    ) -> int:
        """Add a WLAN profile. Returns the new row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        now = _datetime.now(UTC).isoformat()

        if is_default:
            await db.execute("UPDATE wlan_profiles SET is_default = 0")

        cur = await db.execute(
            "INSERT INTO wlan_profiles (name, ssid, password, is_default, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, ssid, password, 1 if is_default else 0, now),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info("WLAN profile added: id={} name={} ssid={}", cur.lastrowid, name, ssid)
        return cur.lastrowid

    async def update_wlan_profile(
        self,
        profile_id: int,
        name: str,
        ssid: str,
        password: str | None = None,
        is_default: bool = False,
    ) -> bool:
        """Update a WLAN profile. Returns True if found."""
        db = self._conn()

        if is_default:
            await db.execute("UPDATE wlan_profiles SET is_default = 0")

        cur = await db.execute(
            "UPDATE wlan_profiles SET name = ?, ssid = ?, password = ?, is_default = ?"
            " WHERE id = ?",
            (name, ssid, password, 1 if is_default else 0, profile_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_wlan_profile(self, profile_id: int) -> bool:
        """Delete a WLAN profile by id. Returns True if found."""
        db = self._conn()
        cur = await db.execute("DELETE FROM wlan_profiles WHERE id = ?", (profile_id,))
        await db.commit()
        deleted = (cur.rowcount or 0) > 0
        if deleted:
            logger.info("WLAN profile deleted: id={}", profile_id)
        return deleted

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

        # Collect attachment file paths (photos, clips) from moments on this session
        cur = await db.execute(
            "SELECT ma.path FROM moment_attachments ma"
            " JOIN moments m ON m.id = ma.moment_id"
            " WHERE m.session_id = ? AND ma.path IS NOT NULL",
            (session_id,),
        )
        for row in await cur.fetchall():
            if row["path"]:
                files.append(row["path"])

        # Cascade delete handles: race_crew, race_results, race_sails,
        # race_videos, moments (and their attachments/comments), etc.
        # Tables below lack ON DELETE CASCADE and must be deleted manually.

        # extraction_runs → transcripts → audio_sessions: neither FK has CASCADE,
        # so delete the chain manually before removing audio_sessions.
        await db.execute(
            "DELETE FROM extraction_items WHERE extraction_run_id IN"
            " (SELECT er.id FROM extraction_runs er"
            "  JOIN transcripts t ON er.transcript_id = t.id"
            "  JOIN audio_sessions a ON t.audio_session_id = a.id"
            "  WHERE a.race_id = ?)",
            (session_id,),
        )
        await db.execute(
            "DELETE FROM extraction_runs WHERE transcript_id IN"
            " (SELECT t.id FROM transcripts t"
            "  JOIN audio_sessions a ON t.audio_session_id = a.id"
            "  WHERE a.race_id = ?)",
            (session_id,),
        )
        await db.execute("DELETE FROM audio_sessions WHERE race_id = ?", (session_id,))
        await db.execute("DELETE FROM camera_sessions WHERE session_id = ?", (session_id,))
        # sensor_readings may not exist yet (created by sensor device feature)
        cur = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sensor_readings'"
        )
        if await cur.fetchone():
            await db.execute("DELETE FROM sensor_readings WHERE session_id = ?", (session_id,))

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
        await self._invalidate_race_cache(session_id)
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
        # extraction_runs FK to transcripts lacks CASCADE — delete manually
        await db.execute(
            "DELETE FROM extraction_items WHERE extraction_run_id IN"
            " (SELECT er.id FROM extraction_runs er"
            "  JOIN transcripts t ON er.transcript_id = t.id"
            "  WHERE t.audio_session_id = ?)",
            (audio_session_id,),
        )
        await db.execute(
            "DELETE FROM extraction_runs WHERE transcript_id IN"
            " (SELECT id FROM transcripts WHERE audio_session_id = ?)",
            (audio_session_id,),
        )
        # transcripts cascade via FK on audio_sessions
        await db.execute("DELETE FROM audio_sessions WHERE id = ?", (audio_session_id,))
        await db.commit()
        logger.info("Audio session {} deleted", audio_session_id)
        return file_path

    # ------------------------------------------------------------------
    # Per-channel audio deletion (#462 pt.7 / #499)
    # ------------------------------------------------------------------

    async def delete_audio_channel(
        self,
        audio_session_id: int,
        *,
        channel_index: int,
        user_id: int | None = None,
        reason: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Atomically delete one channel's audio + transcript + channel_map.

        Executes the per-channel data-licensing deletion right for a single
        position in a multi-channel recording:

        * Zero out ``channel_index`` in the WAV on disk (preserves channel
          count so remaining channels keep their indices).
        * Delete all ``transcript_segments`` rows tagged with that channel
          for this session's transcript(s).
        * Delete the matching ``channel_map`` row(s) for this session.
        * Write an ``audio_channel_delete`` audit log entry.

        The WAV rewrite is staged via a sibling ``.tmpNNN`` file and swapped
        with ``os.replace`` only after all DB mutations succeed; any failure
        rolls back the DB and leaves the original WAV untouched.
        """
        import os
        import tempfile

        import soundfile as sf

        row = await self.get_audio_session_row(audio_session_id)
        if row is None:
            raise ValueError(f"audio session {audio_session_id} not found")

        # Sibling-card dispatch (#509 chunk 4): when the target belongs to a
        # capture group, "delete channel N" means "delete the sibling with
        # capture_ordinal=N" — each sibling is its own mono WAV + transcript
        # + channel_map row, so the data-licensing deletion right reduces to
        # a full-sibling removal rather than zeroing a channel in a file.
        if row.get("capture_group_id"):
            await self._delete_sibling_by_ordinal(
                row,
                channel_index=channel_index,
                user_id=user_id,
                reason=reason,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            return

        channels = int(row.get("channels") or 0)
        if channel_index < 0 or channel_index >= channels:
            raise ValueError(
                f"channel_index {channel_index} out of range for {channels}-channel session"
            )
        wav_path = Path(row["file_path"])
        if not wav_path.exists():
            raise FileNotFoundError(f"audio file missing: {wav_path}")

        # Stage the zeroed WAV into a sibling tmp file. soundfile preserves
        # samplerate/subtype via sf.info.
        data, sr = sf.read(str(wav_path), always_2d=True)
        info = sf.info(str(wav_path))
        if data.shape[1] != channels:
            raise RuntimeError(f"WAV channel count {data.shape[1]} != DB channels {channels}")
        data[:, channel_index] = 0

        fd, tmp_name = tempfile.mkstemp(
            prefix=wav_path.name + ".", suffix=".tmp", dir=str(wav_path.parent)
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            sf.write(str(tmp_path), data, sr, format=info.format, subtype=info.subtype)

            db = self._conn()
            try:
                await db.execute(
                    "DELETE FROM transcript_segments"
                    " WHERE channel_index = ?"
                    "   AND transcript_id IN"
                    "       (SELECT id FROM transcripts WHERE audio_session_id = ?)",
                    (channel_index, audio_session_id),
                )
                await db.execute(
                    "DELETE FROM channel_map WHERE audio_session_id = ? AND channel_index = ?",
                    (audio_session_id, channel_index),
                )
                detail = json.dumps(
                    {
                        "audio_session_id": audio_session_id,
                        "channel_index": channel_index,
                        "position_name": (
                            await self._channel_position_name(audio_session_id, channel_index)
                        ),
                        "reason": reason,
                    }
                )
                await self.log_action(
                    "audio_channel_delete",
                    detail=detail,
                    user_id=user_id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                os.replace(str(tmp_path), str(wav_path))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        except Exception:
            if tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            raise
        logger.info(
            "Audio channel deleted: session={} channel={}",
            audio_session_id,
            channel_index,
        )

    async def _delete_sibling_by_ordinal(
        self,
        row: dict[str, Any],
        *,
        channel_index: int,
        user_id: int | None,
        reason: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Sibling-mode branch of ``delete_audio_channel`` (#509 chunk 4).

        Resolves the sibling whose ``capture_ordinal`` matches
        ``channel_index`` within the same capture group and atomically
        deletes its ``audio_sessions`` row (cascading to transcripts,
        transcript_segments, and channel_map via FK) plus writes an
        ``audio_channel_delete`` audit entry tagged ``sibling_mode=True``
        in the same transaction. The WAV file is unlinked from disk
        post-commit on a best-effort basis — the DB is the source of
        truth for "is this data gone?" and a stale file after commit is
        a cleanup inconvenience, not a compliance gap.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        group_id = str(row["capture_group_id"])
        siblings = await self.list_capture_group_siblings(group_id)
        target = next((s for s in siblings if int(s["capture_ordinal"]) == channel_index), None)
        if target is None:
            raise ValueError(f"no sibling with capture_ordinal={channel_index} in group {group_id}")
        sibling_id = int(target["id"])
        position_name = await self._channel_position_name(sibling_id, 0)

        # Re-read file_path inside the transaction so a concurrent delete
        # cannot leave us pointing at nothing.
        db = self._conn()
        try:
            cur = await db.execute(
                "SELECT file_path FROM audio_sessions WHERE id = ?", (sibling_id,)
            )
            file_row = await cur.fetchone()
            if file_row is None:
                # Raced with another deletion — nothing to do, still audit.
                file_path: str | None = None
            else:
                file_path = file_row["file_path"]

            # Cascade cleanup (copied from delete_audio_session but without
            # its own commit — the whole thing is one transaction).
            # extraction_runs FK to transcripts lacks CASCADE.
            await db.execute(
                "DELETE FROM extraction_items WHERE extraction_run_id IN"
                " (SELECT er.id FROM extraction_runs er"
                "  JOIN transcripts t ON er.transcript_id = t.id"
                "  WHERE t.audio_session_id = ?)",
                (sibling_id,),
            )
            await db.execute(
                "DELETE FROM extraction_runs WHERE transcript_id IN"
                " (SELECT id FROM transcripts WHERE audio_session_id = ?)",
                (sibling_id,),
            )
            await db.execute("DELETE FROM audio_sessions WHERE id = ?", (sibling_id,))
            # Audit INSERT inside the same transaction so the audit trail
            # and the row deletion commit atomically: a failure here
            # triggers the rollback and leaves the sibling intact.
            await db.execute(
                "INSERT INTO audit_log (ts, user_id, action, detail, ip_address, user_agent)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _datetime.now(UTC).isoformat(),
                    user_id,
                    "audio_channel_delete",
                    json.dumps(
                        {
                            "sibling_mode": True,
                            "capture_group_id": group_id,
                            "channel_index": channel_index,
                            "deleted_audio_session_id": sibling_id,
                            "position_name": position_name,
                            "reason": reason,
                        }
                    ),
                    ip_address,
                    user_agent,
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        # Post-commit file cleanup. By this point the DB commit has
        # succeeded, so the data is "deleted" from the user's perspective
        # regardless of whether the unlink succeeds.
        if file_path:
            p = Path(file_path)
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()
        logger.info(
            "Sibling audio deleted: group={} ordinal={} audio_session_id={}",
            group_id,
            channel_index,
            sibling_id,
        )

    async def _channel_position_name(self, audio_session_id: int, channel_index: int) -> str | None:
        """Look up the stored position name for a channel (may be None)."""
        cur = await self._read_conn().execute(
            "SELECT position_name FROM channel_map"
            " WHERE audio_session_id = ? AND channel_index = ?",
            (audio_session_id, channel_index),
        )
        r = await cur.fetchone()
        return r["position_name"] if r else None

    # ------------------------------------------------------------------
    # User deletion (#195)
    # ------------------------------------------------------------------

    async def delete_user(self, user_id: int) -> None:
        """Anonymize and soft-delete a user.

        Replaces email with deleted_<id>@redacted, clears name and avatar,
        deletes auth sessions, credentials, and invitation references.
        Preserves audit trail with anonymized user_id references.
        """
        db = self._conn()
        anon_email = f"deleted_{user_id}@redacted"
        await db.execute(
            "UPDATE users SET email = ?, name = NULL, avatar_path = NULL WHERE id = ?",
            (anon_email, user_id),
        )
        await db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM user_credentials WHERE user_id = ?", (user_id,))
        await db.execute(
            "UPDATE invitations SET invited_by = NULL WHERE invited_by = ?", (user_id,)
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
        """Get transcript with anonymization applied to segments.

        Anonymization rewrites the raw speaker label + redacts text for
        consent-revoked crew (PII concern). Crew-name substitution is
        *NOT* applied here — it happens client-side via the speaker_map
        the client already receives, so the server can keep the raw
        ``sib0:SPEAKER_01``-style labels visible. That lets click-to-
        assign round-trip cleanly: the label the UI posts back is the
        same key the speaker_map is stored under (#648).
        """
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
            # Also redact the plain text for anonymized speakers
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

    async def set_crew_consent(self, user_id: int, consent_type: str, granted: bool) -> int:
        """Record or update crew consent for a user. Returns the row id."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        if granted:
            cur = await db.execute(
                "INSERT INTO crew_consents (user_id, consent_type, granted, granted_at)"
                " VALUES (?, ?, 1, ?)"
                " ON CONFLICT(user_id, consent_type)"
                " DO UPDATE SET granted = 1, granted_at = excluded.granted_at, revoked_at = NULL",
                (user_id, consent_type, now),
            )
        else:
            cur = await db.execute(
                "INSERT INTO crew_consents"
                " (user_id, consent_type, granted, granted_at, revoked_at)"
                " VALUES (?, ?, 0, ?, ?)"
                " ON CONFLICT(user_id, consent_type)"
                " DO UPDATE SET granted = 0, revoked_at = excluded.revoked_at",
                (user_id, consent_type, now, now),
            )
        await db.commit()
        return cur.lastrowid or 0

    async def get_crew_consents(self, user_id: int | None) -> list[CrewConsent]:
        """Return all consent records for a user."""
        cur = await self._read_conn().execute(
            "SELECT cc.id, cc.user_id, cc.consent_type, cc.granted,"
            "       cc.granted_at, cc.revoked_at, u.name AS user_name"
            " FROM crew_consents cc"
            " LEFT JOIN users u ON u.id = cc.user_id"
            " WHERE cc.user_id = ?",
            (user_id,),
        )
        rows: list[CrewConsent] = [dict(r) for r in await cur.fetchall()]  # type: ignore[misc]
        return rows

    async def list_crew_consents(self) -> list[CrewConsent]:
        """Return all consent records."""
        cur = await self._read_conn().execute(
            "SELECT cc.id, cc.user_id, cc.consent_type, cc.granted,"
            "       cc.granted_at, cc.revoked_at, u.name AS user_name"
            " FROM crew_consents cc"
            " LEFT JOIN users u ON u.id = cc.user_id"
            " ORDER BY u.name, cc.consent_type"
        )
        rows: list[CrewConsent] = [dict(r) for r in await cur.fetchall()]  # type: ignore[misc]
        return rows

    async def anonymize_sailor(self, user_id: int, replacement: str = "Anonymous") -> int:
        """Anonymize a crew member by updating their user name. Returns rows updated."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE users SET name = ? WHERE id = ?",
            (replacement, user_id),
        )
        count = cur.rowcount or 0
        await db.commit()
        logger.info("User {} anonymized to {!r}", user_id, replacement)
        return count

    # ------------------------------------------------------------------
    # Speaker crew assignment (#443)
    # ------------------------------------------------------------------

    async def assign_speaker_crew(
        self, transcript_id: int, speaker_label: str, user_id: int, name: str
    ) -> bool:
        """Assign a speaker label to a crew member. Returns True if transcript was found."""
        db = self._conn()
        cur = await db.execute("SELECT speaker_map FROM transcripts WHERE id = ?", (transcript_id,))
        row = await cur.fetchone()
        if row is None:
            return False
        existing: dict[str, Any] = json.loads(row["speaker_map"] or "{}")
        existing[speaker_label] = {"type": "crew", "user_id": user_id, "name": name}
        await db.execute(
            "UPDATE transcripts SET speaker_map = ? WHERE id = ?",
            (json.dumps(existing), transcript_id),
        )
        await db.commit()
        logger.info(
            "Speaker {} assigned to user {} ({}) in transcript {}",
            speaker_label,
            user_id,
            name,
            transcript_id,
        )
        return True

    async def get_speaker_map(self, transcript_id: int) -> dict[str, Any]:
        """Return the speaker_map for a transcript (empty dict if none)."""
        cur = await self._read_conn().execute(
            "SELECT speaker_map FROM transcripts WHERE id = ?", (transcript_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return {}
        result: dict[str, Any] = json.loads(row["speaker_map"] or "{}")
        return result

    # ------------------------------------------------------------------
    # Voice profiles (#443)
    # ------------------------------------------------------------------

    async def upsert_voice_profile(
        self,
        user_id: int,
        embedding: bytes,
        segment_count: int,
        session_count: int,
    ) -> int:
        """Insert or update a voice profile. Returns the row id."""
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        now = _datetime.now(_UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO crew_voice_profiles"
            " (user_id, embedding, segment_count, session_count, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id)"
            " DO UPDATE SET embedding = excluded.embedding,"
            " segment_count = excluded.segment_count,"
            " session_count = excluded.session_count,"
            " updated_at = excluded.updated_at",
            (user_id, embedding, segment_count, session_count, now, now),
        )
        await db.commit()
        return cur.lastrowid or 0

    async def get_voice_profile(self, user_id: int) -> dict[str, Any] | None:
        """Return the voice profile for a user, or None."""
        cur = await self._read_conn().execute(
            "SELECT id, user_id, embedding, segment_count, session_count,"
            " created_at, updated_at"
            " FROM crew_voice_profiles WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_voice_profile(self, user_id: int) -> bool:
        """Delete a voice profile. Returns True if found and deleted."""
        db = self._conn()
        cur = await db.execute("DELETE FROM crew_voice_profiles WHERE user_id = ?", (user_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def revoke_voice_profile_consent(self, user_id: int) -> None:
        """Revoke voice_profile consent and hard-delete all related data.

        Deletes: voice profile, all 'auto' speaker_map entries referencing this user.
        Preserves: manual 'crew' speaker_map entries (crew metadata, not biometric).
        """
        db = self._conn()
        # 1. Delete the voice profile
        await db.execute("DELETE FROM crew_voice_profiles WHERE user_id = ?", (user_id,))
        # 2. Remove 'auto' entries from speaker_map in all transcripts
        cur = await db.execute(
            "SELECT id, speaker_map FROM transcripts WHERE speaker_map IS NOT NULL"
        )
        rows = await cur.fetchall()
        for row in rows:
            smap: dict[str, Any] = json.loads(row["speaker_map"] or "{}")
            changed = False
            to_remove = []
            for label, entry in smap.items():
                if (
                    isinstance(entry, dict)
                    and entry.get("type") == "auto"
                    and entry.get("user_id") == user_id
                ):
                    to_remove.append(label)
                    changed = True
            for label in to_remove:
                del smap[label]
            if changed:
                await db.execute(
                    "UPDATE transcripts SET speaker_map = ? WHERE id = ?",
                    (json.dumps(smap), row["id"]),
                )
        # 3. Revoke the consent
        await self.set_crew_consent(user_id, "voice_profile", False)
        await db.commit()
        logger.info("Voice profile consent revoked for user {}, data deleted", user_id)

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
        db = self._read_conn()
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
        db = self._read_conn()
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
        cur = await self._read_conn().execute(
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
        cur = await self._read_conn().execute(
            "SELECT id, co_op_id, co_op_name, co_op_pub, membership_json,"
            " role, joined_at, status"
            " FROM co_op_memberships ORDER BY joined_at"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_co_op_membership(self, co_op_id: str) -> dict[str, Any] | None:
        """Return a specific co-op membership, or None."""
        cur = await self._read_conn().execute(
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
        cur = await self._read_conn().execute(
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
        cur = await self._read_conn().execute(
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
        cur = await self._read_conn().execute(
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
        cur = await self._read_conn().execute(
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
    # Boat settings
    # ------------------------------------------------------------------

    async def create_boat_settings(
        self,
        race_id: int | None,
        entries: list[dict[str, str]],
        source: str,
        extraction_run_id: int | None = None,
    ) -> list[int]:
        """Insert one or more boat setting entries.

        Each entry must have ``ts``, ``parameter``, and ``value`` keys.
        Returns a list of inserted row IDs.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        # Validate against DB-backed controls table
        valid_names = await self.get_control_names()
        now = _datetime.now(UTC).isoformat()
        ids: list[int] = []
        for entry in entries:
            param = entry["parameter"]
            if param not in valid_names:
                raise ValueError(f"Unknown parameter: {param!r}")
            cur = await db.execute(
                "INSERT INTO boat_settings"
                " (race_id, ts, parameter, value, source, extraction_run_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (race_id, entry["ts"], param, entry["value"], source, extraction_run_id, now),
            )
            ids.append(cur.lastrowid or 0)
        await db.commit()
        # Hot-reload the cached leeway coefficient if it was just written —
        # admin tuning takes effect on the live broadcast immediately
        # without a service restart (#730 follow-up).
        if any(e["parameter"] == "leeway_coefficient" for e in entries):
            await self.refresh_leeway_k()
        if any(e["parameter"] in ("compass_offset_port", "compass_offset_stbd") for e in entries):
            await self.refresh_compass_offsets()
        return ids

    async def list_boat_settings(self, race_id: int | None) -> list[dict[str, Any]]:
        """Return all boat settings for a race, ordered by timestamp."""
        db = self._read_conn()
        params: tuple[Any, ...]
        if race_id is None:
            where, params = "race_id IS NULL", ()
        else:
            where, params = "race_id = ?", (race_id,)
        cur = await db.execute(
            "SELECT id, race_id, ts, parameter, value, source, extraction_run_id, created_at"
            f" FROM boat_settings WHERE {where} ORDER BY ts, id",
            params,
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def current_boat_settings(self, race_id: int | None) -> list[dict[str, Any]]:
        """Return the latest value for each parameter in a race.

        Uses a window function to pick the most recent entry per parameter.
        """
        db = self._read_conn()
        params: tuple[Any, ...]
        if race_id is None:
            where, params = "race_id IS NULL", ()
        else:
            where, params = "race_id = ?", (race_id,)
        cur = await db.execute(
            "SELECT id, race_id, ts, parameter, value, source, extraction_run_id, created_at"
            " FROM boat_settings"
            f" WHERE {where} AND id IN ("
            "   SELECT id FROM ("
            "     SELECT id, ROW_NUMBER() OVER (PARTITION BY parameter ORDER BY ts DESC, id DESC)"
            "       AS rn"
            f"     FROM boat_settings WHERE {where}"
            "   ) WHERE rn = 1"
            " ) ORDER BY parameter",
            params + params,
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def resolve_boat_settings(
        self,
        race_id: int,
        as_of: str,
    ) -> list[dict[str, Any]]:
        """Resolve boat settings at a specific timestamp with override tracking.

        Returns the latest value per parameter at or before *as_of*, merging
        race-specific (``race_id``) settings over boat-level (``race_id IS NULL``)
        defaults.  When a race-specific value overrides a boat-level default the
        result includes the superseded value so the UI can annotate the change.
        """
        db = self._conn()

        async def _latest_per_param(
            where: str,
            params: tuple[object, ...],
        ) -> dict[str, dict[str, Any]]:
            cur = await db.execute(
                "SELECT id, race_id, ts, parameter, value, source,"
                "       extraction_run_id, created_at"
                " FROM boat_settings"
                f" WHERE {where} AND ts <= ?"
                "   AND id IN ("
                "     SELECT id FROM ("
                "       SELECT id, ROW_NUMBER() OVER"
                "         (PARTITION BY parameter ORDER BY ts DESC, id DESC) AS rn"
                f"       FROM boat_settings WHERE {where} AND ts <= ?"
                "     ) WHERE rn = 1"
                "   ) ORDER BY parameter",
                params + (as_of,) + params + (as_of,),
            )
            rows = await cur.fetchall()
            return {row["parameter"]: dict(row) for row in rows}

        boat_level = await _latest_per_param("race_id IS NULL", ())
        race_level = await _latest_per_param("race_id = ?", (race_id,))

        # Merge: race-specific wins; track what it supersedes
        all_names = await self.get_control_names()

        result: list[dict[str, Any]] = []
        for param in sorted(all_names):
            race_row = race_level.get(param)
            boat_row = boat_level.get(param)
            if race_row:
                entry = dict(race_row)
                if boat_row and boat_row["value"] != race_row["value"]:
                    entry["supersedes_value"] = boat_row["value"]
                    entry["supersedes_source"] = boat_row["source"]
                else:
                    entry["supersedes_value"] = None
                    entry["supersedes_source"] = None
                result.append(entry)
            elif boat_row:
                entry = dict(boat_row)
                entry["supersedes_value"] = None
                entry["supersedes_source"] = None
                result.append(entry)
        return result

    async def delete_boat_settings_extraction_run(self, extraction_run_id: int) -> int:
        """Delete all boat settings from a specific extraction run.

        Returns the number of deleted rows.
        """
        db = self._conn()
        cur = await db.execute(
            "DELETE FROM boat_settings WHERE extraction_run_id = ?",
            (extraction_run_id,),
        )
        await db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Comments + read state on moments (#662, was threaded comments #282)
    # ------------------------------------------------------------------

    async def _assert_moment_anchor_in_session(
        self, anchor_kind: str, anchor_entity_id: int | None, session_id: int
    ) -> None:
        """Entity-ref anchor kinds must scope to the moment's session."""
        if anchor_entity_id is None or anchor_kind in {"session", "timestamp"}:
            return
        if anchor_kind == "maneuver":
            cur = await self._read_conn().execute(
                "SELECT 1 FROM maneuvers WHERE id = ? AND session_id = ?",
                (anchor_entity_id, session_id),
            )
            if await cur.fetchone() is None:
                raise AnchorScopeError(
                    f"maneuver {anchor_entity_id} is not part of session {session_id}"
                )
            return
        if anchor_kind == "transcript_segment":
            cur = await self._read_conn().execute(
                "SELECT ts.id FROM transcript_segments ts"
                " JOIN transcripts t ON ts.transcript_id = t.id"
                " JOIN audio_sessions a ON t.audio_session_id = a.id"
                " WHERE ts.id = ? AND a.race_id = ?",
                (anchor_entity_id, session_id),
            )
            if await cur.fetchone() is None:
                raise AnchorScopeError(
                    f"transcript_segment {anchor_entity_id} is not part of session {session_id}"
                )
            return

    async def create_comment(
        self,
        moment_id: int,
        author: int,
        body: str,
    ) -> int:
        """Add a comment to a moment. Returns the comment ID."""
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO comments (moment_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (moment_id, author, body, now),
        )
        # Touch moment updated_at
        await db.execute(
            "UPDATE moments SET updated_at = ? WHERE id = ?",
            (now, moment_id),
        )
        await db.commit()
        return cur.lastrowid or 0

    async def update_comment(self, comment_id: int, body: str) -> bool:
        """Edit a comment body. Returns True if found."""
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "UPDATE comments SET body = ?, edited_at = ? WHERE id = ?",
            (body, now, comment_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def delete_comment(self, comment_id: int) -> bool:
        """Delete a comment. Returns True if found."""
        db = self._conn()
        cur = await db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        await db.commit()
        return cur.rowcount > 0

    async def get_comment(self, comment_id: int) -> dict[str, Any] | None:
        """Return a single comment row."""
        cur = await self._read_conn().execute(
            "SELECT id, moment_id, author, body, created_at, edited_at FROM comments WHERE id = ?",
            (comment_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def mark_moment_read(self, moment_id: int, user_id: int) -> None:
        """Update the read-state for a user on a moment."""
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO comment_read_state (user_id, moment_id, last_read)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, moment_id) DO UPDATE SET last_read = excluded.last_read",
            (user_id, moment_id, now),
        )
        await db.commit()

    async def redact_comment_author(self, user_id: int) -> int:
        """Redact a user's attribution from all comments.

        Replaces the author with NULL so the UI shows 'Crew Member'.
        Returns the number of comments redacted.
        """
        db = self._conn()
        cur = await db.execute(
            "UPDATE comments SET author = NULL WHERE author = ?",
            (user_id,),
        )
        await db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Analysis cache (#283)
    # ------------------------------------------------------------------

    async def get_analysis_cache(self, session_id: int, plugin_name: str) -> dict[str, Any] | None:
        """Return the cached analysis result or None."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT plugin_version, data_hash, result_json, created_at, stale_reason"
            " FROM analysis_cache WHERE session_id = ? AND plugin_name = ?",
            (session_id, plugin_name),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_analysis_cache(
        self,
        session_id: int,
        plugin_name: str,
        plugin_version: str,
        data_hash: str,
        result_json: str,
    ) -> None:
        """Insert or replace a cached analysis result."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO analysis_cache"
            " (session_id, plugin_name, plugin_version, data_hash, result_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id, plugin_name) DO UPDATE SET"
            "   plugin_version = excluded.plugin_version,"
            "   data_hash = excluded.data_hash,"
            "   result_json = excluded.result_json,"
            "   created_at = excluded.created_at,"
            "   stale_reason = NULL",  # fresh result clears staleness (#285)
            (session_id, plugin_name, plugin_version, data_hash, result_json, now),
        )
        await db.commit()

    async def invalidate_analysis_cache(self, session_id: int) -> None:
        """Remove all cached analysis results for a session."""
        db = self._conn()
        await db.execute("DELETE FROM analysis_cache WHERE session_id = ?", (session_id,))
        await db.commit()

    # ------------------------------------------------------------------
    # Analysis preferences (#283)
    # ------------------------------------------------------------------

    async def get_analysis_preference(
        self, scope: str, scope_id: str | None
    ) -> dict[str, Any] | None:
        """Return the preference row for a scope, or None."""
        db = self._conn()
        if scope_id is None:
            cur = await db.execute(
                "SELECT scope, scope_id, model_name, updated_at"
                " FROM analysis_preferences WHERE scope = ? AND scope_id IS NULL",
                (scope,),
            )
        else:
            cur = await db.execute(
                "SELECT scope, scope_id, model_name, updated_at"
                " FROM analysis_preferences WHERE scope = ? AND scope_id = ?",
                (scope, scope_id),
            )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_analysis_preference(
        self, scope: str, scope_id: str | None, model_name: str
    ) -> None:
        """Set or update the preferred analysis model at the given scope."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO analysis_preferences (scope, scope_id, model_name, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(scope, scope_id) DO UPDATE SET"
            "   model_name = excluded.model_name, updated_at = excluded.updated_at",
            (scope, scope_id, model_name, now),
        )
        await db.commit()

    async def resolve_analysis_preference(
        self, user_id: int, co_op_id: str | None = None
    ) -> str | None:
        """Walk platform → co_op → boat → user and return model_name."""
        checks: list[tuple[str, str | None]] = [
            ("user", str(user_id)),
            ("boat", None),
        ]
        if co_op_id:
            checks.append(("co_op", co_op_id))
        checks.append(("platform", None))

        for scope, sid in checks:
            pref = await self.get_analysis_preference(scope, sid)
            if pref is not None:
                return pref["model_name"]  # type: ignore[no-any-return]
        return None

    # ------------------------------------------------------------------
    # Analysis catalog (#285)
    # ------------------------------------------------------------------

    async def get_catalog_entry(self, plugin_name: str, co_op_id: str) -> dict[str, Any] | None:
        """Return the catalog entry for (plugin_name, co_op_id), or None."""
        cur = await self._conn().execute(
            "SELECT plugin_name, co_op_id, state, proposing_boat, version, author,"
            " changelog, proposed_at, resolved_at, reject_reason, data_license_gate_passed"
            " FROM analysis_catalog WHERE plugin_name = ? AND co_op_id = ?",
            (plugin_name, co_op_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_catalog_entry(
        self,
        plugin_name: str,
        co_op_id: str,
        state: str,
        proposing_boat: str | None,
        version: str | None,
        author: str | None,
        changelog: str | None,
        proposed_at: str | None,
        resolved_at: str | None,
        reject_reason: str | None,
        data_license_gate_passed: int,
    ) -> None:
        """Insert or replace a catalog entry."""
        from datetime import UTC  # noqa: PLC0415
        from datetime import datetime as _datetime  # noqa: PLC0415

        now = proposed_at or _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO analysis_catalog"
            " (plugin_name, co_op_id, state, proposing_boat, version, author,"
            "  changelog, proposed_at, resolved_at, reject_reason, data_license_gate_passed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(plugin_name, co_op_id) DO UPDATE SET"
            "   state = excluded.state,"
            "   proposing_boat = excluded.proposing_boat,"
            "   version = excluded.version,"
            "   author = excluded.author,"
            "   changelog = excluded.changelog,"
            "   proposed_at = excluded.proposed_at,"
            "   resolved_at = excluded.resolved_at,"
            "   reject_reason = excluded.reject_reason,"
            "   data_license_gate_passed = excluded.data_license_gate_passed",
            (
                plugin_name,
                co_op_id,
                state,
                proposing_boat,
                version,
                author,
                changelog,
                now,
                resolved_at,
                reject_reason,
                data_license_gate_passed,
            ),
        )
        await db.commit()

    async def list_catalog_entries(self, co_op_id: str) -> list[dict[str, Any]]:
        """Return all catalog entries for a co-op, ordered by state then name."""
        cur = await self._conn().execute(
            "SELECT plugin_name, co_op_id, state, proposing_boat, version, author,"
            " changelog, proposed_at, resolved_at, reject_reason, data_license_gate_passed"
            " FROM analysis_catalog WHERE co_op_id = ?"
            " ORDER BY state, plugin_name",
            (co_op_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def clear_co_op_default(self, co_op_id: str) -> None:
        """Revert any co_op_default plugin in this co-op back to co_op_active."""
        db = self._conn()
        await db.execute(
            "UPDATE analysis_catalog SET state = 'co_op_active'"
            " WHERE co_op_id = ? AND state = 'co_op_default'",
            (co_op_id,),
        )
        await db.commit()

    async def mark_plugin_cache_stale(self, plugin_name: str, current_version: str) -> int:
        """Mark analysis_cache rows for *plugin_name* stale where version != current_version.

        Returns the number of rows updated.
        """
        db = self._conn()
        cur = await db.execute(
            "UPDATE analysis_cache SET stale_reason = 'version_change'"
            " WHERE plugin_name = ? AND plugin_version != ? AND stale_reason IS NULL",
            (plugin_name, current_version),
        )
        await db.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Visualization preferences (#286)
    # ------------------------------------------------------------------

    async def get_viz_preference(self, scope: str, scope_id: str | None) -> dict[str, Any] | None:
        """Return the visualization preference row for a scope, or None."""
        db = self._read_conn()
        if scope_id is None:
            cur = await db.execute(
                "SELECT scope, scope_id, plugin_names, updated_at"
                " FROM visualization_preferences WHERE scope = ? AND scope_id IS NULL",
                (scope,),
            )
        else:
            cur = await db.execute(
                "SELECT scope, scope_id, plugin_names, updated_at"
                " FROM visualization_preferences WHERE scope = ? AND scope_id = ?",
                (scope, scope_id),
            )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_viz_preference(self, scope: str, scope_id: str | None, plugin_names: str) -> None:
        """Set or update the preferred visualization plugins at the given scope."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO visualization_preferences (scope, scope_id, plugin_names, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(scope, scope_id) DO UPDATE SET"
            "   plugin_names = excluded.plugin_names, updated_at = excluded.updated_at",
            (scope, scope_id, plugin_names, now),
        )
        await db.commit()

    async def get_viz_selection(self, user_id: int, session_id: int) -> dict[str, Any] | None:
        """Return the visualization selection for a user+session, or None."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT user_id, session_id, plugin_names, updated_at"
            " FROM visualization_selections WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_viz_selection(self, user_id: int, session_id: int, plugin_names: str) -> None:
        """Set or update the active visualization set for a user+session."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO visualization_selections (user_id, session_id, plugin_names, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id, session_id) DO UPDATE SET"
            "   plugin_names = excluded.plugin_names, updated_at = excluded.updated_at",
            (user_id, session_id, plugin_names, now),
        )
        await db.commit()

    # ------------------------------------------------------------------
    # Sail active ranges (#309)
    # ------------------------------------------------------------------

    async def get_sail_active_ranges(
        self,
        *,
        sail_id: int | None = None,
        sail_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return sail active ranges from sail_changes.

        Each row is (session_id, sail_id, sail_name, sail_type, start_ts, end_ts).
        """
        db = self._conn()
        # Build query that joins sail_changes with sails and races
        # We need to determine the active range for each sail in each session
        conditions: list[str] = []
        params: list[Any] = []

        base = (
            "SELECT sc.race_id AS session_id, s.id AS sail_id, s.name AS sail_name,"
            " s.type AS sail_type, sc.ts AS start_ts, r.end_utc AS end_ts"
            " FROM sail_changes sc"
            " JOIN races r ON r.id = sc.race_id"
            " JOIN sails s ON s.id IN (sc.main_id, sc.jib_id, sc.spinnaker_id)"
            " WHERE r.end_utc IS NOT NULL"
        )

        if sail_id is not None:
            conditions.append("s.id = ?")
            params.append(sail_id)
        if sail_type is not None:
            conditions.append("s.type = ?")
            params.append(sail_type)
        if start_date is not None:
            conditions.append("r.start_utc >= ?")
            params.append(start_date)
        if end_date is not None:
            conditions.append("r.end_utc <= ?")
            params.append(end_date)

        if conditions:
            base += " AND " + " AND ".join(conditions)

        base += " ORDER BY sc.ts"
        cur = await db.execute(base, params)
        return [dict(row) for row in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Notifications (#284)
    # ------------------------------------------------------------------

    async def create_notification(
        self,
        user_id: int,
        type: str,
        *,
        source_moment_id: int | None = None,
        source_comment_id: int | None = None,
        session_id: int | None = None,
        actor_id: int | None = None,
        message: str | None = None,
    ) -> int:
        """Insert a notification. Returns the row id."""
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO notifications"
            " (user_id, type, source_moment_id, source_comment_id,"
            "  session_id, actor_id, message, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                type,
                source_moment_id,
                source_comment_id,
                session_id,
                actor_id,
                message,
                now,
            ),
        )
        await db.commit()
        return cur.lastrowid or 0

    async def get_notifications(
        self,
        user_id: int,
        *,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return notifications for a user."""
        db = self._conn()
        where = "WHERE n.user_id = ? AND n.dismissed = 0"
        if unread_only:
            where += " AND n.read = 0"
        cur = await db.execute(
            f"SELECT n.id, n.user_id, n.type, n.source_moment_id, n.source_comment_id,"
            f" n.session_id, n.actor_id, n.message, n.created_at, n.read, n.dismissed,"
            f" u.name AS actor_name, u.email AS actor_email,"
            f" r.name AS session_name,"
            f" c.body AS comment_body,"
            f" m.subject AS moment_subject"
            f" FROM notifications n"
            f" LEFT JOIN users u ON n.actor_id = u.id"
            f" LEFT JOIN races r ON n.session_id = r.id"
            f" LEFT JOIN comments c ON n.source_comment_id = c.id"
            f" LEFT JOIN moments m ON n.source_moment_id = m.id"
            f" {where}"
            f" ORDER BY n.created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_notification_count(self, user_id: int) -> dict[str, int]:
        """Return unread and mention counts."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT"
            " SUM(CASE WHEN read = 0 AND dismissed = 0 THEN 1 ELSE 0 END) AS unread,"
            " SUM(CASE WHEN read = 0 AND dismissed = 0 AND type = 'mention' THEN 1 ELSE 0 END)"
            "   AS mentions"
            " FROM notifications WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return {"unread": 0, "mentions": 0}
        return {"unread": int(row["unread"] or 0), "mentions": int(row["mentions"] or 0)}

    async def mark_notification_read(self, notification_id: int, user_id: int) -> bool:
        """Mark a notification as read. Returns True if updated."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE notifications SET read = 1 WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def mark_all_notifications_read(self, user_id: int) -> int:
        """Mark all notifications as read for a user. Returns count updated."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE notifications SET read = 1 WHERE user_id = ? AND read = 0",
            (user_id,),
        )
        await db.commit()
        return cur.rowcount

    async def dismiss_notification(self, notification_id: int, user_id: int) -> bool:
        """Dismiss a notification. Returns True if updated."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE notifications SET dismissed = 1 WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0

    async def get_notification_preferences(self, user_id: int) -> list[dict[str, Any]]:
        """Return all notification preferences for a user."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, user_id, scope, type, channel, enabled, frequency, updated_at"
            " FROM notification_preferences WHERE user_id = ?",
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def set_notification_preference(
        self,
        user_id: int,
        scope: str,
        type: str,
        channel: str,
        *,
        enabled: bool = True,
        frequency: str = "immediate",
    ) -> None:
        """Set or update a notification preference."""
        from datetime import UTC
        from datetime import datetime as _datetime

        now = _datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO notification_preferences"
            " (user_id, scope, type, channel, enabled, frequency, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, scope, type, channel) DO UPDATE SET"
            "   enabled = excluded.enabled, frequency = excluded.frequency,"
            "   updated_at = excluded.updated_at",
            (user_id, scope, type, channel, int(enabled), frequency, now),
        )
        await db.commit()

    async def get_users_for_notification(
        self, session_id: int, notif_type: str
    ) -> list[dict[str, Any]]:
        """Return users who should receive a notification for this session.

        Returns all users by default (platform channel is always on).
        Users who have explicitly disabled this type are excluded.
        """
        db = self._conn()
        cur = await db.execute(
            "SELECT u.id, u.email, u.name FROM users u"
            " WHERE u.id NOT IN ("
            "   SELECT np.user_id FROM notification_preferences np"
            "   WHERE np.type = ? AND np.channel = 'platform' AND np.enabled = 0"
            " )",
            (notif_type,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def cascade_crew_redaction_to_notifications(self, user_id: int) -> int:
        """Nullify actor and scrub message for a redacted user's notifications."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE notifications SET actor_id = NULL, message = NULL WHERE actor_id = ?",
            (user_id,),
        )
        await db.commit()
        return cur.rowcount

    async def resolve_user_names(self, names: list[str]) -> dict[str, int]:
        """Resolve display names to user IDs for @mention resolution."""
        if not names:
            return {}
        db = self._read_conn()
        placeholders = ",".join("?" * len(names))
        cur = await db.execute(
            f"SELECT id, name FROM users WHERE name IN ({placeholders})",
            names,
        )
        return {str(row["name"]): int(row["id"]) for row in await cur.fetchall()}

    # ------------------------------------------------------------------
    # Control categories (#425)
    # ------------------------------------------------------------------

    async def list_control_categories(self) -> list[dict[str, Any]]:
        """Return all categories ordered by sort_order."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, label, sort_order FROM control_categories ORDER BY sort_order, name"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def add_control_category(self, name: str, label: str, sort_order: int = 0) -> int:
        """Add a category. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO control_categories (name, label, sort_order) VALUES (?, ?, ?)",
            (name, label, sort_order),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def update_control_category(
        self,
        category_id: int,
        *,
        name: str | None = None,
        label: str | None = None,
        sort_order: int | None = None,
    ) -> bool:
        """Update a category. Returns True if found."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE control_categories SET"
            " name = COALESCE(?, name),"
            " label = COALESCE(?, label),"
            " sort_order = COALESCE(?, sort_order)"
            " WHERE id = ?",
            (name, label, sort_order, category_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_control_category(self, category_id: int) -> bool:
        """Delete a category. Returns True if found."""
        db = self._conn()
        cur = await db.execute("DELETE FROM control_categories WHERE id = ?", (category_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Unified controls (#425)
    # ------------------------------------------------------------------

    async def list_controls(self) -> list[dict[str, Any]]:
        """Return all controls with ArUco config and trigger words."""
        import json as _json

        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, label, unit, input_type, category,"
            " sort_order, preset_values, created_at"
            " FROM controls ORDER BY sort_order, name"
        )
        controls = [dict(r) for r in await cur.fetchall()]

        # Attach ArUco config
        cur = await db.execute(
            "SELECT control_id, camera_id, marker_id_a, marker_id_b, tolerance_mm"
            " FROM control_aruco"
        )
        aruco_map = {r["control_id"]: dict(r) for r in await cur.fetchall()}

        # Attach trigger words
        cur = await db.execute(
            "SELECT id, control_id, phrase FROM control_trigger_words ORDER BY phrase"
        )
        tw_map: dict[int, list[dict[str, Any]]] = {}
        for r in await cur.fetchall():
            tw_map.setdefault(r["control_id"], []).append({"id": r["id"], "phrase": r["phrase"]})

        # Attach camera names for ArUco controls
        cur = await db.execute("SELECT id, name FROM aruco_cameras")
        cam_names = {r["id"]: r["name"] for r in await cur.fetchall()}

        for ctrl in controls:
            cid = ctrl["id"]
            aruco = aruco_map.get(cid)
            if aruco:
                aruco["camera_name"] = cam_names.get(aruco["camera_id"], "")
            ctrl["aruco"] = aruco
            ctrl["trigger_words"] = tw_map.get(cid, [])
            if ctrl["preset_values"]:
                import contextlib

                with contextlib.suppress(_json.JSONDecodeError, TypeError):
                    ctrl["preset_values"] = _json.loads(ctrl["preset_values"])
        return controls

    async def get_control_by_name(self, name: str) -> dict[str, Any] | None:
        """Return a single control by canonical name."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, label, unit, input_type, category,"
            " sort_order, preset_values, created_at"
            " FROM controls WHERE name = ?",
            (name,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_control(
        self,
        name: str,
        label: str,
        unit: str = "",
        input_type: str = "number",
        category: str = "sail_controls",
        sort_order: int = 0,
        preset_values: str | None = None,
    ) -> int:
        """Add a control. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO controls"
            " (name, label, unit, input_type, category, sort_order, preset_values)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, label, unit, input_type, category, sort_order, preset_values),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info("Control added: id={} name={}", cur.lastrowid, name)
        return cur.lastrowid

    async def update_control(
        self,
        control_id: int,
        *,
        name: str | None = None,
        label: str | None = None,
        unit: str | None = None,
        input_type: str | None = None,
        category: str | None = None,
        sort_order: int | None = None,
    ) -> bool:
        """Update a control. Returns True if found."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE controls SET"
            " name = COALESCE(?, name),"
            " label = COALESCE(?, label),"
            " unit = COALESCE(?, unit),"
            " input_type = COALESCE(?, input_type),"
            " category = COALESCE(?, category),"
            " sort_order = COALESCE(?, sort_order)"
            " WHERE id = ?",
            (name, label, unit, input_type, category, sort_order, control_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_control(self, control_id: int) -> bool:
        """Delete a control (cascades to aruco, trigger words, measurements)."""
        db = self._conn()
        cur = await db.execute("DELETE FROM controls WHERE id = ?", (control_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def set_control_aruco(
        self,
        control_id: int,
        camera_id: int,
        marker_id_a: int,
        marker_id_b: int,
        tolerance_mm: float | None = None,
    ) -> None:
        """Attach or update ArUco marker config for a control."""
        db = self._conn()
        await db.execute(
            "INSERT INTO control_aruco"
            " (control_id, camera_id, marker_id_a, marker_id_b, tolerance_mm)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(control_id) DO UPDATE SET"
            " camera_id = excluded.camera_id,"
            " marker_id_a = excluded.marker_id_a,"
            " marker_id_b = excluded.marker_id_b,"
            " tolerance_mm = excluded.tolerance_mm",
            (control_id, camera_id, marker_id_a, marker_id_b, tolerance_mm),
        )
        await db.commit()

    async def delete_control_aruco(self, control_id: int) -> bool:
        """Remove ArUco marker config from a control."""
        db = self._conn()
        cur = await db.execute("DELETE FROM control_aruco WHERE control_id = ?", (control_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def list_control_trigger_words(
        self, control_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return trigger words, optionally filtered by control."""
        db = self._read_conn()
        if control_id is not None:
            cur = await db.execute(
                "SELECT tw.id, tw.control_id, tw.phrase, c.name AS control_name"
                " FROM control_trigger_words tw"
                " JOIN controls c ON c.id = tw.control_id"
                " WHERE tw.control_id = ? ORDER BY tw.phrase",
                (control_id,),
            )
        else:
            cur = await db.execute(
                "SELECT tw.id, tw.control_id, tw.phrase, c.name AS control_name"
                " FROM control_trigger_words tw"
                " JOIN controls c ON c.id = tw.control_id"
                " ORDER BY tw.phrase"
            )
        return [dict(r) for r in await cur.fetchall()]

    async def add_control_trigger_word(self, control_id: int, phrase: str) -> int:
        """Add a trigger word to a control. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO control_trigger_words (control_id, phrase) VALUES (?, ?)",
            (control_id, phrase),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def delete_control_trigger_word(self, trigger_id: int) -> bool:
        """Delete a trigger word."""
        db = self._conn()
        cur = await db.execute("DELETE FROM control_trigger_words WHERE id = ?", (trigger_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def get_latest_camera_reading(self, parameter: str) -> dict[str, Any] | None:
        """Return the latest boat_settings entry with source='camera' for a parameter."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, race_id, ts, parameter, value, source, created_at"
            " FROM boat_settings WHERE parameter = ? AND source = 'camera'"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (parameter,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_control_names(self) -> frozenset[str]:
        """Return the set of all control names (for validation)."""
        db = self._read_conn()
        cur = await db.execute("SELECT name FROM controls")
        return frozenset(r["name"] for r in await cur.fetchall())

    async def controls_with_aruco(self) -> list[dict[str, Any]]:
        """Return controls that have ArUco marker config, with camera info."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT c.id, c.name, ca.camera_id, ca.marker_id_a, ca.marker_id_b,"
            " ca.tolerance_mm, cam.name AS camera_name, cam.ip AS camera_ip,"
            " cam.marker_size_mm, cam.calibration, cam.retain_images"
            " FROM controls c"
            " JOIN control_aruco ca ON ca.control_id = c.id"
            " JOIN aruco_cameras cam ON cam.id = ca.camera_id"
            " ORDER BY c.name"
        )
        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # ArUco marker tracking (#425)
    # ------------------------------------------------------------------

    async def list_aruco_cameras(self) -> list[dict[str, Any]]:
        """Return all ArUco cameras ordered by name."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, ip, marker_size_mm, capture_interval_s,"
            " retain_images, calibration, calibration_state, created_at"
            " FROM aruco_cameras ORDER BY name ASC"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_aruco_camera(self, camera_id: int) -> dict[str, Any] | None:
        """Return a single ArUco camera by id."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, ip, marker_size_mm, capture_interval_s,"
            " retain_images, calibration, calibration_state, created_at"
            " FROM aruco_cameras WHERE id = ?",
            (camera_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_aruco_camera_by_name(self, name: str) -> dict[str, Any] | None:
        """Return a single ArUco camera by name."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, ip, marker_size_mm, capture_interval_s,"
            " retain_images, calibration, calibration_state, created_at"
            " FROM aruco_cameras WHERE name = ?",
            (name,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_aruco_camera(
        self,
        name: str,
        ip: str,
        marker_size_mm: float = 50.0,
        capture_interval_s: int = 60,
        retain_images: bool = False,
    ) -> int:
        """Add an ArUco camera. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO aruco_cameras"
            " (name, ip, marker_size_mm, capture_interval_s, retain_images)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, ip, marker_size_mm, capture_interval_s, int(retain_images)),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info("ArUco camera added: id={} name={} ip={}", cur.lastrowid, name, ip)
        return cur.lastrowid

    async def update_aruco_camera(
        self,
        camera_id: int,
        *,
        name: str | None = None,
        ip: str | None = None,
        marker_size_mm: float | None = None,
        capture_interval_s: int | None = None,
        retain_images: bool | None = None,
    ) -> bool:
        """Update an ArUco camera. Returns True if found."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE aruco_cameras SET"
            " name = COALESCE(?, name),"
            " ip = COALESCE(?, ip),"
            " marker_size_mm = COALESCE(?, marker_size_mm),"
            " capture_interval_s = COALESCE(?, capture_interval_s),"
            " retain_images = COALESCE(?, retain_images)"
            " WHERE id = ?",
            (
                name,
                ip,
                marker_size_mm,
                capture_interval_s,
                int(retain_images) if retain_images is not None else None,
                camera_id,
            ),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_aruco_camera(self, camera_id: int) -> bool:
        """Delete an ArUco camera and cascade to profiles/controls/measurements."""
        db = self._conn()
        cur = await db.execute("DELETE FROM aruco_cameras WHERE id = ?", (camera_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def update_aruco_calibration(
        self, camera_id: int, calibration_json: str, state: str
    ) -> bool:
        """Update calibration data and state for a camera."""
        db = self._conn()
        cur = await db.execute(
            "UPDATE aruco_cameras SET calibration = ?, calibration_state = ? WHERE id = ?",
            (calibration_json, state, camera_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    # -- Camera profiles --

    async def list_aruco_profiles(self, camera_id: int) -> list[dict[str, Any]]:
        """Return all profiles for a camera."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, camera_id, name, settings, is_active"
            " FROM aruco_camera_profiles WHERE camera_id = ? ORDER BY name",
            (camera_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def add_aruco_profile(
        self, camera_id: int, name: str, settings: str, *, is_active: bool = False
    ) -> int:
        """Add a camera profile. Returns the new row id."""
        db = self._conn()
        if is_active:
            await db.execute(
                "UPDATE aruco_camera_profiles SET is_active = 0 WHERE camera_id = ?",
                (camera_id,),
            )
        cur = await db.execute(
            "INSERT INTO aruco_camera_profiles (camera_id, name, settings, is_active)"
            " VALUES (?, ?, ?, ?)",
            (camera_id, name, settings, int(is_active)),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def activate_aruco_profile(self, profile_id: int) -> bool:
        """Activate a profile (deactivating others for the same camera)."""
        db = self._conn()
        cur = await db.execute(
            "SELECT camera_id FROM aruco_camera_profiles WHERE id = ?", (profile_id,)
        )
        row = await cur.fetchone()
        if not row:
            return False
        camera_id = row[0]
        await db.execute(
            "UPDATE aruco_camera_profiles SET is_active = 0 WHERE camera_id = ?",
            (camera_id,),
        )
        await db.execute(
            "UPDATE aruco_camera_profiles SET is_active = 1 WHERE id = ?",
            (profile_id,),
        )
        await db.commit()
        return True

    async def delete_aruco_profile(self, profile_id: int) -> bool:
        """Delete a camera profile."""
        db = self._conn()
        cur = await db.execute("DELETE FROM aruco_camera_profiles WHERE id = ?", (profile_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    # -- Controls --

    async def list_aruco_controls(self) -> list[dict[str, Any]]:
        """Return all ArUco controls with camera name."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT c.id, c.name, c.camera_id, c.marker_id_a, c.marker_id_b,"
            " c.tolerance_mm, c.created_at, cam.name AS camera_name"
            " FROM aruco_controls c"
            " JOIN aruco_cameras cam ON cam.id = c.camera_id"
            " ORDER BY c.name"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_aruco_control(self, control_id: int) -> dict[str, Any] | None:
        """Return a single ArUco control by id."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, name, camera_id, marker_id_a, marker_id_b,"
            " tolerance_mm, created_at FROM aruco_controls WHERE id = ?",
            (control_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_aruco_control(
        self,
        name: str,
        camera_id: int,
        marker_id_a: int,
        marker_id_b: int,
        tolerance_mm: float | None = None,
    ) -> int:
        """Add a control (marker pair → boat control mapping). Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO aruco_controls (name, camera_id, marker_id_a, marker_id_b, tolerance_mm)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, camera_id, marker_id_a, marker_id_b, tolerance_mm),
        )
        await db.commit()
        assert cur.lastrowid is not None
        logger.info(
            "ArUco control added: id={} name={} markers={}/{}",
            cur.lastrowid,
            name,
            marker_id_a,
            marker_id_b,
        )
        return cur.lastrowid

    async def update_aruco_control(
        self,
        control_id: int,
        *,
        name: str | None = None,
        marker_id_a: int | None = None,
        marker_id_b: int | None = None,
        tolerance_mm: float | None = ...,  # type: ignore[assignment]
    ) -> bool:
        """Update an ArUco control. Pass tolerance_mm=None explicitly to clear it."""
        db = self._conn()
        # tolerance_mm uses sentinel ... to distinguish "not provided" from "set to NULL"
        sets = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if marker_id_a is not None:
            sets.append("marker_id_a = ?")
            params.append(marker_id_a)
        if marker_id_b is not None:
            sets.append("marker_id_b = ?")
            params.append(marker_id_b)
        if tolerance_mm is not ...:  # type: ignore[comparison-overlap]
            sets.append("tolerance_mm = ?")
            params.append(tolerance_mm)
        if not sets:
            return False
        params.append(control_id)
        cur = await db.execute(
            f"UPDATE aruco_controls SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_aruco_control(self, control_id: int) -> bool:
        """Delete a control and cascade to measurements and trigger words."""
        db = self._conn()
        cur = await db.execute("DELETE FROM aruco_controls WHERE id = ?", (control_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    # -- Measurements --

    async def add_aruco_measurement(
        self,
        control_id: int,
        distance_cm: float,
        image_path: str | None = None,
        session_id: int | None = None,
    ) -> int:
        """Record a distance measurement. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO aruco_measurements (control_id, distance_cm, image_path, session_id)"
            " VALUES (?, ?, ?, ?)",
            (control_id, distance_cm, image_path, session_id),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_latest_aruco_measurement(self, control_id: int) -> dict[str, Any] | None:
        """Return the most recent measurement for a control."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, control_id, distance_cm, image_path, session_id, measured_at"
            " FROM aruco_measurements WHERE control_id = ?"
            " ORDER BY measured_at DESC LIMIT 1",
            (control_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_aruco_measurements(
        self, control_id: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return recent measurements for a control, newest first."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id, control_id, distance_cm, image_path, session_id, measured_at"
            " FROM aruco_measurements WHERE control_id = ?"
            " ORDER BY measured_at DESC LIMIT ?",
            (control_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    # -- Trigger words --

    async def list_aruco_trigger_words(self) -> list[dict[str, Any]]:
        """Return all trigger word → control mappings."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT tw.id, tw.phrase, tw.control_id, c.name AS control_name"
            " FROM aruco_trigger_words tw"
            " JOIN aruco_controls c ON c.id = tw.control_id"
            " ORDER BY tw.phrase"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def add_aruco_trigger_word(self, phrase: str, control_id: int) -> int:
        """Add a trigger word mapping. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO aruco_trigger_words (phrase, control_id) VALUES (?, ?)",
            (phrase, control_id),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def delete_aruco_trigger_word(self, trigger_id: int) -> bool:
        """Delete a trigger word mapping."""
        db = self._conn()
        cur = await db.execute("DELETE FROM aruco_trigger_words WHERE id = ?", (trigger_id,))
        await db.commit()
        return (cur.rowcount or 0) > 0

    # -- ArUco settings --

    async def get_aruco_setting(self, key: str) -> str | None:
        """Return an ArUco setting value or None."""
        db = self._read_conn()
        cur = await db.execute("SELECT value FROM aruco_settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_aruco_setting(self, key: str, value: str) -> None:
        """Set an ArUco setting (upsert)."""
        db = self._conn()
        await db.execute(
            "INSERT INTO aruco_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()

    async def get_aruco_tolerance_mm(self, control_id: int | None = None) -> float:
        """Return the effective tolerance: per-control if set, else global default."""
        if control_id is not None:
            ctrl = await self.get_aruco_control(control_id)
            if ctrl and ctrl["tolerance_mm"] is not None:
                return float(ctrl["tolerance_mm"])
        val = await self.get_aruco_setting("tolerance_mm_default")
        return float(val) if val else 5.0

    # ------------------------------------------------------------------
    # Vakaros VKX ingest (#458)
    # ------------------------------------------------------------------

    async def find_vakaros_session_by_hash(self, source_hash: str) -> int | None:
        """Return the id of a stored Vakaros session with this hash, or None."""
        db = self._read_conn()
        cur = await db.execute(
            "SELECT id FROM vakaros_sessions WHERE source_hash = ?",
            (source_hash,),
        )
        row = await cur.fetchone()
        return int(row["id"]) if row is not None else None

    async def store_vakaros_session(self, session: VakarosSession) -> int:
        """Insert a parsed Vakaros session and all its child rows.

        Idempotent on `source_hash` — if a session with the same hash
        already exists, returns that session's id without reinserting.
        Writes session + children in a single transaction.
        """
        from datetime import UTC
        from datetime import datetime as _datetime

        db = self._conn()
        cur = await db.execute(
            "SELECT id FROM vakaros_sessions WHERE source_hash = ?",
            (session.source_hash,),
        )
        existing = await cur.fetchone()
        if existing is not None:
            return int(existing["id"])

        ingested_at = _datetime.now(UTC).isoformat()
        cur = await db.execute(
            "INSERT INTO vakaros_sessions "
            "(source_hash, source_file, start_utc, end_utc, ingested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session.source_hash,
                session.source_file,
                session.start_utc.isoformat(),
                session.end_utc.isoformat(),
                ingested_at,
            ),
        )
        session_id = cur.lastrowid
        if session_id is None:
            await db.rollback()
            raise RuntimeError("Failed to insert vakaros_sessions row")

        if session.positions:
            await db.executemany(
                "INSERT INTO vakaros_positions "
                "(session_id, ts, latitude_deg, longitude_deg, sog_mps, cog_deg, altitude_m) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        p.timestamp.isoformat(),
                        p.latitude_deg,
                        p.longitude_deg,
                        p.sog_mps,
                        p.cog_deg,
                        p.altitude_m,
                    )
                    for p in session.positions
                ],
            )
        if session.line_positions:
            await db.executemany(
                "INSERT INTO vakaros_line_positions "
                "(session_id, ts, line_type, latitude_deg, longitude_deg) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        lp.timestamp.isoformat(),
                        lp.line_type.name.lower(),
                        lp.latitude_deg,
                        lp.longitude_deg,
                    )
                    for lp in session.line_positions
                ],
            )
        if session.race_events:
            await db.executemany(
                "INSERT INTO vakaros_race_events "
                "(session_id, ts, event_type, timer_value_s) VALUES (?, ?, ?, ?)",
                [
                    (
                        session_id,
                        e.timestamp.isoformat(),
                        e.event_type.name.lower(),
                        e.timer_value_s,
                    )
                    for e in session.race_events
                ],
            )
        if session.winds:
            await db.executemany(
                "INSERT INTO vakaros_winds "
                "(session_id, ts, direction_deg, speed_mps) VALUES (?, ?, ?, ?)",
                [
                    (
                        session_id,
                        w.timestamp.isoformat(),
                        w.direction_deg,
                        w.speed_mps,
                    )
                    for w in session.winds
                ],
            )
        await db.commit()
        return int(session_id)

    async def delete_vakaros_session(self, session_id: int) -> None:
        """Delete a Vakaros session and all its child rows (cascade)."""
        db = self._conn()
        await db.execute("DELETE FROM vakaros_sessions WHERE id = ?", (session_id,))
        await db.commit()

    async def get_vakaros_overlay_for_race(self, race_id: int) -> dict[str, Any] | None:
        """Return the Vakaros overlay payload for a race, or None if unlinked.

        Payload shape:
            {
                "vakaros_session_id": int,
                "track": GeoJSON Feature (LineString, [lon, lat] order) | None,
                "line_positions": [{"line_type": "pin"|"boat", ...}, ...],
                "race_events": [{"ts", "event_type", "timer_value_s"}, ...],
                "line": {"pin": [lat, lon], "boat": [lat, lon],
                         "length_m": float, "bearing_deg": float} | None,
            }

        The track is **trimmed to the race's time window** so a short race
        that shares a Vakaros file with other races doesn't display 2 hours
        of track. ``line`` is computed from the most recent pin + boat
        pings (across the whole Vakaros session, not just the race window,
        so a line set during pre-start is still shown). ``None`` when
        either endpoint is missing.
        """
        import math

        db = self._read_conn()
        cur = await db.execute(
            "SELECT vakaros_session_id, start_utc, end_utc FROM races WHERE id = ?",
            (race_id,),
        )
        row = await cur.fetchone()
        if row is None or row["vakaros_session_id"] is None:
            return None
        vakaros_id = int(row["vakaros_session_id"])
        race_start = row["start_utc"]
        race_end = row["end_utc"]

        if race_end is None:
            # Race still in progress — show everything up to now.
            pos_cur = await db.execute(
                "SELECT ts, latitude_deg, longitude_deg, sog_mps, cog_deg "
                "FROM vakaros_positions WHERE session_id = ? AND ts >= ? "
                "ORDER BY ts",
                (vakaros_id, race_start),
            )
        else:
            pos_cur = await db.execute(
                "SELECT ts, latitude_deg, longitude_deg, sog_mps, cog_deg "
                "FROM vakaros_positions WHERE session_id = ? "
                "  AND ts >= ? AND ts <= ? ORDER BY ts",
                (vakaros_id, race_start, race_end),
            )
        positions = await pos_cur.fetchall()
        track: dict[str, Any] | None = None
        if positions:
            coords = [[p["longitude_deg"], p["latitude_deg"]] for p in positions]
            track = {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "vakaros_session_id": vakaros_id,
                    "points": len(coords),
                    "timestamps": [p["ts"] for p in positions],
                    "sog_mps": [p["sog_mps"] for p in positions],
                    "cog_deg": [p["cog_deg"] for p in positions],
                },
            }

        # Line positions: pull *all* pings (not trimmed to race window) so the
        # UI can show every line-set event during the Vakaros session.
        line_cur = await db.execute(
            "SELECT ts, line_type, latitude_deg, longitude_deg "
            "FROM vakaros_line_positions WHERE session_id = ? ORDER BY ts",
            (vakaros_id,),
        )
        line_positions = [dict(r) for r in await line_cur.fetchall()]

        # Race events: trim to the race's window with a 60s buffer on each
        # side so the RACE_START event (which typically fires at the start
        # boundary) is included for this race and not the adjacent one.
        from datetime import datetime as _datetime
        from datetime import timedelta as _timedelta

        if race_end is not None:
            evt_start = (_datetime.fromisoformat(race_start) - _timedelta(seconds=60)).isoformat()
            evt_end = (_datetime.fromisoformat(race_end) + _timedelta(seconds=60)).isoformat()
            evt_cur = await db.execute(
                "SELECT ts, event_type, timer_value_s "
                "FROM vakaros_race_events WHERE session_id = ? "
                "  AND ts >= ? AND ts <= ? ORDER BY ts",
                (vakaros_id, evt_start, evt_end),
            )
        else:
            evt_cur = await db.execute(
                "SELECT ts, event_type, timer_value_s "
                "FROM vakaros_race_events WHERE session_id = ? AND ts >= ? "
                "ORDER BY ts",
                (vakaros_id, race_start),
            )
        race_events = [dict(r) for r in await evt_cur.fetchall()]

        # Line geometry: "the line that was active at the start of this
        # race" — latest pin + boat pings on or before the race start.
        # If no pre-race ping exists for a side, fall back to the earliest
        # post-race ping so the user still sees a line.
        pre_pin: dict[str, Any] | None = None
        pre_boat: dict[str, Any] | None = None
        post_pin: dict[str, Any] | None = None
        post_boat: dict[str, Any] | None = None
        for lp in line_positions:  # ordered by ts ascending
            is_pin = lp["line_type"] == "pin"
            if lp["ts"] <= race_start:
                if is_pin:
                    pre_pin = lp
                else:
                    pre_boat = lp
            elif is_pin and post_pin is None:
                post_pin = lp
            elif not is_pin and post_boat is None:
                post_boat = lp
        latest_pin = pre_pin or post_pin
        latest_boat = pre_boat or post_boat

        line: dict[str, Any] | None = None
        if latest_pin is not None and latest_boat is not None:
            lat1 = math.radians(latest_pin["latitude_deg"])
            lon1 = math.radians(latest_pin["longitude_deg"])
            lat2 = math.radians(latest_boat["latitude_deg"])
            lon2 = math.radians(latest_boat["longitude_deg"])
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            # Haversine
            a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            length_m = 2 * 6371000.0 * math.asin(math.sqrt(a))
            # Initial bearing pin -> boat
            y = math.sin(dlon) * math.cos(lat2)
            x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
            bearing_deg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
            line = {
                "pin": [latest_pin["latitude_deg"], latest_pin["longitude_deg"]],
                "boat": [latest_boat["latitude_deg"], latest_boat["longitude_deg"]],
                "length_m": round(length_m, 1),
                "bearing_deg": round(bearing_deg, 1),
                "pin_set_at": latest_pin["ts"],
                "boat_set_at": latest_boat["ts"],
            }

        # Trim line_positions to pings relevant to *this* race: anything set
        # at or before the race start. Pings from after the race belong to a
        # later race and would otherwise leak into this race's overlay (and
        # break the "latest = active" saturation rule on the frontend).
        # Fallback: if no pre-race pings exist for a side but the line
        # geometry above filled in from a post-race fallback, include those.
        relevant_pings: list[dict[str, Any]] = [
            lp for lp in line_positions if lp["ts"] <= race_start
        ]
        if line is not None:
            for fallback in (latest_pin, latest_boat):
                if fallback is None:
                    continue
                if not any(
                    lp["ts"] == fallback["ts"] and lp["line_type"] == fallback["line_type"]
                    for lp in relevant_pings
                ):
                    relevant_pings.append(fallback)
            relevant_pings.sort(key=lambda lp: lp["ts"])

        race_start_context = await self._build_race_start_context(
            race_events=race_events, line=line
        )

        return {
            "vakaros_session_id": vakaros_id,
            "track": track,
            "line_positions": relevant_pings,
            "race_events": race_events,
            "line": line,
            "race_start_context": race_start_context,
        }

    async def _build_race_start_context(
        self,
        race_events: list[dict[str, Any]],
        line: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Build a per-race snapshot of the boat's state at the race-start gun.

        Used by the session detail page to show boat speed, distance to the
        start line, polar %, and wind-relative line bias at the moment the
        Vakaros race_start event fires.  Returns ``None`` if there's no
        race_start event at all; otherwise a dict with the event ts and
        nullable per-field values so the UI can render partial data.
        """
        import math

        from helmlog.polar import _compute_twa, lookup_polar

        race_start_event: dict[str, Any] | None = None
        for e in race_events:
            if e["event_type"] == "race_start":
                race_start_event = e
                break
        if race_start_event is None:
            return None

        ts = race_start_event["ts"]
        db = self._read_conn()

        # Nearest SK sample within ±5 seconds of the race start.
        async def _nearest(table: str, columns: str) -> dict[str, Any] | None:
            cur = await db.execute(
                f"SELECT {columns} FROM {table} "
                "WHERE ABS(strftime('%s', ts) - strftime('%s', ?)) <= 5 "
                "ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?)) ASC "
                "LIMIT 1",
                (ts, ts),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None

        speed_row = await _nearest("speeds", "ts, speed_kts")
        sog_row = await _nearest("cogsog", "ts, sog_kts")
        pos_row = await _nearest("positions", "ts, latitude_deg, longitude_deg")
        head_row = await _nearest("headings", "ts, heading_deg")

        # Wind: only consider true-wind references (boat-referenced TWA = 0,
        # north-referenced TWD = 4). Apparent wind (reference = 2) is useless
        # for polar lookup or wind-relative line bias and would otherwise
        # poison the nearest-sample query when AWA samples are denser.
        wind_cur = await db.execute(
            "SELECT ts, wind_speed_kts, wind_angle_deg, reference FROM winds "
            "WHERE reference IN (0, 4) "
            "  AND ABS(strftime('%s', ts) - strftime('%s', ?)) <= 5 "
            "ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?)) ASC LIMIT 1",
            (ts, ts),
        )
        wind_row_raw = await wind_cur.fetchone()
        wind_row = dict(wind_row_raw) if wind_row_raw is not None else None

        bsp_kts = float(speed_row["speed_kts"]) if speed_row else None
        sog_kts = float(sog_row["sog_kts"]) if sog_row else None
        lat = float(pos_row["latitude_deg"]) if pos_row else None
        lon = float(pos_row["longitude_deg"]) if pos_row else None
        heading_deg = float(head_row["heading_deg"]) if head_row else None

        # Wind: TWS, TWD (when north-referenced), and TWA via polar's helper.
        tws_kts: float | None = None
        twd_deg: float | None = None
        twa_deg: float | None = None
        if wind_row is not None:
            tws_kts = float(wind_row["wind_speed_kts"])
            wind_angle = float(wind_row["wind_angle_deg"])
            wind_ref = int(wind_row["reference"])
            twa_deg = _compute_twa(wind_angle, wind_ref, heading_deg)
            # reference=4 → wind_angle IS TWD; reference=0 → derive from heading.
            if wind_ref == 4:
                twd_deg = wind_angle
            elif wind_ref == 0 and heading_deg is not None:
                twd_deg = (heading_deg + wind_angle) % 360.0

        # Polar %: actual BSP / target BSP at the (TWS, TWA) cell.
        polar_pct: float | None = None
        if bsp_kts is not None and tws_kts is not None and twa_deg is not None:
            polar_row = await lookup_polar(self, tws_kts, twa_deg)
            if polar_row is not None:
                target = float(polar_row["p90_bsp"])
                if target > 0:
                    polar_pct = round(bsp_kts / target * 100.0, 1)

        # Distance to line: perpendicular from boat position to the line.
        distance_to_line_m: float | None = None
        if line is not None and lat is not None and lon is not None:
            pin_lat, pin_lon = line["pin"]
            boat_end_lat, boat_end_lon = line["boat"]
            lat_rad = math.radians(pin_lat)
            m_per_deg_lat = 111320.0
            m_per_deg_lon = 111320.0 * math.cos(lat_rad)
            vx = (boat_end_lon - pin_lon) * m_per_deg_lon
            vy = (boat_end_lat - pin_lat) * m_per_deg_lat
            px = (lon - pin_lon) * m_per_deg_lon
            py = (lat - pin_lat) * m_per_deg_lat
            seg_len = math.hypot(vx, vy)
            if seg_len > 0:
                distance_to_line_m = round(abs(vx * py - vy * px) / seg_len, 1)

        # Wind-relative line bias.
        # The "square" line is perpendicular to the wind direction (TWD).
        # We measure the line bearing pin→boat against TWD; the offset from
        # square tells you which end is favoured (more upwind).
        #
        # Derivation: with line_bearing = B (pin→boat) and wind FROM = TWD,
        # raw = (B - TWD - 90) wrapped to [-90, 90].
        #   raw > 0 → pin  end is more upwind → pin  favoured
        #   raw < 0 → boat end is more upwind → boat favoured
        # Example: B=80, TWD=0 (wind from N). Pin at origin, boat at bearing
        # 80° is 0.17d north of pin → boat more upwind → boat favoured.
        # raw = 80 - 0 - 90 = -10 → boat, matches.
        line_bias_deg: float | None = None
        favored_end: str | None = None
        if line is not None and twd_deg is not None:
            line_bearing = float(line["bearing_deg"])
            raw = (line_bearing - twd_deg - 90.0) % 360.0
            if raw > 180.0:
                raw -= 360.0
            # Bring into [-90, 90] (a 180° flip is the same line).
            if raw > 90.0:
                raw -= 180.0
            elif raw < -90.0:
                raw += 180.0
            # Positive == boat favoured, negative == pin favoured.
            line_bias_deg = round(raw, 1)
            if abs(line_bias_deg) < 1.0:
                favored_end = "square"
            elif line_bias_deg > 0:
                favored_end = "pin"
            else:
                favored_end = "boat"

        return {
            "ts": ts,
            "bsp_kts": bsp_kts,
            "sog_kts": sog_kts,
            "latitude_deg": lat,
            "longitude_deg": lon,
            "distance_to_line_m": distance_to_line_m,
            "tws_kts": tws_kts,
            "twd_deg": twd_deg,
            "twa_deg": round(twa_deg, 1) if twa_deg is not None else None,
            "polar_pct": polar_pct,
            "line_bias_deg": line_bias_deg,
            "favored_end": favored_end,
        }

    async def list_vakaros_sessions(self) -> list[dict[str, Any]]:
        """Return all Vakaros sessions with row counts and matched races.

        Each session carries a ``matched_races`` list: zero or more
        ``{id, name, start_utc}`` dicts for the races currently linked to
        it via ``races.vakaros_session_id``.  Ordered newest Vakaros
        session first.
        """
        db = self._read_conn()
        cur = await db.execute(
            """
            SELECT
                vs.id,
                vs.source_file,
                vs.source_hash,
                vs.start_utc,
                vs.end_utc,
                vs.ingested_at,
                (SELECT COUNT(*) FROM vakaros_positions WHERE session_id = vs.id)
                    AS position_count,
                (SELECT COUNT(*) FROM vakaros_line_positions WHERE session_id = vs.id)
                    AS line_count,
                (SELECT COUNT(*) FROM vakaros_race_events WHERE session_id = vs.id)
                    AS event_count
            FROM vakaros_sessions vs
            ORDER BY vs.start_utc DESC
            """
        )
        sessions = [dict(r) for r in await cur.fetchall()]

        # One extra query to collect matched race names per session.
        match_cur = await db.execute(
            "SELECT id, name, start_utc, vakaros_session_id FROM races "
            "WHERE vakaros_session_id IS NOT NULL ORDER BY start_utc"
        )
        matches_by_session: dict[int, list[dict[str, Any]]] = {}
        for row in await match_cur.fetchall():
            sid = int(row["vakaros_session_id"])
            matches_by_session.setdefault(sid, []).append(
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "start_utc": row["start_utc"],
                }
            )
        for s in sessions:
            s["matched_races"] = matches_by_session.get(int(s["id"]), [])
        return sessions

    async def match_vakaros_session(self, session_id: int) -> list[int]:
        """Link a Vakaros session to *all* overlapping races.

        Rule (from the spec): a race matches when its time window overlaps
        the Vakaros session by at least 50% of the shorter duration. A
        single VKX file typically contains a full race day — multiple
        races plus practice — so each qualifying race on the races table
        gets its own ``vakaros_session_id`` link.

        Races still in progress (``end_utc IS NULL``) are never matched.
        Races that already point at a different Vakaros session are not
        overwritten — the first matcher to claim a race wins.  Re-running
        the matcher on the same Vakaros session is idempotent.

        Returns the list of race ids that now point at this session,
        ordered by race ``start_utc``.
        """
        from datetime import datetime as _datetime

        db = self._conn()
        cur = await db.execute(
            "SELECT start_utc, end_utc FROM vakaros_sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return []
        v_start = _datetime.fromisoformat(row["start_utc"])
        v_end = _datetime.fromisoformat(row["end_utc"])
        v_duration = (v_end - v_start).total_seconds()
        if v_duration <= 0:
            return []

        cur = await db.execute(
            "SELECT id, start_utc, end_utc, vakaros_session_id FROM races "
            "WHERE end_utc IS NOT NULL "
            "  AND start_utc < ? AND end_utc > ? "
            "ORDER BY start_utc",
            (v_end.isoformat(), v_start.isoformat()),
        )
        candidates = await cur.fetchall()

        linked: list[int] = []
        for cand in candidates:
            r_start = _datetime.fromisoformat(cand["start_utc"])
            r_end = _datetime.fromisoformat(cand["end_utc"])
            r_duration = (r_end - r_start).total_seconds()
            if r_duration <= 0:
                continue
            overlap_start = max(v_start, r_start)
            overlap_end = min(v_end, r_end)
            overlap_s = (overlap_end - overlap_start).total_seconds()
            if overlap_s <= 0:
                continue
            shorter = min(v_duration, r_duration)
            ratio = overlap_s / shorter
            if ratio < 0.5:
                continue
            existing = cand["vakaros_session_id"]
            if existing is not None and int(existing) != session_id:
                # Don't steal a race that's already claimed by a different
                # Vakaros session — leaves room for future manual override.
                continue
            await db.execute(
                "UPDATE races SET vakaros_session_id = ? WHERE id = ?",
                (session_id, int(cand["id"])),
            )
            linked.append(int(cand["id"]))

        await db.commit()
        return linked

    async def rematch_all_vakaros_sessions(self) -> dict[int, list[int]]:
        """Re-run matching for every stored Vakaros session.

        Intended for historical data ingested before the matcher existed
        or when matching rules change.  Returns a mapping from Vakaros
        session id to the list of race ids that now link to it.
        """
        db = self._read_conn()
        cur = await db.execute("SELECT id FROM vakaros_sessions ORDER BY id")
        rows = await cur.fetchall()
        results: dict[int, list[int]] = {}
        for r in rows:
            sid = int(r["id"])
            results[sid] = await self.match_vakaros_session(sid)
        return results

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
