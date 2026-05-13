"""Tests for web response cache (#594, PR 1).

Covers EARS requirements 1-5 from the spec:
 1. data_hash is stable across processes.
 2. races INSERT/UPDATE/DELETE trigger invalidation in-transaction.
 3. Cache write failure is logged and swallowed.
 4. Corrupt blob is treated as miss and deleted.
 5. T2 cache rows are capped and evicted oldest-first.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.cache import MAX_CACHE_ROWS_DEFAULT, WebCache, compute_race_data_hash

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Req. 1 — data_hash stability
# ---------------------------------------------------------------------------


def test_compute_race_data_hash_is_stable_across_calls() -> None:
    start = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 4, 1, 13, 30, tzinfo=UTC)
    a = compute_race_data_hash(race_id=42, start_utc=start, end_utc=end, row_count=1234)
    b = compute_race_data_hash(race_id=42, start_utc=start, end_utc=end, row_count=1234)
    assert a == b
    assert len(a) == 16


def test_compute_race_data_hash_changes_on_any_input() -> None:
    start = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 4, 1, 13, 30, tzinfo=UTC)
    base = compute_race_data_hash(race_id=42, start_utc=start, end_utc=end, row_count=100)
    assert compute_race_data_hash(race_id=43, start_utc=start, end_utc=end, row_count=100) != base
    assert (
        compute_race_data_hash(
            race_id=42, start_utc=start + timedelta(seconds=1), end_utc=end, row_count=100
        )
        != base
    )
    assert (
        compute_race_data_hash(
            race_id=42, start_utc=start, end_utc=end + timedelta(seconds=1), row_count=100
        )
        != base
    )
    assert compute_race_data_hash(race_id=42, start_utc=start, end_utc=end, row_count=101) != base


def test_compute_race_data_hash_accepts_null_end_utc() -> None:
    start = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    assert compute_race_data_hash(race_id=42, start_utc=start, end_utc=None, row_count=0)


def test_compute_race_data_hash_flips_on_calibration_change() -> None:
    """#711: a change to the calibration hash must produce a fresh data
    hash so per-race caches miss and recompute with the new offsets."""
    start = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 4, 1, 13, 30, tzinfo=UTC)
    base = compute_race_data_hash(
        race_id=42, start_utc=start, end_utc=end, row_count=100, calibration_hash="cal-v1"
    )
    changed = compute_race_data_hash(
        race_id=42, start_utc=start, end_utc=end, row_count=100, calibration_hash="cal-v2"
    )
    assert base != changed


# ---------------------------------------------------------------------------
# T1 process LRU with TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_get_returns_none_on_miss(storage: Storage) -> None:
    cache = WebCache(storage)
    assert cache.t1_get("missing") is None


@pytest.mark.asyncio
async def test_t1_put_then_get_returns_value(storage: Storage) -> None:
    cache = WebCache(storage)
    cache.t1_put("foo", {"x": 1}, ttl_seconds=60)
    assert cache.t1_get("foo") == {"x": 1}


@pytest.mark.asyncio
async def test_t1_expired_entry_returns_none(storage: Storage) -> None:
    cache = WebCache(storage)
    cache.t1_put("foo", {"x": 1}, ttl_seconds=0)
    # tick the clock past zero
    await asyncio.sleep(0.01)
    assert cache.t1_get("foo") is None


# ---------------------------------------------------------------------------
# T2 SQLite blob cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_miss_returns_none(storage: Storage) -> None:
    cache = WebCache(storage)
    assert await cache.t2_get("session_summary", race_id=1, data_hash="abc") is None


@pytest.mark.asyncio
async def test_t2_hit_returns_value(storage: Storage) -> None:
    cache = WebCache(storage)
    await cache.t2_put(
        "session_summary",
        race_id=1,
        data_hash="abc",
        value={"distance": 12.3},
    )
    assert await cache.t2_get("session_summary", race_id=1, data_hash="abc") == {"distance": 12.3}


@pytest.mark.asyncio
async def test_t2_stale_hash_returns_none(storage: Storage) -> None:
    cache = WebCache(storage)
    await cache.t2_put("session_summary", race_id=1, data_hash="abc", value={"v": 1})
    # Different hash — underlying data has changed
    assert await cache.t2_get("session_summary", race_id=1, data_hash="def") is None


@pytest.mark.asyncio
async def test_t2_put_keeps_distinct_hashes_until_invalidation(storage: Storage) -> None:
    """Post-v84 the PK includes data_hash, so two writes with different
    hashes leave both rows in place. The old hash can never be looked up
    by anyone who already has the new data — it stays until the race
    mutation invalidation hook drops everything for the race id, or the
    row-cap evictor sweeps it out."""
    cache = WebCache(storage)
    await cache.t2_put("session_summary", race_id=1, data_hash="abc", value={"v": 1})
    await cache.t2_put("session_summary", race_id=1, data_hash="def", value={"v": 2})
    assert await cache.t2_get("session_summary", race_id=1, data_hash="abc") == {"v": 1}
    assert await cache.t2_get("session_summary", race_id=1, data_hash="def") == {"v": 2}
    # An invalidate flushes both.
    await cache.invalidate(1)
    assert await cache.t2_get("session_summary", race_id=1, data_hash="abc") is None
    assert await cache.t2_get("session_summary", race_id=1, data_hash="def") is None


# ---------------------------------------------------------------------------
# Req. 4 — corrupt blob is treated as miss and deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_corrupt_blob_is_evicted_and_miss(storage: Storage) -> None:
    cache = WebCache(storage)
    await cache.t2_put("summary", race_id=7, data_hash="h", value={"ok": True})
    # Corrupt the stored blob directly.
    db = storage._conn()  # type: ignore[attr-defined]
    await db.execute(
        "UPDATE web_cache SET blob = ? WHERE key_family = ? AND race_id = ?",
        ("{not valid json", "summary", 7),
    )
    await db.commit()

    # First read: treat as miss and delete row.
    assert await cache.t2_get("summary", race_id=7, data_hash="h") is None
    cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache WHERE race_id = 7")
    row = await cur.fetchone()
    assert row["n"] == 0


# ---------------------------------------------------------------------------
# invalidate(race_id) covers T1 + T2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_drops_t2_entries_for_race(storage: Storage) -> None:
    cache = WebCache(storage)
    await cache.t2_put("session_summary", race_id=1, data_hash="a", value={"v": 1})
    await cache.t2_put("session_track", race_id=1, data_hash="b", value={"v": 2})
    await cache.t2_put("session_summary", race_id=2, data_hash="c", value={"v": 3})

    await cache.invalidate(1)

    assert await cache.t2_get("session_summary", race_id=1, data_hash="a") is None
    assert await cache.t2_get("session_track", race_id=1, data_hash="b") is None
    # Other race is untouched
    assert await cache.t2_get("session_summary", race_id=2, data_hash="c") == {"v": 3}


@pytest.mark.asyncio
async def test_invalidate_drops_t1_entries_for_race(storage: Storage) -> None:
    cache = WebCache(storage)
    cache.t1_put_for_race("session_detail", race_id=1, value={"v": 1}, ttl_seconds=60)
    cache.t1_put_for_race("session_detail", race_id=2, value={"v": 2}, ttl_seconds=60)
    cache.t1_put("session_list:page=0", {"v": "list"}, ttl_seconds=60)

    await cache.invalidate(1)

    assert cache.t1_get_for_race("session_detail", race_id=1) is None
    # Other race preserved
    assert cache.t1_get_for_race("session_detail", race_id=2) == {"v": 2}
    # Global list-style keys are not race-scoped and stay put here; the caller
    # is expected to drop them via invalidate_family when needed.
    assert cache.t1_get("session_list:page=0") == {"v": "list"}


@pytest.mark.asyncio
async def test_invalidate_family_clears_all_keys_in_family(storage: Storage) -> None:
    cache = WebCache(storage)
    cache.t1_put("session_list:page=0", {"v": 0}, ttl_seconds=60)
    cache.t1_put("session_list:page=1", {"v": 1}, ttl_seconds=60)
    cache.t1_put("other:page=0", {"v": 99}, ttl_seconds=60)

    cache.t1_invalidate_family("session_list")

    assert cache.t1_get("session_list:page=0") is None
    assert cache.t1_get("session_list:page=1") is None
    assert cache.t1_get("other:page=0") == {"v": 99}


# ---------------------------------------------------------------------------
# Req. 5 — MAX_CACHE_ROWS eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_evicts_oldest_over_cap(storage: Storage) -> None:
    cap = 3
    cache = WebCache(storage, max_rows=cap)
    # Insert 5 rows with monotonically increasing created_utc.
    for i in range(5):
        await cache.t2_put(
            "summary",
            race_id=i,
            data_hash=f"h{i}",
            value={"i": i},
            _now=datetime(2026, 1, 1, 0, 0, i, tzinfo=UTC),
        )

    db = storage._conn()  # type: ignore[attr-defined]
    cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache")
    row = await cur.fetchone()
    assert row["n"] == cap

    # Oldest two rows (i=0, i=1) evicted
    assert await cache.t2_get("summary", race_id=0, data_hash="h0") is None
    assert await cache.t2_get("summary", race_id=1, data_hash="h1") is None
    # Newest three survive
    for i in (2, 3, 4):
        assert await cache.t2_get("summary", race_id=i, data_hash=f"h{i}") == {"i": i}


def test_max_cache_rows_default_is_1000() -> None:
    assert MAX_CACHE_ROWS_DEFAULT == 1000


# ---------------------------------------------------------------------------
# Req. 3 — cache failures are logged and swallowed (no user-facing raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_put_swallows_db_errors(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = WebCache(storage)

    async def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(cache, "_write_row", _boom)

    # Must not raise
    await cache.t2_put("summary", race_id=1, data_hash="h", value={"v": 1})


@pytest.mark.asyncio
async def test_t2_get_swallows_db_errors(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = WebCache(storage)

    async def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("db closed")

    monkeypatch.setattr(cache, "_read_row", _boom)

    # Must return None, not raise
    assert await cache.t2_get("summary", race_id=1, data_hash="h") is None


# ---------------------------------------------------------------------------
# Req. 2 — storage race-mutation paths invalidate
# ---------------------------------------------------------------------------


class _RecordingCache:
    """Minimal stand-in that just captures invalidate() calls."""

    def __init__(self) -> None:
        self.invalidations: list[int] = []

    async def invalidate(self, race_id: int) -> None:
        self.invalidations.append(race_id)


@pytest.mark.asyncio
async def test_start_race_invalidates_cache(storage: Storage) -> None:
    recorder = _RecordingCache()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    race = await storage.start_race(
        event="E",
        start_utc=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        date_str="2026-04-01",
        race_num=1,
        name="R1",
    )

    assert race.id in recorder.invalidations


@pytest.mark.asyncio
async def test_end_race_invalidates_cache(storage: Storage) -> None:
    race = await storage.start_race(
        event="E",
        start_utc=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        date_str="2026-04-01",
        race_num=1,
        name="R1",
    )
    recorder = _RecordingCache()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    await storage.end_race(race.id, datetime(2026, 4, 1, 11, 0, tzinfo=UTC))

    assert race.id in recorder.invalidations


@pytest.mark.asyncio
async def test_rename_race_invalidates_cache(storage: Storage) -> None:
    race = await storage.start_race(
        event="E",
        start_utc=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        date_str="2026-04-01",
        race_num=1,
        name="Race Alpha",
    )
    await storage.end_race(race.id, datetime(2026, 4, 1, 11, 0, tzinfo=UTC))

    recorder = _RecordingCache()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    await storage.rename_race(race.id, new_name="Race Beta")

    assert race.id in recorder.invalidations


@pytest.mark.asyncio
async def test_delete_race_session_invalidates_cache(storage: Storage) -> None:
    race = await storage.start_race(
        event="E",
        start_utc=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        date_str="2026-04-01",
        race_num=1,
        name="R1",
    )
    await storage.end_race(race.id, datetime(2026, 4, 1, 11, 0, tzinfo=UTC))

    recorder = _RecordingCache()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    await storage.delete_race_session(race.id)

    assert race.id in recorder.invalidations


@pytest.mark.asyncio
async def test_import_race_invalidates_cache(storage: Storage) -> None:
    recorder = _RecordingCache()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    race_id = await storage.import_race(
        name="Imported",
        event="E",
        race_num=1,
        date_str="2026-04-01",
        start_utc=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        end_utc=datetime(2026, 4, 1, 11, 0, tzinfo=UTC),
        session_type="race",
        source="clubspot",
        source_id="abc",
    )

    assert race_id in recorder.invalidations


# ---------------------------------------------------------------------------
# Serialization must reject PII-category fields (Req. 9 — partial coverage; full
# data-licensing integration tests live in tests/integration/)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_put_round_trip_preserves_json(storage: Storage) -> None:
    cache = WebCache(storage)
    value = {
        "geometry": {"type": "LineString", "coordinates": [[1.0, 2.0], [3.0, 4.0]]},
        "metrics": {"distance_nm": 12.5, "samples": 1234},
    }
    await cache.t2_put("session_track", race_id=1, data_hash="h", value=value)
    got = await cache.t2_get("session_track", race_id=1, data_hash="h")
    assert got == value
    assert json.dumps(got, sort_keys=True) == json.dumps(value, sort_keys=True)


# ---------------------------------------------------------------------------
# v84 — multiple distinct hashes coexist for the same global key_family
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_global_distinct_hashes_coexist(storage: Storage) -> None:
    """Two cross-session overlay selections with different content hashes
    must not evict each other. Pre-v84 the (key_family, race_id=0) PK
    held only one row, so any new selection blew away the previous
    cached payload."""
    cache = WebCache(storage)
    await cache.t2_put_global(
        "maneuvers_overlay", data_hash="h_a", value={"sel": "A"}, ttl_seconds=3600
    )
    await cache.t2_put_global(
        "maneuvers_overlay", data_hash="h_b", value={"sel": "B"}, ttl_seconds=3600
    )

    a = await cache.t2_get_global("maneuvers_overlay", data_hash="h_a")
    b = await cache.t2_get_global("maneuvers_overlay", data_hash="h_b")
    assert a == {"sel": "A"}
    assert b == {"sel": "B"}


@pytest.mark.asyncio
async def test_t2_race_keyed_distinct_hashes_coexist(storage: Storage) -> None:
    """Race-keyed entries also benefit: writing a new hash for a race
    that hasn't been invalidated yet leaves the prior hash readable
    until the race-mutation hook drops everything for that race."""
    cache = WebCache(storage)
    await cache.t2_put("session_summary", race_id=42, data_hash="h_old", value={"v": 1})
    await cache.t2_put("session_summary", race_id=42, data_hash="h_new", value={"v": 2})

    old = await cache.t2_get("session_summary", race_id=42, data_hash="h_old")
    new = await cache.t2_get("session_summary", race_id=42, data_hash="h_new")
    assert old == {"v": 1}
    assert new == {"v": 2}


@pytest.mark.asyncio
async def test_invalidate_race_drops_all_hashes_for_that_race(storage: Storage) -> None:
    """Race-mutation invalidation still works as before — every cached
    hash for the touched race id is dropped."""
    cache = WebCache(storage)
    await cache.t2_put("session_summary", race_id=99, data_hash="h_a", value={"v": 1})
    await cache.t2_put("session_summary", race_id=99, data_hash="h_b", value={"v": 2})

    await cache.invalidate(99)

    assert await cache.t2_get("session_summary", race_id=99, data_hash="h_a") is None
    assert await cache.t2_get("session_summary", race_id=99, data_hash="h_b") is None
