"""Tests for the POST /api/sessions/{id}/detect-maneuvers endpoint —
specifically its resilience to a transient SQLite write lock.

A re-detect writes through ``write_maneuvers`` and can lose a race with the
live logger or the startup maneuver-backfill task, raising
``sqlite3.OperationalError: database is locked``. The endpoint must surface a
retryable 503 rather than an opaque 500 in that case.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from httpx import ASGITransport

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _seed_race(storage: Storage, session_id: int) -> None:
    db = storage._conn()
    start = datetime(2026, 4, 20, 14, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=120)
    await db.execute(
        "INSERT INTO races"
        " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, 'e', ?, ?, 'race', ?, ?)",
        (session_id, "s", session_id, start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_detect_maneuvers_lock_returns_503(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_race(storage, 1)
    from helmlog import maneuver_detector
    from helmlog.web import create_app

    async def boom(*_a: object, **_k: object) -> list:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(maneuver_detector, "detect_maneuvers", boom)

    app = create_app(storage)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/sessions/1/detect-maneuvers")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "3"
    assert r.json()["retryable"] is True


@pytest.mark.asyncio
async def test_detect_maneuvers_non_lock_operationalerror_not_swallowed(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-lock OperationalError (e.g. a real schema error) must NOT be
    masked as a friendly 503 — it should propagate as a 500."""
    await _seed_race(storage, 1)
    from helmlog import maneuver_detector
    from helmlog.web import create_app

    async def boom(*_a: object, **_k: object) -> list:
        raise sqlite3.OperationalError("no such column: bogus")

    monkeypatch.setattr(maneuver_detector, "detect_maneuvers", boom)

    app = create_app(storage)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=10) as c:
        with pytest.raises(sqlite3.OperationalError):
            await c.post("/api/sessions/1/detect-maneuvers")


@pytest.mark.asyncio
async def test_detect_maneuvers_unknown_session_404(storage: Storage) -> None:
    from helmlog.web import create_app

    app = create_app(storage)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/sessions/999/detect-maneuvers")
    assert r.status_code == 404
