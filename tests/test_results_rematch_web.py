"""Test the /api/results/regattas/{id}/rematch endpoint (#520)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest_asyncio.fixture
async def admin_client(  # type: ignore[misc]
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> httpx.AsyncClient:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_rematch_links_imported_race_to_new_local_session(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    """Rematch backfills local_session_id for imported races created before the local session."""
    db = storage._conn()
    now = datetime.now(UTC).isoformat()

    # Insert a regatta + an imported race with no local link.
    cur = await db.execute(
        "INSERT INTO regattas (source, source_id, name, created_at)"
        " VALUES ('clubspot', 'rg1', 'Test Regatta', ?)",
        (now,),
    )
    regatta_id = cur.lastrowid

    race_date = "2026-04-09"
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, "
        "session_type, regatta_id, source, source_id) "
        "VALUES (?, ?, ?, ?, ?, 'race', ?, 'clubspot', 'rc1')",
        ("Race 1", "J/105", 1, race_date, race_date, regatta_id),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (imported_id,) = await cur.fetchone()  # type: ignore[misc]

    # Now create a local session on that date.
    local_start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Local Wed", "Local", 1, race_date, local_start.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    # Pre-condition: imported race has no link.
    cur = await db.execute("SELECT local_session_id FROM races WHERE id = ?", (imported_id,))
    assert (await cur.fetchone())[0] is None

    # Rematch.
    resp = await admin_client.post(f"/api/results/regattas/{regatta_id}/rematch")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["races_checked"] == 1
    assert body["linked"] == 1

    # Post-condition: link populated.
    cur = await db.execute("SELECT local_session_id FROM races WHERE id = ?", (imported_id,))
    assert (await cur.fetchone())[0] == local_id


@pytest.mark.asyncio
async def test_rematch_unknown_regatta_returns_404(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    resp = await admin_client.post("/api/results/regattas/9999/rematch")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_link_imported_invalidates_session_summary_cache(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    """Manual link-imported drops the cached session_summary blob (#739 follow-up).

    Reproduces the history-page bug: a /summary fetch ran *before* the user
    linked imported results, populating web_cache with results: []. Without
    invalidation the history thumbnails keep serving the stale empty list.
    """
    db = storage._conn()
    now = datetime.now(UTC).isoformat()

    cur = await db.execute(
        "INSERT INTO regattas (source, source_id, name, created_at)"
        " VALUES ('clubspot', 'rg-link', 'Test Regatta', ?)",
        (now,),
    )
    regatta_id = cur.lastrowid

    race_date = "2026-04-09"
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, "
        "session_type, regatta_id, source, source_id) "
        "VALUES (?, ?, ?, ?, ?, 'race', ?, 'clubspot', 'rc-link-1')",
        ("Imported Race", "J/105", 1, race_date, race_date, regatta_id),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (imported_id,) = await cur.fetchone()  # type: ignore[misc]

    local_start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, ?, 'race')",
        (
            "Local Race",
            "Local",
            1,
            race_date,
            local_start.isoformat(),
            (local_start + timedelta(minutes=30)).isoformat(),
        ),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]

    # A boat + result so the imported race actually has something to surface.
    cur = await db.execute(
        "INSERT INTO boats (sail_number, name, class, last_used) VALUES ('USA 1', 'Test', '', ?)",
        (now,),
    )
    boat_id = cur.lastrowid
    await db.execute(
        "INSERT INTO race_results (race_id, boat_id, place, dnf, dns, created_at)"
        " VALUES (?, ?, 1, 0, 0, ?)",
        (imported_id, boat_id, now),
    )
    await db.commit()

    # Prime the cache with a pre-link summary (results will be empty).
    pre = await admin_client.get(f"/api/sessions/{local_id}/summary")
    assert pre.status_code == 200
    assert pre.json()["results"] == []
    pre_etag = pre.headers.get("etag")
    assert pre_etag is not None

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ? AND key_family = ?",
        (local_id, "session_summary"),
    )
    assert (await cur.fetchone())["n"] == 1

    # Link the imported race manually.
    resp = await admin_client.post(
        f"/api/sessions/{local_id}/link-imported",
        data={"imported_race_id": imported_id},
    )
    assert resp.status_code == 200, resp.text

    # Cache must be gone for the local session AND the imported race.
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ? AND key_family = ?",
        (local_id, "session_summary"),
    )
    assert (await cur.fetchone())["n"] == 0

    # Re-fetch — fresh compute now sees the linked results AND the ETag flips
    # so a browser holding the pre-link 200 won't get a 304 with stale data.
    post = await admin_client.get(f"/api/sessions/{local_id}/summary")
    assert post.status_code == 200
    assert len(post.json()["results"]) == 1
    post_etag = post.headers.get("etag")
    assert post_etag is not None
    assert post_etag != pre_etag

    # Sanity: a conditional GET with the OLD ETag must NOT return 304 — the
    # server must recognise the link change and serve the fresh payload.
    cond = await admin_client.get(
        f"/api/sessions/{local_id}/summary",
        headers={"If-None-Match": pre_etag},
    )
    assert cond.status_code == 200
    assert len(cond.json()["results"]) == 1


@pytest.mark.asyncio
async def test_link_imported_unlink_invalidates_cache(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    """imported_race_id=0 (unlink) also drops the session's cached summary."""
    db = storage._conn()
    now = datetime.now(UTC).isoformat()

    cur = await db.execute(
        "INSERT INTO regattas (source, source_id, name, created_at)"
        " VALUES ('clubspot', 'rg-unlink', 'Test Regatta', ?)",
        (now,),
    )
    regatta_id = cur.lastrowid
    race_date = "2026-04-16"
    local_start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, ?, 'race')",
        (
            "Local",
            "Local",
            1,
            race_date,
            local_start.isoformat(),
            (local_start + timedelta(minutes=30)).isoformat(),
        ),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]

    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, "
        "session_type, regatta_id, source, source_id, local_session_id) "
        "VALUES (?, ?, ?, ?, ?, 'race', ?, 'clubspot', 'rc-unlink', ?)",
        ("Imported", "J/105", 1, race_date, race_date, regatta_id, local_id),
    )
    await db.commit()

    # Prime the cache with the linked summary.
    pre = await admin_client.get(f"/api/sessions/{local_id}/summary")
    assert pre.status_code == 200

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ? AND key_family = ?",
        (local_id, "session_summary"),
    )
    assert (await cur.fetchone())["n"] == 1

    resp = await admin_client.post(
        f"/api/sessions/{local_id}/link-imported",
        data={"imported_race_id": 0},
    )
    assert resp.status_code == 200

    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ? AND key_family = ?",
        (local_id, "session_summary"),
    )
    assert (await cur.fetchone())["n"] == 0
