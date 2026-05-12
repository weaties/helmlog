"""Tests for ETag/304 + T2 caching on session summary/track/wind-field (#594, PR 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


_START = datetime(2026, 2, 26, 14, 0, 0, tzinfo=UTC)
_END = datetime(2026, 2, 26, 14, 30, 0, tzinfo=UTC)


async def _seed_completed_race(storage: Storage) -> int:
    """Insert a completed race with a handful of position rows. Returns race_id."""
    race = await storage.start_race(
        event="E",
        start_utc=_START,
        date_str="2026-02-26",
        race_num=1,
        name="Race 1",
    )
    # Seed a few positions inside the window, tagged with race_id.
    db = storage._conn()
    for i in range(5):
        ts = (_START + timedelta(seconds=i * 30)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 0, 37.7 + i * 0.001, -122.4 + i * 0.001, race.id),
        )
    await db.commit()
    await storage.end_race(race.id, _END)
    return race.id


@pytest.mark.asyncio
async def test_summary_emits_etag_and_cache_control(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/summary")
    assert resp.status_code == 200
    etag = resp.headers.get("etag")
    assert etag, f"expected ETag header, got: {dict(resp.headers)}"
    assert etag.startswith('"') and etag.endswith('"')
    assert len(etag.strip('"')) == 16  # 16-char hex digest
    assert "must-revalidate" in resp.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_summary_returns_304_on_if_none_match(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get(f"/api/sessions/{race_id}/summary")
        etag = r1.headers["etag"]
        r2 = await client.get(f"/api/sessions/{race_id}/summary", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers.get("etag") == etag


@pytest.mark.asyncio
async def test_summary_returns_200_when_if_none_match_stale(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/sessions/{race_id}/summary",
            headers={"If-None-Match": '"deadbeefcafebabe"'},
        )
    assert resp.status_code == 200
    assert resp.json()["track"]  # body regenerated


@pytest.mark.asyncio
async def test_summary_cache_hit_returns_same_body_as_miss(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        miss = await client.get(f"/api/sessions/{race_id}/summary")
        hit = await client.get(f"/api/sessions/{race_id}/summary")
    assert miss.status_code == 200
    assert hit.status_code == 200
    assert miss.headers["etag"] == hit.headers["etag"]
    assert miss.json() == hit.json()


@pytest.mark.asyncio
async def test_summary_etag_changes_after_race_mutation(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rename_race → _invalidate_race_cache hook → new data_hash on next request."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get(f"/api/sessions/{race_id}/summary")
        etag1 = r1.headers["etag"]

        # end_utc doesn't change under rename, so data_hash is stable — we
        # need a race-row field the hash depends on. Add more position rows
        # instead so row_count changes, exercising the same hook path because
        # start/end_utc are the canonical fields. Either way, rename_race
        # fires the invalidation hook and drops the blob.
        db = storage._conn()
        for i in range(5, 10):
            ts = (_START + timedelta(seconds=i * 30)).isoformat()
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (ts, 0, 37.7, -122.4, race_id),
            )
        await db.commit()
        await storage.rename_race(race_id, new_name="Race 1 Renamed")

        r2 = await client.get(f"/api/sessions/{race_id}/summary")
        etag2 = r2.headers["etag"]
    assert etag1 != etag2


@pytest.mark.asyncio
async def test_summary_404_on_missing_race(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/9999/summary")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_track_emits_etag_and_304(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get(f"/api/sessions/{race_id}/track")
        assert r1.status_code == 200
        etag = r1.headers["etag"]
        r2 = await client.get(f"/api/sessions/{race_id}/track", headers={"If-None-Match": etag})
    assert r2.status_code == 304


@pytest.mark.asyncio
async def test_replay_emits_etag_and_304(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """/api/sessions/{id}/replay rolls up set/drift + polar grades + instrument
    series. Same caching semantics as summary/track.
    """
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.get(f"/api/sessions/{race_id}/replay")
        assert r1.status_code == 200
        payload = r1.json()
        assert "samples" in payload
        etag = r1.headers["etag"]
        assert etag
        r2 = await client.get(f"/api/sessions/{race_id}/replay", headers={"If-None-Match": etag})
    assert r2.status_code == 304


@pytest.mark.asyncio
async def test_replay_cache_blob_written_and_invalidated(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/api/sessions/{race_id}/replay")

    db = storage._conn()
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ? AND key_family = ?",
        (race_id, "session_replay_v3"),
    )
    assert (await cur.fetchone())["n"] == 1

    # rename fires the invalidation hook — replay blob must be dropped.
    await storage.rename_race(race_id, new_name="After Rename")
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ? AND key_family = ?",
        (race_id, "session_replay_v3"),
    )
    assert (await cur.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_t2_blob_written_on_summary_miss(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a summary request, web_cache must contain the corresponding blob."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/api/sessions/{race_id}/summary")

    db = storage._conn()
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ? AND key_family = ?",
        (race_id, "session_summary"),
    )
    row = await cur.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_race_end_invalidation_clears_summary_blob(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race_id = await _seed_completed_race(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/api/sessions/{race_id}/summary")

    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ?", (race_id,))
    assert (await cur.fetchone())["n"] >= 1

    # rename_race goes through the invalidation hook
    await storage.rename_race(race_id, new_name="Post Rename")

    cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache WHERE race_id = ?", (race_id,))
    assert (await cur.fetchone())["n"] == 0
