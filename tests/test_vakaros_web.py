"""Tests for the Vakaros admin web routes (#458 cycle 5)."""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage
else:
    from pathlib import Path  # noqa: TC003  # runtime needed by pytest fixture types


def _build_minimal_vkx_bytes(ts_ms: int = 1_700_000_000_000) -> bytes:
    header = bytes([0xFF, 0x05, 0, 0, 0, 0, 0, 0])
    payload = struct.pack(
        "<Qiifffffff",
        ts_ms,
        round(47.68 / 1e-7),
        round(-122.41 / 1e-7),
        1.0,
        math.radians(0.0),
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
    )
    row = bytes([0x02]) + payload
    terminator = bytes([0xFE]) + struct.pack("<H", len(row))
    return header + row + terminator


@pytest.fixture
def inbox_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    inbox = tmp_path / "vakaros-inbox"
    inbox.mkdir()
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("VAKAROS_INBOX_DIR", str(inbox))
    return inbox


@pytest.mark.asyncio
async def test_admin_vakaros_page_lists_inbox_files_and_sessions(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "alpha.vkx").write_bytes(b"x")
    (inbox_path / "bravo.vkx").write_bytes(b"x")
    (inbox_path / "readme.txt").write_bytes(b"ignored")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/vakaros")

    assert resp.status_code == 200
    body = resp.text
    assert "alpha.vkx" in body
    assert "bravo.vkx" in body
    assert "readme.txt" not in body
    # Empty state for the sessions list
    assert "No Vakaros sessions" in body or "vakaros_sessions" in body.lower()


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_processes_valid_file(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "good.vkx").write_bytes(_build_minimal_vkx_bytes())

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "good.vkx"},
            follow_redirects=False,
        )

    # Expect a redirect back to the admin page (PRG pattern).
    assert resp.status_code in (302, 303)
    assert resp.headers["location"].startswith("/admin/vakaros")

    # Original is gone, archived in processed/, DB has one row.
    assert not (inbox_path / "good.vkx").exists()
    assert (inbox_path / "processed" / "good.vkx").exists()

    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM vakaros_sessions")
    row = await cur.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_moves_malformed_file_to_failed(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "junk.vkx").write_bytes(b"\x00\x00\x00")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "junk.vkx"},
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    assert (inbox_path / "failed" / "junk.vkx").exists()
    assert (inbox_path / "failed" / "junk.vkx.err").exists()

    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM vakaros_sessions")
    row = await cur.fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_vakaros_overlay_returns_404_for_unknown_race(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/99999/vakaros-overlay")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_vakaros_overlay_returns_empty_when_no_match(
    storage: Storage, inbox_path: Path
) -> None:
    """A race that has no matched Vakaros session should return an empty payload."""
    from datetime import UTC, datetime

    from helmlog.web import create_app

    # Insert a race with no Vakaros session nearby.
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Solo race",
            "evt",
            1,
            "2026-04-09",
            datetime(2026, 4, 9, 12, 0, tzinfo=UTC).isoformat(),
            datetime(2026, 4, 9, 13, 0, tzinfo=UTC).isoformat(),
            "race",
        ),
    )
    await db.commit()
    race_id = cur.lastrowid

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is False
    assert data["line_positions"] == []
    assert data["race_events"] == []
    assert data["track"] is None


@pytest.mark.asyncio
async def test_vakaros_overlay_returns_full_payload_when_matched(
    storage: Storage, inbox_path: Path
) -> None:
    """When a race is matched to a Vakaros session, overlay returns track, line, events."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import (
        LinePosition,
        LinePositionType,
        PositionRow,
        RaceTimerEvent,
        RaceTimerEventType,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = VakarosSession(
        source_hash="f" * 64,
        source_file="test.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=15),
                latitude_deg=47.681,
                longitude_deg=-122.411,
                sog_mps=2.0,
                cog_deg=45.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=30),
                latitude_deg=47.682,
                longitude_deg=-122.412,
                sog_mps=1.5,
                cog_deg=90.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            LinePosition(
                timestamp=t0,
                line_type=LinePositionType.PIN,
                latitude_deg=47.687,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0,
                line_type=LinePositionType.BOAT,
                latitude_deg=47.687,
                longitude_deg=-122.416,
            ),
        ),
        race_events=(
            RaceTimerEvent(
                timestamp=t0 + timedelta(minutes=5),
                event_type=RaceTimerEventType.RACE_START,
                timer_value_s=0,
            ),
            RaceTimerEvent(
                timestamp=t0 + timedelta(minutes=28),
                event_type=RaceTimerEventType.RACE_END,
                timer_value_s=1380,
            ),
        ),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    # Create an overlapping race and match it.
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Shilshole race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=3)).isoformat(),
            (t0 + timedelta(minutes=27)).isoformat(),
            "race",
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    assert race_id is not None
    linked = await storage.match_vakaros_session(vakaros_id)
    assert linked == [race_id]

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")

    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is True
    assert data["vakaros_session_id"] == vakaros_id

    # Track is trimmed to the race window (03:00 - 27:00 inside a session
    # with positions at 00:00, 15:00, 30:00), so only the middle point is
    # inside — a single-coord LineString.
    assert data["track"] is not None
    assert data["track"]["type"] == "Feature"
    assert data["track"]["geometry"]["type"] == "LineString"
    assert len(data["track"]["geometry"]["coordinates"]) == 1
    # [lon, lat] GeoJSON order
    assert data["track"]["geometry"]["coordinates"][0] == [-122.411, 47.681]

    # Line positions: one pin, one boat.
    lines_by_type = {lp["line_type"]: lp for lp in data["line_positions"]}
    assert set(lines_by_type.keys()) == {"pin", "boat"}
    assert lines_by_type["pin"]["latitude_deg"] == 47.687
    assert lines_by_type["boat"]["longitude_deg"] == -122.416

    # Race events: race_start + race_end.
    event_types = {e["event_type"] for e in data["race_events"]}
    assert "race_start" in event_types
    assert "race_end" in event_types

    # Computed line geometry (from the most recent pin + boat pings).
    assert data["line"] is not None
    line = data["line"]
    assert line["pin"] == [47.687, -122.420]
    assert line["boat"] == [47.687, -122.416]
    # Length: ~301 m along this latitude (0.004 deg lon @ 47.687 N).
    assert 280 < line["length_m"] < 320
    # Bearing from pin to boat: close to 90° (due east).
    assert 85 < line["bearing_deg"] < 95


@pytest.mark.asyncio
async def test_vakaros_overlay_line_uses_line_active_at_race_start(
    storage: Storage, inbox_path: Path
) -> None:
    """When the line was re-set, each race gets the line active at its start.

    Two races share a Vakaros file with an early line and a re-set line.
    The race that starts *before* the re-set sees the early line; the
    race that starts *after* the re-set sees the late line.
    """
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import (
        LinePosition,
        LinePositionType,
        PositionRow,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = VakarosSession(
        source_hash="e" * 64,
        source_file="test.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=30),
                latitude_deg=47.682,
                longitude_deg=-122.412,
                sog_mps=1.5,
                cog_deg=90.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            # Early line
            LinePosition(
                timestamp=t0 + timedelta(minutes=2),
                line_type=LinePositionType.PIN,
                latitude_deg=47.680,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0 + timedelta(minutes=3),
                line_type=LinePositionType.BOAT,
                latitude_deg=47.680,
                longitude_deg=-122.416,
            ),
            # Line was re-set later
            LinePosition(
                timestamp=t0 + timedelta(minutes=15),
                line_type=LinePositionType.PIN,
                latitude_deg=47.690,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0 + timedelta(minutes=16),
                line_type=LinePositionType.BOAT,
                latitude_deg=47.690,
                longitude_deg=-122.416,
            ),
        ),
        race_events=(),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    db = storage._conn()
    # Race 1 starts BEFORE the re-set (at minute 5) — should see the early line.
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Race 1",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=5)).isoformat(),
            (t0 + timedelta(minutes=10)).isoformat(),
            "race",
        ),
    )
    race1_id = cur.lastrowid
    # Race 2 starts AFTER the re-set (at minute 20) — should see the late line.
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Race 2",
            "evt",
            2,
            "2026-04-09",
            (t0 + timedelta(minutes=20)).isoformat(),
            (t0 + timedelta(minutes=28)).isoformat(),
            "race",
        ),
    )
    race2_id = cur.lastrowid
    await db.commit()
    linked = await storage.match_vakaros_session(vakaros_id)
    assert set(linked) == {race1_id, race2_id}

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1_resp = await client.get(f"/api/sessions/{race1_id}/vakaros-overlay")
        r2_resp = await client.get(f"/api/sessions/{race2_id}/vakaros-overlay")

    r1 = r1_resp.json()
    assert r1["line"] is not None
    assert r1["line"]["pin"] == [47.680, -122.420]
    assert r1["line"]["boat"] == [47.680, -122.416]

    r2 = r2_resp.json()
    assert r2["line"] is not None
    assert r2["line"]["pin"] == [47.690, -122.420]
    assert r2["line"]["boat"] == [47.690, -122.416]


@pytest.mark.asyncio
async def test_vakaros_overlay_line_is_none_when_only_one_endpoint(
    storage: Storage, inbox_path: Path
) -> None:
    """If only a pin or only a boat has been pinged, line is None."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import (
        LinePosition,
        LinePositionType,
        PositionRow,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = VakarosSession(
        source_hash="d" * 64,
        source_file="test.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=30),
                latitude_deg=47.682,
                longitude_deg=-122.412,
                sog_mps=1.5,
                cog_deg=90.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            LinePosition(
                timestamp=t0 + timedelta(minutes=2),
                line_type=LinePositionType.PIN,
                latitude_deg=47.687,
                longitude_deg=-122.420,
            ),
        ),
        race_events=(),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Half-line race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=5)).isoformat(),
            (t0 + timedelta(minutes=25)).isoformat(),
            "race",
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    await storage.match_vakaros_session(vakaros_id)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")

    data = resp.json()
    assert data["line"] is None
    # line_positions still includes the single pin so the UI can render it.
    assert len(data["line_positions"]) == 1


@pytest.mark.asyncio
async def test_admin_vakaros_rematch_links_existing_sessions(
    storage: Storage, inbox_path: Path
) -> None:
    """POST /admin/vakaros/rematch should run matching across all stored sessions."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import PositionRow, VakarosSession
    from helmlog.web import create_app

    # Insert a Vakaros session directly (bypassing ingest_vkx_file so its
    # auto-match path does NOT run — simulating historical data).
    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = VakarosSession(
        source_hash="99" * 32,
        source_file="historical.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(hours=2),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(hours=2),
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(),
        race_events=(),
        winds=(),
    )
    await storage.store_vakaros_session(session)

    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Late race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=30)).isoformat(),
            (t0 + timedelta(minutes=90)).isoformat(),
            "race",
        ),
    )
    await db.commit()
    race_id = cur.lastrowid

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/admin/vakaros/rematch", follow_redirects=False)

    assert resp.status_code in (302, 303)
    cur = await db.execute("SELECT vakaros_session_id FROM races WHERE id = ?", (race_id,))
    row = await cur.fetchone()
    assert row["vakaros_session_id"] is not None


@pytest.mark.asyncio
async def test_vakaros_overlay_race_start_context_includes_bsp_and_line_distance(
    storage: Storage, inbox_path: Path
) -> None:
    """Overlay payload should surface boat speed + distance-to-line at race start."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import (
        LinePosition,
        LinePositionType,
        PositionRow,
        RaceTimerEvent,
        RaceTimerEventType,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    race_start_ts = t0 + timedelta(minutes=5)  # race_start event
    session = VakarosSession(
        source_hash="aa" * 32,
        source_file="context.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=30),
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            LinePosition(
                timestamp=t0 + timedelta(minutes=1),
                line_type=LinePositionType.PIN,
                latitude_deg=47.687,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0 + timedelta(minutes=1),
                line_type=LinePositionType.BOAT,
                latitude_deg=47.687,
                longitude_deg=-122.416,
            ),
        ),
        race_events=(
            RaceTimerEvent(
                timestamp=race_start_ts,
                event_type=RaceTimerEventType.RACE_START,
                timer_value_s=0,
            ),
        ),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    # Insert an SK race that overlaps the Vakaros window.
    db = storage._conn()
    race_cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Start ctx race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=3)).isoformat(),
            (t0 + timedelta(minutes=25)).isoformat(),
            "race",
        ),
    )
    race_id = race_cur.lastrowid
    await db.commit()
    await storage.match_vakaros_session(vakaros_id)

    # Seed SK instrument samples right around race_start.
    # BSP 5.4 kt, SOG 5.6 kt, position mid-way between pin and boat but 20 m behind.
    # Pin: 47.687, -122.420    Boat: 47.687, -122.416 (line runs due east)
    # Boat behind the line means 20 m *south* of the line (lat < 47.687).
    # 20 m south ≈ 20 / 111320 degrees latitude.
    boat_lat = 47.687 - (20.0 / 111320.0)
    boat_lon = -122.418  # midway between -122.420 and -122.416
    for delta in (-2, -1, 0, 1, 2):
        ts = (race_start_ts + timedelta(seconds=delta)).isoformat()
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (ts, 5, 5.4),
        )
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (ts, 5, 0.0, 5.6),
        )
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg) "
            "VALUES (?, ?, ?, ?)",
            (ts, 5, boat_lat, boat_lon),
        )
    await db.commit()

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")

    data = resp.json()
    assert data["matched"] is True
    ctx = data.get("race_start_context")
    assert ctx is not None, "overlay should include race_start_context"
    assert ctx["ts"] == race_start_ts.isoformat()
    # Boat speed at the start — within 0.1 kt of the seeded value.
    assert 5.3 <= ctx["bsp_kts"] <= 5.5
    assert 5.5 <= ctx["sog_kts"] <= 5.7
    # Boat position at the start.
    assert ctx["latitude_deg"] == pytest.approx(boat_lat, abs=1e-6)
    assert ctx["longitude_deg"] == pytest.approx(boat_lon, abs=1e-6)
    # Perpendicular distance to the start line — we set the boat 20 m behind.
    assert 18.0 <= ctx["distance_to_line_m"] <= 22.0


@pytest.mark.asyncio
async def test_vakaros_overlay_race_start_context_includes_polar_pct_and_line_bias(
    storage: Storage, inbox_path: Path
) -> None:
    """Race start context should compute polar % and wind-relative line bias.

    Wind comes in as north-referenced (reference=4 → wind_angle_deg IS TWD).
    Polar baseline is seeded directly so the lookup hits a known cell.
    """
    from datetime import UTC, datetime, timedelta

    from helmlog.polar import _twa_bin, _tws_bin
    from helmlog.vakaros import (
        LinePosition,
        LinePositionType,
        PositionRow,
        RaceTimerEvent,
        RaceTimerEventType,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    race_start_ts = t0 + timedelta(minutes=5)
    session = VakarosSession(
        source_hash="cc" * 32,
        source_file="polar.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=30),
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            # Line runs due east (pin west, boat east) — bearing 90° T.
            LinePosition(
                timestamp=t0 + timedelta(minutes=1),
                line_type=LinePositionType.PIN,
                latitude_deg=47.687,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0 + timedelta(minutes=1),
                line_type=LinePositionType.BOAT,
                latitude_deg=47.687,
                longitude_deg=-122.416,
            ),
        ),
        race_events=(
            RaceTimerEvent(
                timestamp=race_start_ts,
                event_type=RaceTimerEventType.RACE_START,
                timer_value_s=0,
            ),
        ),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    db = storage._conn()
    race_cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Polar test race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=3)).isoformat(),
            (t0 + timedelta(minutes=25)).isoformat(),
            "race",
        ),
    )
    race_id = race_cur.lastrowid
    await db.commit()
    await storage.match_vakaros_session(vakaros_id)

    # Seed SK samples around race_start.
    boat_lat = 47.687  # exactly on the line
    boat_lon = -122.418
    bsp_kts = 5.4
    sog_kts = 5.6
    heading_deg = 0.0  # boat pointing due north
    twd_deg = 45.0  # wind from NE → TWD 45°
    tws_kts = 12.0
    for delta in (-2, -1, 0, 1, 2):
        ts = (race_start_ts + timedelta(seconds=delta)).isoformat()
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (ts, 5, bsp_kts),
        )
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (ts, 5, 0.0, sog_kts),
        )
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg) "
            "VALUES (?, ?, ?, ?)",
            (ts, 5, boat_lat, boat_lon),
        )
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
            (ts, 5, heading_deg),
        )
        # reference=4 → wind_angle_deg IS TWD (north-referenced)
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, 5, tws_kts, twd_deg, 4),
        )
    await db.commit()

    # Seed a polar baseline cell that matches the (TWS, TWA) bin we'll
    # compute. heading=0, twd=45 → twa = 45 (boat is on starboard tack
    # close-hauled on a 45° wind).
    tws_bin = _tws_bin(tws_kts)
    twa_bin = _twa_bin(45.0)
    await storage.upsert_polar_baseline(
        [
            {
                "tws_bin": tws_bin,
                "twa_bin": twa_bin,
                "mean_bsp": 5.0,
                "p90_bsp": 6.0,  # target → polar % = 5.4 / 6.0 = 90%
                "session_count": 5,
                "sample_count": 100,
            }
        ],
        built_at=t0.isoformat(),
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")

    data = resp.json()
    ctx = data["race_start_context"]
    assert ctx is not None

    # Wind context
    assert ctx["tws_kts"] == pytest.approx(tws_kts)
    assert ctx["twd_deg"] == pytest.approx(twd_deg)
    assert ctx["twa_deg"] == pytest.approx(45.0)

    # Polar %: 5.4 / 6.0 ≈ 90%
    assert ctx["polar_pct"] == pytest.approx(90.0, abs=0.5)

    # Wind-relative line bias.
    # Line bearing pin → boat = 90° T (pin at west end, boat at east end).
    # Wind FROM 45° T (NE), so the upwind direction is to the NE. The boat
    # end (east) projects further along the NE upwind vector than the pin
    # end (west), so the boat end is more upwind → boat favoured.
    assert ctx["line_bias_deg"] is not None
    assert ctx["favored_end"] in ("pin", "boat", "square")
    assert ctx["favored_end"] == "boat"  # for this geometry


@pytest.mark.asyncio
async def test_vakaros_overlay_race_start_context_absent_when_no_sk_data(
    storage: Storage, inbox_path: Path
) -> None:
    """If there's no SK data around the race_start event, ctx is None."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import (
        PositionRow,
        RaceTimerEvent,
        RaceTimerEventType,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = VakarosSession(
        source_hash="bb" * 32,
        source_file="no_sk.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=30),
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(),
        race_events=(
            RaceTimerEvent(
                timestamp=t0 + timedelta(minutes=5),
                event_type=RaceTimerEventType.RACE_START,
                timer_value_s=0,
            ),
        ),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "No-SK race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=3)).isoformat(),
            (t0 + timedelta(minutes=25)).isoformat(),
            "race",
        ),
    )
    race_id = cur.lastrowid
    await db.commit()
    await storage.match_vakaros_session(vakaros_id)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")

    data = resp.json()
    # No SK speeds/positions/wind seeded → context is present but with nulls
    # (race_start timestamp is still known even without instrument data).
    ctx = data.get("race_start_context")
    assert ctx is not None
    assert ctx["bsp_kts"] is None
    assert ctx["sog_kts"] is None
    assert ctx["distance_to_line_m"] is None  # no line endpoints anyway
    assert ctx["tws_kts"] is None
    assert ctx["twd_deg"] is None
    assert ctx["twa_deg"] is None
    assert ctx["polar_pct"] is None
    assert ctx["line_bias_deg"] is None
    assert ctx["favored_end"] is None


@pytest.mark.asyncio
async def test_vakaros_overlay_trims_line_positions_to_pre_race(
    storage: Storage, inbox_path: Path
) -> None:
    """Pings set after this race's start belong to a later race and must
    not leak into this race's overlay. Race 1 should NOT see the post-race
    re-set that's used by race 2."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import (
        LinePosition,
        LinePositionType,
        PositionRow,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = VakarosSession(
        source_hash="dd" * 32,
        source_file="trim_pings.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=60),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=60),
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            # Race 1 line — pre-race
            LinePosition(
                timestamp=t0 + timedelta(minutes=2),
                line_type=LinePositionType.PIN,
                latitude_deg=47.681,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0 + timedelta(minutes=3),
                line_type=LinePositionType.BOAT,
                latitude_deg=47.681,
                longitude_deg=-122.416,
            ),
            # Race 2 line — set AFTER race 1 has started
            LinePosition(
                timestamp=t0 + timedelta(minutes=20),
                line_type=LinePositionType.PIN,
                latitude_deg=47.690,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0 + timedelta(minutes=21),
                line_type=LinePositionType.BOAT,
                latitude_deg=47.690,
                longitude_deg=-122.416,
            ),
        ),
        race_events=(),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Race 1",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=5)).isoformat(),
            (t0 + timedelta(minutes=15)).isoformat(),
            "race",
        ),
    )
    race1_id = cur.lastrowid
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Race 2",
            "evt",
            2,
            "2026-04-09",
            (t0 + timedelta(minutes=25)).isoformat(),
            (t0 + timedelta(minutes=35)).isoformat(),
            "race",
        ),
    )
    race2_id = cur.lastrowid
    await db.commit()
    await storage.match_vakaros_session(vakaros_id)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = (await client.get(f"/api/sessions/{race1_id}/vakaros-overlay")).json()
        r2 = (await client.get(f"/api/sessions/{race2_id}/vakaros-overlay")).json()

    # Race 1 sees only the pre-race pings (one of each).
    r1_pings = sorted(
        [(lp["line_type"], round(lp["latitude_deg"], 3)) for lp in r1["line_positions"]]
    )
    assert r1_pings == [("boat", 47.681), ("pin", 47.681)]

    # Race 2 sees both sets — pings on or before race 2's start at +25 min.
    r2_pings = sorted(
        [(lp["line_type"], round(lp["latitude_deg"], 3)) for lp in r2["line_positions"]]
    )
    assert r2_pings == [
        ("boat", 47.681),
        ("boat", 47.690),
        ("pin", 47.681),
        ("pin", 47.690),
    ]


@pytest.mark.asyncio
async def test_vakaros_overlay_trims_track_to_race_window(
    storage: Storage, inbox_path: Path
) -> None:
    """Overlay track contains only positions inside the race's time window."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import PositionRow, VakarosSession
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    positions = tuple(
        PositionRow(
            timestamp=t0 + timedelta(minutes=i * 30),
            latitude_deg=47.68 + i * 0.001,
            longitude_deg=-122.41,
            sog_mps=1.0,
            cog_deg=0.0,
            altitude_m=0.0,
            quat_w=1.0,
            quat_x=0.0,
            quat_y=0.0,
            quat_z=0.0,
        )
        for i in range(5)
    )
    session = VakarosSession(
        source_hash="77" * 32,
        source_file="trim.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(hours=2),
        positions=positions,
        line_positions=(),
        race_events=(),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    # Race from t0+75m to t0+105m — only the position at t0+90m is inside.
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Trimmed race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=75)).isoformat(),
            (t0 + timedelta(minutes=105)).isoformat(),
            "race",
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    linked = await storage.match_vakaros_session(vakaros_id)
    assert race_id in linked

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")

    data = resp.json()
    assert data["matched"] is True
    assert data["track"] is not None
    coords = data["track"]["geometry"]["coordinates"]
    assert len(coords) == 1


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_rejects_path_traversal(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "../escape.vkx"},
            follow_redirects=False,
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_race_start_context_nearest_lookups_use_ts_index(storage: Storage) -> None:
    """Nearest-sample lookups must be index range scans, not full table scans (#859).

    A function-wrapped ``ts`` predicate forced SQLite to scan every row of the
    instrument tables (~15 s on a season-sized DB), blocking the shared read
    connection for every other request.
    """
    from datetime import UTC, datetime

    race_start_ts = datetime(2026, 4, 9, 12, 5, 0, tzinfo=UTC).isoformat()
    statements: list[str] = []
    db = storage._read_conn()
    await db.set_trace_callback(statements.append)
    try:
        await storage._build_race_start_context(
            [{"ts": race_start_ts, "event_type": "race_start", "timer_value_s": 0}],
            None,
        )
    finally:
        await db.set_trace_callback(None)

    tables = ("speeds", "cogsog", "positions", "headings", "winds")
    lookups = [s for s in statements if any(f"FROM {t} " in s for t in tables)]
    assert len(lookups) == len(tables)
    for sql in lookups:
        cur = await db.execute(f"EXPLAIN QUERY PLAN {sql}")
        plan = " | ".join(str(r["detail"]) for r in await cur.fetchall())
        assert "SCAN" not in plan, f"full scan in plan: {plan} for {sql}"
        assert "INDEX" in plan, f"no index used: {plan} for {sql}"


@pytest.mark.asyncio
async def test_race_start_context_nearest_lookup_window_and_ordering(storage: Storage) -> None:
    """The nearest sample within ±5 s wins; samples further out are ignored (#859)."""
    from datetime import UTC, datetime, timedelta

    t = datetime(2026, 4, 9, 12, 5, 0, 500000, tzinfo=UTC)
    db = storage._conn()
    for delta, kts in ((-9, 1.0), (-3, 3.0), (2, 2.0), (8, 8.0)):
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            ((t + timedelta(seconds=delta)).isoformat(), 5, kts),
        )
    # Second-resolution timestamp (no fractional part) at the window edge.
    await db.execute(
        "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
        ("2026-04-09T12:05:05+00:00", 5, 0.0, 4.4),
    )
    await db.commit()

    ctx = await storage._build_race_start_context(
        [{"ts": t.isoformat(), "event_type": "race_start", "timer_value_s": 0}], None
    )
    assert ctx is not None
    assert ctx["bsp_kts"] == pytest.approx(2.0)
    assert ctx["sog_kts"] == pytest.approx(4.4)

    # Nothing within ±5 s → fields are None.
    far = (t + timedelta(minutes=10)).isoformat()
    ctx_far = await storage._build_race_start_context(
        [{"ts": far, "event_type": "race_start", "timer_value_s": 0}], None
    )
    assert ctx_far is not None
    assert ctx_far["bsp_kts"] is None
