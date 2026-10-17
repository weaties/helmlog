"""Tests for session deletion (#409) — decision table + guard conditions."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from helmlog.auth import generate_token, session_expires_at
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_START = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
_END = datetime(2026, 3, 1, 13, 0, 0, tzinfo=UTC)


async def _create_user(storage: Storage, role: str) -> tuple[int, str]:
    email = f"{role}@test.com"
    user_id = await storage.create_user(email, f"Test {role}", role)
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


async def _create_ended_session(storage: Storage) -> int:
    """Create a completed (ended) race session. Returns race id."""
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Test Race", "Regatta", 1, "2026-03-01", _START.isoformat(), _END.isoformat(), "race"),
    )
    race_id = cur.lastrowid
    assert race_id is not None
    await db.commit()
    return race_id


async def _create_active_session(storage: Storage) -> int:
    """Create an active (no end_utc) race session. Returns race id."""
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Active Race", "Regatta", 2, "2026-03-01", _START.isoformat(), None, "race"),
    )
    race_id = cur.lastrowid
    assert race_id is not None
    await db.commit()
    return race_id


@pytest_asyncio.fixture
async def admin_client(storage: Storage) -> tuple[httpx.AsyncClient, int, str]:  # type: ignore[misc]
    """Return (client, user_id, auth_session_id) for an admin user."""
    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            yield client, _, session_id


# ---------------------------------------------------------------------------
# Decision table row 1: viewer cannot delete (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_viewer_cannot_delete_session(storage: Storage) -> None:
    race_id = await _create_ended_session(storage)
    _, session_id = await _create_user(storage, "viewer")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Decision table row 2: crew cannot delete (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crew_cannot_delete_session(storage: Storage) -> None:
    race_id = await _create_ended_session(storage)
    _, session_id = await _create_user(storage, "crew")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Decision table row 3: admin cannot delete active session (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_cannot_delete_active_session(storage: Storage) -> None:
    race_id = await _create_active_session(storage)
    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 409
    assert "active" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Decision table row 4: admin deletes ended session (no co-op) → 204
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_deletes_ended_session(storage: Storage) -> None:
    race_id = await _create_ended_session(storage)
    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 204

    # Verify session is gone
    db = storage._conn()
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (race_id,))
    assert await cur.fetchone() is None


# ---------------------------------------------------------------------------
# Decision table row 5: admin deletes ended session (shared with co-op) → 204
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_deletes_shared_session(storage: Storage) -> None:
    race_id = await _create_ended_session(storage)
    # Insert a co-op sharing row (shared_by is INTEGER FK to users, nullable)
    db = storage._conn()
    await db.execute(
        "INSERT INTO session_sharing (session_id, co_op_id, shared_at, shared_by)"
        " VALUES (?, ?, ?, ?)",
        (race_id, "test-coop-id", _END.isoformat(), None),
    )
    await db.commit()

    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 204

    # Both session and sharing row should be gone (FK cascade)
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (race_id,))
    assert await cur.fetchone() is None
    cur = await db.execute("SELECT * FROM session_sharing WHERE session_id = ?", (race_id,))
    assert await cur.fetchone() is None


# ---------------------------------------------------------------------------
# Guard: delete nonexistent session → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_nonexistent_session_returns_404(storage: Storage) -> None:
    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete("/api/sessions/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Guard: cascade completeness — instrument data deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_cascades_instrument_data(storage: Storage) -> None:
    race_id = await _create_ended_session(storage)
    db = storage._conn()

    # Insert instrument data within session time range
    ts = datetime(2026, 3, 1, 12, 30, 0, tzinfo=UTC).isoformat()
    await db.execute(
        "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
        (ts, 0, 180.0),
    )
    await db.execute(
        "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
        (ts, 0, 5.5),
    )
    await db.commit()

    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 204

    # Instrument data within the session's time range should be deleted
    cur = await db.execute("SELECT COUNT(*) FROM headings WHERE ts = ?", (ts,))
    row = await cur.fetchone()
    assert row[0] == 0

    cur = await db.execute("SELECT COUNT(*) FROM speeds WHERE ts = ?", (ts,))
    row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_delete_removes_tagged_rows_outside_time_window(storage: Storage) -> None:
    """Rows tagged with the session's race_id but stamped just before its
    start_utc (GPS-disciplined timestamps on records queued as the race
    opened) used to survive the time-window delete and then block the
    session row via the non-cascading race_id FK — a 500 on delete."""
    race_id = await _create_ended_session(storage)
    db = storage._conn()
    early = datetime(2026, 3, 1, 11, 59, 59, 900000, tzinfo=UTC).isoformat()
    for sql, params in (
        ("INSERT INTO headings (ts, source_addr, heading_deg, race_id)", (180.0,)),
        ("INSERT INTO speeds (ts, source_addr, speed_kts, race_id)", (5.5,)),
        ("INSERT INTO depths (ts, source_addr, depth_m, race_id)", (12.0,)),
        (
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)",
            (47.6, -122.4),
        ),
        ("INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts, race_id)", (90.0, 6.0)),
        (
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference,"
            " race_id)",
            (10.0, 45.0, 2),
        ),
    ):
        values = ", ".join("?" * (len(params) + 3))
        await db.execute(f"{sql} VALUES ({values})", (early, 0, *params, race_id))
    await db.commit()

    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 204

    for table in ("headings", "speeds", "depths", "positions", "cogsog", "winds"):
        cur = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE race_id = ?", (race_id,))  # noqa: S608
        row = await cur.fetchone()
        assert row[0] == 0, table
    cur = await db.execute("SELECT COUNT(*) FROM races WHERE id = ?", (race_id,))
    row = await cur.fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# Guard: audit log entry created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_creates_audit_entry(storage: Storage) -> None:
    race_id = await _create_ended_session(storage)
    _, session_id = await _create_user(storage, "admin")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.delete(f"/api/sessions/{race_id}")
    assert resp.status_code == 204

    db = storage._conn()
    cur = await db.execute("SELECT action, detail FROM audit_log WHERE action = 'session.delete'")
    row = await cur.fetchone()
    assert row is not None
    assert "Test Race" in row["detail"]
