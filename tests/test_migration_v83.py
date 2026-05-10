"""Tests for migration v83 — dedupe race_videos and add UNIQUE index."""

from __future__ import annotations

import contextlib

import aiosqlite
import pytest

from helmlog.storage import _MIGRATIONS, _split_migration_sql


async def _apply_migration(db: aiosqlite.Connection, version: int) -> None:
    for stmt in _split_migration_sql(_MIGRATIONS[version]):
        upper = stmt.lstrip().upper()
        is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
        if is_alter_add:
            with contextlib.suppress(aiosqlite.OperationalError):
                await db.execute(stmt)
        else:
            await db.execute(stmt)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,))
    await db.commit()


async def _build_db_at(version: int) -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for v in sorted(_MIGRATIONS):
        if v > version:
            break
        await _apply_migration(db, v)
    return db


async def _seed_race(db: aiosqlite.Connection, race_id: int) -> None:
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc)"
        " VALUES (?, ?, 'TestEvent', 1, '2026-04-30', '2026-04-30T01:25:03+00:00')",
        (race_id, f"race-{race_id}"),
    )


async def _seed_video(
    db: aiosqlite.Connection,
    race_id: int,
    video_id: str,
    *,
    sync_offset_s: float,
    created_at: str,
) -> int:
    cur = await db.execute(
        "INSERT INTO race_videos"
        " (race_id, youtube_url, video_id, title, label,"
        "  sync_utc, sync_offset_s, duration_s, created_at, user_id)"
        " VALUES (?, ?, ?, '', '',"
        "  '2026-04-30T01:25:03+00:00', ?, 3600.0, ?, NULL)",
        (race_id, f"https://youtu.be/{video_id}", video_id, sync_offset_s, created_at),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


@pytest.mark.asyncio
async def test_v83_collapses_duplicate_rows_keeping_latest_created_at() -> None:
    """Three rows for the same (race_id, video_id) collapse to one — the
    latest created_at — preserving the most recent sync calibration."""
    db = await _build_db_at(82)
    try:
        await _seed_race(db, 1)
        oldest = await _seed_video(
            db, 1, "vid-aaaaaaa", sync_offset_s=0.0, created_at="2026-04-30T09:18:21+00:00"
        )
        middle = await _seed_video(
            db, 1, "vid-aaaaaaa", sync_offset_s=100.0, created_at="2026-04-30T09:18:28+00:00"
        )
        latest = await _seed_video(
            db, 1, "vid-aaaaaaa", sync_offset_s=243.0, created_at="2026-04-30T09:31:12+00:00"
        )
        await db.commit()

        await _apply_migration(db, 83)

        async with db.execute("SELECT id, sync_offset_s FROM race_videos WHERE race_id = 1") as cur:
            rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == latest
        assert rows[0]["sync_offset_s"] == 243.0
        assert oldest != latest and middle != latest
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v83_preserves_distinct_video_ids() -> None:
    db = await _build_db_at(82)
    try:
        await _seed_race(db, 1)
        await _seed_video(
            db, 1, "vid-aaaaaaa", sync_offset_s=0.0, created_at="2026-04-30T09:18:21+00:00"
        )
        await _seed_video(
            db, 1, "vid-bbbbbbb", sync_offset_s=0.0, created_at="2026-04-30T09:18:21+00:00"
        )
        await db.commit()

        await _apply_migration(db, 83)

        async with db.execute(
            "SELECT video_id FROM race_videos WHERE race_id = 1 ORDER BY video_id"
        ) as cur:
            rows = await cur.fetchall()
        assert [r["video_id"] for r in rows] == ["vid-aaaaaaa", "vid-bbbbbbb"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v83_unique_index_blocks_future_duplicates() -> None:
    db = await _build_db_at(83)
    try:
        await _seed_race(db, 1)
        await _seed_video(
            db, 1, "vid-aaaaaaa", sync_offset_s=0.0, created_at="2026-04-30T09:18:21+00:00"
        )
        await db.commit()

        with pytest.raises(aiosqlite.IntegrityError):
            await _seed_video(
                db,
                1,
                "vid-aaaaaaa",
                sync_offset_s=243.0,
                created_at="2026-04-30T09:18:22+00:00",
            )
    finally:
        await db.close()
