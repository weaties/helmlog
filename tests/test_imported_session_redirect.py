"""Tests for redirecting imported results sessions to their linked live
recording (the imported→live navigation fix).

Imported Clubspot/results rows (source != 'live') with a placeholder
start_utc==end_utc window carry a local_session_id pointing at the live
recording that actually holds the track + maneuvers. Visiting the imported
race page should redirect to the live session's canonical URL, where the
maneuvers exist and the imported results are folded back in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from httpx import ASGITransport

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _seed_live(storage: Storage, sid: int, slug: str) -> None:
    db = storage._conn()
    start = datetime(2026, 6, 4, 0, 13, 0, tzinfo=UTC)
    end = start + timedelta(minutes=55)
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, session_type,"
        " start_utc, end_utc, slug, source)"
        " VALUES (?, ?, 'e', ?, '2026-06-04', 'race', ?, ?, ?, 'live')",
        (sid, f"Live {sid}", sid, start.isoformat(), end.isoformat(), slug),
    )
    await db.commit()


async def _seed_imported(
    storage: Storage, sid: int, slug: str, *, local_session_id: int | None
) -> None:
    db = storage._conn()
    placeholder = "2026-06-04T00:00:00+00:00"
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, session_type,"
        " start_utc, end_utc, slug, source, source_id, local_session_id)"
        " VALUES (?, ?, 'e', ?, '2026-06-04', 'race', ?, ?, ?, 'clubspot', ?, ?)",
        (sid, f"Race {sid}", sid, placeholder, placeholder, slug, f"tok_{sid}", local_session_id),
    )
    await db.commit()


def _client(storage: Storage) -> httpx.AsyncClient:
    from helmlog.web import create_app

    app = create_app(storage)
    return httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t", follow_redirects=False
    )


@pytest.mark.asyncio
async def test_matched_import_redirects_to_live_session(storage: Storage) -> None:
    await _seed_live(storage, 185, "live-185")
    await _seed_imported(storage, 188, "race-19", local_session_id=185)

    async with _client(storage) as c:
        r = await c.get("/session/188/race-19")
    assert r.status_code == 302
    assert r.headers["location"] == "/session/185/live-185"


@pytest.mark.asyncio
async def test_redirect_preserves_query_string(storage: Storage) -> None:
    await _seed_live(storage, 185, "live-185")
    await _seed_imported(storage, 188, "race-19", local_session_id=185)

    async with _client(storage) as c:
        r = await c.get("/session/188/race-19", params={"moment": "5"})
    assert r.status_code == 302
    assert r.headers["location"] == "/session/185/live-185?moment=5"


@pytest.mark.asyncio
async def test_unmatched_import_renders_in_place(storage: Storage) -> None:
    """An imported row with no local_session_id has nothing to redirect to —
    it should still render its own (scoreboard) page, not 404 or 500."""
    await _seed_imported(storage, 189, "race-20", local_session_id=None)

    async with _client(storage) as c:
        r = await c.get("/session/189/race-20")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_live_session_does_not_redirect(storage: Storage) -> None:
    await _seed_live(storage, 185, "live-185")

    async with _client(storage) as c:
        r = await c.get("/session/185/live-185")
    assert r.status_code == 200
