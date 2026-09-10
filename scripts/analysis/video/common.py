"""Shared pieces for the video-analysis scripts: config, ledger, race index, telemetry.

Configuration is by environment variable so the scripts follow the
``scripts/analysis/README.md`` conventions:

``HELMLOG_DB``          telemetry SQLite (default ``data/logger.db``), read-only
``HELMLOG_META_DB``     races / videos / Vakaros / maneuvers SQLite; defaults to
                        ``HELMLOG_DB``. Lets a fresh metadata dump from the Pi
                        pair with an older, larger telemetry snapshot.
``VIDEO_ANALYSIS_DIR``  sidecar root (default ``data/video-analysis``)
``INSTA360_EXPORTS``    where local 8K stitched files live
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
OWN_SAIL_NUMBER = "475"  # Corvo 105 (the 105 is the class, the sail is 475)


def va_dir() -> Path:
    return Path(os.environ.get("VIDEO_ANALYSIS_DIR", "data/video-analysis")).expanduser()


def telemetry_db_path() -> str:
    return os.environ.get("HELMLOG_DB", "data/logger.db")


def meta_db_path() -> str:
    return os.environ.get("HELMLOG_META_DB", telemetry_db_path())


def exports_dir() -> Path:
    return Path(os.environ.get("INSTA360_EXPORTS", "~/Insta360 Exports")).expanduser()


def open_ro(path: str) -> sqlite3.Connection:
    """Read-only connection. ``immutable=1`` only for backup snapshots (no WAL)."""
    uri = path if path.startswith("file:") else f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_date(utc: datetime) -> str:
    return utc.astimezone(LOCAL_TZ).date().isoformat()


def is_wednesday_local(utc: datetime) -> bool:
    return utc.astimezone(LOCAL_TZ).weekday() == 2


# ---------------------------------------------------------------------------
# Bearing geometry of the boat-locked equirectangular frame
# ---------------------------------------------------------------------------


def x_to_rel_bearing(x: float, width: int) -> float:
    """Horizontal pixel → relative bearing, bow = 0, range -180..+180."""
    return (x / width) * 360.0 - 180.0


def rel_bearing_to_x(rel: float, width: int) -> int:
    return int(round((rel + 180.0) / 360.0 * width)) % width


def abs_bearing(heading: float, rel: float) -> float:
    return (heading + rel) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Signed smallest difference a-b in degrees, -180..+180."""
    return ((a - b + 180.0) % 360.0) - 180.0


# ---------------------------------------------------------------------------
# Ledger (sidecar SQLite)
# ---------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    video_id    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,           -- local | youtube
    path        TEXT NOT NULL,
    width       INTEGER, height INTEGER, fps REAL, duration_s REAL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audio_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL, video_t REAL NOT NULL, kind TEXT NOT NULL,
    z REAL NOT NULL, dur_s REAL NOT NULL, detector_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audio_events_video ON audio_events(video_id, video_t);
CREATE TABLE IF NOT EXISTS sync (
    race_id       INTEGER PRIMARY KEY,
    video_id      TEXT NOT NULL,
    method        TEXT NOT NULL,          -- stored | horn | horn+vakaros
    gun_utc       TEXT,                   -- effective gun for the race
    gun_video_t   REAL,
    sync_utc      TEXT NOT NULL,
    sync_offset_s REAL NOT NULL,
    delta_s       REAL NOT NULL DEFAULT 0, -- correction vs the stored sync
    horn_hits     INTEGER NOT NULL DEFAULT 0, -- warning horns supporting the gun
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instants (
    race_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    utc TEXT NOT NULL, video_id TEXT NOT NULL, video_t REAL NOT NULL,
    PRIMARY KEY (race_id, name)
);
CREATE TABLE IF NOT EXISTS frames (
    video_id TEXT NOT NULL, video_t REAL NOT NULL, path TEXT NOT NULL,
    width INTEGER NOT NULL, height INTEGER NOT NULL, source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (video_id, video_t)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL, instant TEXT NOT NULL, utc TEXT NOT NULL,
    video_id TEXT NOT NULL, video_t REAL NOT NULL,
    schema_version INTEGER NOT NULL, reader TEXT NOT NULL,
    reader_version TEXT NOT NULL, prompt_version TEXT NOT NULL,
    confidence REAL, payload_json TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    superseded_by INTEGER REFERENCES observations(id)
);
CREATE INDEX IF NOT EXISTS idx_obs_race ON observations(race_id, instant);
CREATE TABLE IF NOT EXISTS facts (
    race_id INTEGER NOT NULL, key TEXT NOT NULL, value_json TEXT NOT NULL,
    computed_from TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (race_id, key)
);
"""


def open_ledger(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and create) the sidecar ledger. ``":memory:"`` for tests."""
    target = Path(path) if path is not None else va_dir() / "ledger.sqlite"
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.executescript(LEDGER_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Race index (from the metadata DB)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    url: str
    title: str
    sync_utc: datetime
    sync_offset_s: float
    duration_s: float | None

    def video_t(self, utc: datetime) -> float:
        return self.sync_offset_s + (utc - self.sync_utc).total_seconds()

    def utc_at(self, video_t: float) -> datetime:
        return self.sync_utc + timedelta(seconds=video_t - self.sync_offset_s)


@dataclass(frozen=True)
class Race:
    id: int
    name: str
    event: str
    start_utc: datetime
    end_utc: datetime | None
    session_type: str
    video: VideoRef | None
    vakaros_gun: datetime | None
    roundings: tuple[datetime, ...] = ()
    tacks: tuple[datetime, ...] = ()
    gybes: tuple[datetime, ...] = ()
    result_place: int | None = None
    fleet_size: int | None = None
    tags: tuple[str, ...] = ()

    @property
    def local_date(self) -> str:
        return local_date(self.start_utc)


def _video_for(conn: sqlite3.Connection, race_id: int) -> VideoRef | None:
    row = conn.execute(
        "SELECT youtube_url, video_id, title, sync_utc, sync_offset_s, duration_s"
        " FROM race_videos WHERE race_id = ? ORDER BY id LIMIT 1",
        (race_id,),
    ).fetchone()
    if row is None:
        return None
    return VideoRef(
        video_id=row["video_id"],
        url=row["youtube_url"],
        title=row["title"] or "",
        sync_utc=parse_utc(row["sync_utc"]),
        sync_offset_s=float(row["sync_offset_s"]),
        duration_s=float(row["duration_s"]) if row["duration_s"] is not None else None,
    )


def _vakaros_gun(conn: sqlite3.Connection, row: sqlite3.Row) -> datetime | None:
    """The Vakaros ``race_start`` inside the session window, if any."""
    if row["vakaros_session_id"] is None:
        return None
    end = row["end_utc"] or (parse_utc(row["start_utc"]) + timedelta(hours=3)).isoformat()
    ev = conn.execute(
        "SELECT ts FROM vakaros_race_events WHERE session_id = ? AND event_type = 'race_start'"
        " AND ts >= ? AND ts <= ? ORDER BY ts LIMIT 1",
        (row["vakaros_session_id"], row["start_utc"], end),
    ).fetchone()
    return parse_utc(ev["ts"]) if ev else None


def _result(conn: sqlite3.Connection, race_id: int) -> tuple[int | None, int | None]:
    """Our finishing place and fleet size from a scoring duplicate linked to this race."""
    rows = conn.execute(
        "SELECT rr.place, b.sail_number, rr.status_code FROM race_results rr"
        " JOIN races sr ON sr.id = rr.race_id LEFT JOIN boats b ON b.id = rr.boat_id"
        " WHERE sr.local_session_id = ? ORDER BY rr.place",
        (race_id,),
    ).fetchall()
    if not rows:
        return None, None
    place = None
    for r in rows:
        sail = (r["sail_number"] or "").split()[-1] if r["sail_number"] else ""
        if sail == OWN_SAIL_NUMBER:
            place = int(r["place"])
    return place, len(rows)


def load_race(conn: sqlite3.Connection, race_id: int) -> Race:
    row = conn.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
    if row is None:
        raise KeyError(f"race {race_id} not found")
    mans = conn.execute(
        "SELECT type, ts FROM maneuvers WHERE session_id = ? ORDER BY ts", (race_id,)
    ).fetchall()
    tags = conn.execute(
        "SELECT t.name FROM session_tags st JOIN tags t ON t.id = st.tag_id"
        " WHERE st.session_id = ? ORDER BY t.name",
        (race_id,),
    ).fetchall()
    place, fleet = _result(conn, race_id)
    return Race(
        id=int(row["id"]),
        name=row["name"],
        event=row["event"],
        start_utc=parse_utc(row["start_utc"]),
        end_utc=parse_utc(row["end_utc"]) if row["end_utc"] else None,
        session_type=row["session_type"],
        video=_video_for(conn, race_id),
        vakaros_gun=_vakaros_gun(conn, row),
        roundings=tuple(parse_utc(m["ts"]) for m in mans if m["type"] == "rounding"),
        tacks=tuple(parse_utc(m["ts"]) for m in mans if m["type"] == "tack"),
        gybes=tuple(parse_utc(m["ts"]) for m in mans if m["type"] == "gybe"),
        result_place=place,
        fleet_size=fleet,
        tags=tuple(t["name"] for t in tags),
    )


def list_cyc_wednesday_races(conn: sqlite3.Connection, season: int = 2026) -> list[Race]:
    """Live CYC races sailed on a local Wednesday with a linked video."""
    rows = conn.execute(
        "SELECT id, event, start_utc FROM races WHERE source = 'live'"
        " AND session_type = 'race' AND start_utc >= ? ORDER BY start_utc",
        (f"{season}-01-01",),
    ).fetchall()
    out: list[Race] = []
    for r in rows:
        ev = r["event"].lower()
        if not (ev.startswith("cyc") or "sol" in ev):
            continue
        if not is_wednesday_local(parse_utc(r["start_utc"])):
            continue
        race = load_race(conn, int(r["id"]))
        if race.video is not None:
            out.append(race)
    return out


# ---------------------------------------------------------------------------
# Telemetry (1 Hz series from the telemetry DB)
# ---------------------------------------------------------------------------

WIND_REF_BOAT = 0
WIND_REF_NORTH = 4


@dataclass(frozen=True)
class Stamp:
    hdg: float | None
    sog: float | None
    bsp: float | None
    twa: float | None
    tws: float | None
    twd: float | None
    lat: float | None
    lon: float | None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "hdg": self.hdg,
            "sog": self.sog,
            "bsp": self.bsp,
            "twa": self.twa,
            "tws": self.tws,
            "twd": self.twd,
            "lat": self.lat,
            "lon": self.lon,
        }


@dataclass
class Telemetry:
    """Per-second series keyed by ISO second ``YYYY-MM-DDTHH:MM:SS``."""

    hdg: dict[str, float] = field(default_factory=dict)
    sog: dict[str, float] = field(default_factory=dict)
    bsp: dict[str, float] = field(default_factory=dict)
    twa: dict[str, float] = field(default_factory=dict)
    tws: dict[str, float] = field(default_factory=dict)
    twd: dict[str, float] = field(default_factory=dict)
    lat: dict[str, float] = field(default_factory=dict)
    lon: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def _key(utc: datetime) -> str:
        return utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    def _lookup(self, series: dict[str, float], utc: datetime, back_s: int = 5) -> float | None:
        for k in range(back_s + 1):
            v = series.get(self._key(utc - timedelta(seconds=k)))
            if v is not None:
                return v
        return None

    def at(self, utc: datetime) -> Stamp:
        hdg = self._lookup(self.hdg, utc)
        twa = self._lookup(self.twa, utc)
        twd = self._lookup(self.twd, utc)
        if twd is None and hdg is not None and twa is not None:
            twd = (hdg + twa) % 360.0
        return Stamp(
            hdg=hdg,
            sog=self._lookup(self.sog, utc),
            bsp=self._lookup(self.bsp, utc),
            twa=twa,
            tws=self._lookup(self.tws, utc),
            twd=twd,
            lat=self._lookup(self.lat, utc),
            lon=self._lookup(self.lon, utc),
        )

    def tack_at(self, utc: datetime) -> str | None:
        """'stbd' when the wind is over the starboard side (TWA > 0), else 'port'."""
        twa = self._lookup(self.twa, utc)
        if twa is None:
            return None
        return "stbd" if twa > 0 else "port"


def _fill(series: dict[str, float], rows: list[sqlite3.Row], col: str) -> None:
    for r in rows:
        key = str(r["ts"])[:19]
        series.setdefault(key, float(r[col]))


def load_telemetry(conn: sqlite3.Connection, start: datetime, end: datetime) -> Telemetry:
    """First sample per second for each channel between start and end (UTC)."""
    s, e = start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()
    t = Telemetry()
    q = "SELECT ts, {col} FROM {table} WHERE ts >= ? AND ts <= ? ORDER BY ts"
    _fill(
        t.hdg,
        conn.execute(q.format(col="heading_deg", table="headings"), (s, e)).fetchall(),
        "heading_deg",
    )
    _fill(
        t.bsp,
        conn.execute(q.format(col="speed_kts", table="speeds"), (s, e)).fetchall(),
        "speed_kts",
    )
    _fill(
        t.sog, conn.execute(q.format(col="sog_kts", table="cogsog"), (s, e)).fetchall(), "sog_kts"
    )
    pos = conn.execute(
        "SELECT ts, latitude_deg, longitude_deg FROM positions WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (s, e),
    ).fetchall()
    _fill(t.lat, pos, "latitude_deg")
    _fill(t.lon, pos, "longitude_deg")
    winds = conn.execute(
        "SELECT ts, wind_speed_kts, wind_angle_deg, reference FROM winds"
        " WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (s, e),
    ).fetchall()
    for r in winds:
        key = str(r["ts"])[:19]
        if int(r["reference"]) == WIND_REF_BOAT:
            t.twa.setdefault(key, float(r["wind_angle_deg"]))
            t.tws.setdefault(key, float(r["wind_speed_kts"]))
        elif int(r["reference"]) == WIND_REF_NORTH:
            t.twd.setdefault(key, float(r["wind_angle_deg"]))
    return t


def race_window(race: Race, pre_s: int = 60, post_s: int = 120) -> tuple[datetime, datetime]:
    end = race.end_utc or (race.start_utc + timedelta(hours=2))
    return race.start_utc - timedelta(seconds=pre_s), end + timedelta(seconds=post_s)


# ---------------------------------------------------------------------------
# Effective sync per race (ledger overrides the stored race_videos sync)
# ---------------------------------------------------------------------------


def effective_video(ledger: sqlite3.Connection, race: Race) -> VideoRef | None:
    if race.video is None:
        return None
    row = ledger.execute("SELECT * FROM sync WHERE race_id = ?", (race.id,)).fetchone()
    if row is None:
        return race.video
    return VideoRef(
        video_id=row["video_id"],
        url=race.video.url,
        title=race.video.title,
        sync_utc=parse_utc(row["sync_utc"]),
        sync_offset_s=float(row["sync_offset_s"]),
        duration_s=race.video.duration_s,
    )


def effective_gun(ledger: sqlite3.Connection, race: Race) -> datetime | None:
    row = ledger.execute("SELECT gun_utc FROM sync WHERE race_id = ?", (race.id,)).fetchone()
    if row is not None and row["gun_utc"]:
        return parse_utc(row["gun_utc"])
    return race.vakaros_gun


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, float) and math.isnan(o):
        return None
    raise TypeError(f"not serialisable: {type(o).__name__}")
