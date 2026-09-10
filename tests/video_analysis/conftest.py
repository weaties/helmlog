"""Fixtures for the offline video-analysis scripts (scripts/analysis/video).

The scripts are not part of the ``helmlog`` package, so the repo root is put
on ``sys.path`` and they are imported as ``scripts.analysis.video.<module>``.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.analysis.video.common import open_ledger  # noqa: E402

GUN = datetime(2026, 9, 10, 1, 25, 1, tzinfo=UTC)
SESSION_START = datetime(2026, 9, 10, 1, 17, 7, tzinfo=UTC)
SESSION_END = datetime(2026, 9, 10, 1, 50, 28, tzinfo=UTC)

META_SCHEMA = """
CREATE TABLE races (id INTEGER PRIMARY KEY, name TEXT, event TEXT, race_num INTEGER,
  date TEXT, start_utc TEXT, end_utc TEXT, session_type TEXT DEFAULT 'race',
  source TEXT DEFAULT 'live', vakaros_session_id INTEGER, local_session_id INTEGER);
CREATE TABLE race_videos (id INTEGER PRIMARY KEY, race_id INTEGER, youtube_url TEXT,
  video_id TEXT, label TEXT, sync_utc TEXT, sync_offset_s REAL, duration_s REAL, title TEXT);
CREATE TABLE vakaros_race_events (id INTEGER PRIMARY KEY, session_id INTEGER, ts TEXT,
  event_type TEXT, timer_value_s INTEGER);
CREATE TABLE maneuvers (id INTEGER PRIMARY KEY, session_id INTEGER, type TEXT, ts TEXT);
CREATE TABLE race_results (id INTEGER PRIMARY KEY, race_id INTEGER, place INTEGER,
  boat_id INTEGER, status_code TEXT);
CREATE TABLE boats (id INTEGER PRIMARY KEY, sail_number TEXT, name TEXT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE session_tags (session_id INTEGER, tag_id INTEGER);
CREATE TABLE headings (id INTEGER PRIMARY KEY, ts TEXT, heading_deg REAL);
CREATE TABLE speeds (id INTEGER PRIMARY KEY, ts TEXT, speed_kts REAL);
CREATE TABLE cogsog (id INTEGER PRIMARY KEY, ts TEXT, cog_deg REAL, sog_kts REAL);
CREATE TABLE positions (id INTEGER PRIMARY KEY, ts TEXT, latitude_deg REAL, longitude_deg REAL);
CREATE TABLE winds (id INTEGER PRIMARY KEY, ts TEXT, wind_speed_kts REAL,
  wind_angle_deg REAL, reference INTEGER);
"""


@pytest.fixture
def meta_db() -> sqlite3.Connection:
    """In-memory metadata + telemetry DB shaped like the app schema, with race 254."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(META_SCHEMA)
    window = (SESSION_START.isoformat(), SESSION_END.isoformat())
    conn.execute(
        "INSERT INTO races VALUES (254,'20260909-Cyc-1','Cyc',1,'2026-09-09',?,?,"
        "'race','live',7,NULL)",
        window,
    )
    conn.execute(
        "INSERT INTO races VALUES (270,'Race 46 - J/105','J/105',46,'2026-09-09',?,?,"
        "'race','clubspot',NULL,254)",
        window,
    )
    conn.execute(
        "INSERT INTO race_videos VALUES (78,254,'https://youtu.be/jygj-NbqFJE','jygj-NbqFJE',"
        "'',?,0.0,1976.0,'VID 20260909 181623 00 002')",
        (SESSION_START.isoformat(),),
    )
    # Vakaros session 7 spans the evening: one race_start before, one inside, one after.
    for ts in ("2026-09-10T00:47:05+00:00", GUN.isoformat(), "2026-09-10T02:01:02+00:00"):
        conn.execute(
            "INSERT INTO vakaros_race_events (session_id, ts, event_type, timer_value_s)"
            " VALUES (7, ?, 'race_start', 0)",
            (ts,),
        )
    mans = [
        ("gybe", "01:17:59"),
        ("rounding", "01:21:36"),  # pre-start turn, not a mark
        ("tack", "01:23:33"),
        ("rounding", "01:24:37"),  # pre-start turn
        ("rounding", "01:25:15"),  # just after the gun, not a mark
        ("tack", "01:27:42"),
        ("tack", "01:31:50"),
        ("rounding", "01:33:37"),  # W1
        ("gybe", "01:36:11"),
        ("rounding", "01:44:42"),  # L1
        ("tack", "01:47:27"),
    ]
    for typ, hms in mans:
        conn.execute(
            "INSERT INTO maneuvers (session_id, type, ts) VALUES (254, ?, ?)",
            (typ, f"2026-09-10T{hms}+00:00"),
        )
    conn.execute("INSERT INTO boats VALUES (47,'475','Corvo 105')")
    conn.execute("INSERT INTO boats VALUES (48,'412','Other')")
    conn.execute("INSERT INTO race_results VALUES (1,270,1,48,NULL)")
    conn.execute("INSERT INTO race_results VALUES (2,270,5,47,NULL)")
    conn.execute("INSERT INTO race_results VALUES (3,270,6,NULL,'DNC')")
    conn.execute("INSERT INTO tags VALUES (25,'start-2nd-row')")
    conn.execute("INSERT INTO session_tags VALUES (254,25)")
    # 1 Hz telemetry across the session: heading 337 on stbd tack, TWA +55, TWS 8.6.
    t = SESSION_START
    i = 0
    while t <= SESSION_END:
        ts = t.isoformat()
        conn.execute(
            "INSERT INTO headings (ts, heading_deg) VALUES (?, ?)", (ts, 337.0 + (i % 3) * 0.1)
        )
        conn.execute("INSERT INTO speeds (ts, speed_kts) VALUES (?, ?)", (ts, 4.5))
        conn.execute("INSERT INTO cogsog (ts, cog_deg, sog_kts) VALUES (?, ?, ?)", (ts, 335.0, 4.7))
        conn.execute(
            "INSERT INTO positions (ts, latitude_deg, longitude_deg) VALUES (?, ?, ?)",
            (ts, 47.68, -122.41),
        )
        wind = "INSERT INTO winds (ts, wind_speed_kts, wind_angle_deg, reference) VALUES (?,?,?,?)"
        conn.execute(wind, (ts, 8.6, 55.0, 0))
        conn.execute(wind, (ts, 9.0, 40.0, 2))
        t += timedelta(seconds=1)
        i += 1
    conn.commit()
    return conn


@pytest.fixture
def ledger() -> sqlite3.Connection:
    return open_ledger(":memory:")
