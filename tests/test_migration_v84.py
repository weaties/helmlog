"""Tests for migration v84 — web_cache PK now includes data_hash."""

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


@pytest.mark.asyncio
async def test_v84_pk_includes_data_hash() -> None:
    db = await _build_db_at(84)
    try:
        async with db.execute("PRAGMA table_info(web_cache)") as cur:
            cols = {r[1]: r["pk"] for r in await cur.fetchall()}
        # data_hash is part of the PK alongside key_family and race_id.
        assert cols["key_family"] > 0
        assert cols["race_id"] > 0
        assert cols["data_hash"] > 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v84_allows_distinct_hashes_for_same_global_family() -> None:
    """The whole point of the migration: two global (race_id=0) rows with
    the same key_family but different data_hash must coexist instead of
    one overwriting the other."""
    db = await _build_db_at(84)
    try:
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc)"
            " VALUES (?, ?, ?, ?, ?)",
            ("maneuvers_overlay", 0, "hash_a", '{"a":1}', "2026-04-20T00:00:00+00:00"),
        )
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc)"
            " VALUES (?, ?, ?, ?, ?)",
            ("maneuvers_overlay", 0, "hash_b", '{"b":2}', "2026-04-20T00:00:01+00:00"),
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM web_cache WHERE key_family = 'maneuvers_overlay'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v84_drops_pre_migration_rows() -> None:
    """Cache content is best-effort and the migration drops it; downstream
    requests rebuild on demand. Verify that v83 rows do not leak through."""
    db = await _build_db_at(83)
    try:
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc)"
            " VALUES (?, ?, ?, ?, ?)",
            ("session_summary", 1, "old_hash", "{}", "2026-04-20T00:00:00+00:00"),
        )
        await db.commit()
        await _apply_migration(db, 84)
        async with db.execute("SELECT COUNT(*) FROM web_cache") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v84_migration_applied_on_fresh_db() -> None:
    from helmlog.storage import Storage, StorageConfig

    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    try:
        assert s._db is not None
        async with s._db.execute("SELECT 1 FROM schema_version WHERE version = 84") as cur:
            row = await cur.fetchone()
        assert row is not None
    finally:
        await s.close()
