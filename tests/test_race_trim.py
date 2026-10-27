"""Manual "trim non-race data" preview + apply (#11).

Spec: issue #11 v2 — decision table in the /spec comment. Each test maps to
a row: preview detects/doesn't-detect a cutoff, trim closes an open race or
moves back an already-closed race's end_utc, and detaches (not deletes)
trailing telemetry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

_START_LAT = 37.80
_START_LON = -122.27


async def _insert_track_point(
    storage: Storage,
    race_id: int | None,
    ts: datetime,
    *,
    near: bool,
    close_hauled: bool,
) -> None:
    """Insert one position + wind sample so #812's finish heuristic has
    something to scan. ``near``/``close_hauled`` mirror test_polar.py's
    TestDetectFinishHeuristic sample shape."""
    lat = _START_LAT + (0.001 if near else 0.03)  # ~140 m vs ~3.3 km from start
    twa = 40.0 if close_hauled else 140.0
    db = storage._conn()  # noqa: SLF001 — white-box telemetry seed
    await db.execute(
        "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (ts.isoformat(), 1, lat, _START_LON + 0.001, race_id),
    )
    await db.execute(
        "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts.isoformat(), 1, 10.0, twa, 0, race_id),  # reference=0 -> boat-referenced TWA
    )
    await db.commit()


async def _seed_race_with_finish_and_tail(
    storage: Storage, *, closed: bool
) -> tuple[int, datetime]:
    """A race that races normally for 200s, finishes (close-hauled near start),
    then has a long non-racing tail (sailed away, downwind, still logging).
    Returns (race_id, expected_cutoff)."""
    gun = datetime.now(UTC) - timedelta(hours=3)
    race = await storage.start_race("CYC", gun, gun.date().isoformat(), 1, "R1")
    for t in range(0, 201, 10):
        await _insert_track_point(
            storage, race.id, gun + timedelta(seconds=t), near=True, close_hauled=True
        )
    tail_end_offset = 3 * 3600  # 3 hours of post-finish junk, matching the reported bug
    for t in range(210, tail_end_offset, 600):  # every 10 min
        await _insert_track_point(
            storage, race.id, gun + timedelta(seconds=t), near=False, close_hauled=False
        )
    expected_cutoff = gun + timedelta(seconds=200)
    if closed:
        await storage.end_race(race.id, gun + timedelta(seconds=tail_end_offset))
    return race.id, expected_cutoff


# ---------------------------------------------------------------------------
# preview_race_trim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_open_race_detects_cutoff(storage: Storage) -> None:
    race_id, expected_cutoff = await _seed_race_with_finish_and_tail(storage, closed=False)
    preview = await storage.preview_race_trim(race_id, datetime.now(UTC))
    assert preview.detected is True
    assert preview.cutoff_utc == expected_cutoff
    assert preview.rows_by_table["positions"] > 0
    assert preview.duration_removed_s is not None and preview.duration_removed_s > 0


@pytest.mark.asyncio
async def test_preview_closed_race_detects_cutoff(storage: Storage) -> None:
    race_id, expected_cutoff = await _seed_race_with_finish_and_tail(storage, closed=True)
    preview = await storage.preview_race_trim(race_id, datetime.now(UTC))
    assert preview.detected is True
    assert preview.cutoff_utc == expected_cutoff


@pytest.mark.asyncio
async def test_preview_no_plausible_finish_not_detected(storage: Storage) -> None:
    """Distance race: never close-hauled-near-start -> nothing to detect."""
    gun = datetime.now(UTC) - timedelta(hours=1)
    race = await storage.start_race("CYC", gun, gun.date().isoformat(), 1, "R1")
    for t in range(0, 3601, 300):
        await _insert_track_point(
            storage, race.id, gun + timedelta(seconds=t), near=False, close_hauled=False
        )
    preview = await storage.preview_race_trim(race.id, datetime.now(UTC))
    assert preview.detected is False
    assert preview.cutoff_utc is None
    assert preview.rows_by_table == {}


@pytest.mark.asyncio
async def test_preview_empty_race_not_detected(storage: Storage) -> None:
    now = datetime.now(UTC)
    race = await storage.start_race("CYC", now - timedelta(minutes=5), "2026-06-09", 1, "R1")
    preview = await storage.preview_race_trim(race.id, now)
    assert preview.detected is False


@pytest.mark.asyncio
async def test_preview_missing_race_not_detected(storage: Storage) -> None:
    preview = await storage.preview_race_trim(99999, datetime.now(UTC))
    assert preview.detected is False


# ---------------------------------------------------------------------------
# trim_race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trim_open_race_closes_at_cutoff(storage: Storage) -> None:
    race_id, cutoff = await _seed_race_with_finish_and_tail(storage, closed=False)
    detached = await storage.trim_race(race_id, cutoff)
    assert detached > 0
    race = await storage.get_race(race_id)
    assert race is not None
    assert race.end_utc == cutoff


@pytest.mark.asyncio
async def test_trim_detaches_rows_after_cutoff_not_deletes(storage: Storage) -> None:
    race_id, cutoff = await _seed_race_with_finish_and_tail(storage, closed=False)
    before_count_cur = await storage._conn().execute("SELECT COUNT(*) AS n FROM positions")  # noqa: SLF001
    before_total = (await before_count_cur.fetchone())["n"]

    await storage.trim_race(race_id, cutoff)

    after_count_cur = await storage._conn().execute("SELECT COUNT(*) AS n FROM positions")  # noqa: SLF001
    after_total = (await after_count_cur.fetchone())["n"]
    assert after_total == before_total  # nothing deleted

    tagged_cur = await storage._conn().execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM positions WHERE race_id = ?", (race_id,)
    )
    tagged_after = (await tagged_cur.fetchone())["n"]
    detached_cur = await storage._conn().execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM positions WHERE race_id IS NULL"
    )
    detached_after = (await detached_cur.fetchone())["n"]
    assert detached_after > 0
    assert tagged_after > 0  # the pre-cutoff racing data is still tagged


@pytest.mark.asyncio
async def test_trim_leaves_rows_before_cutoff_tagged(storage: Storage) -> None:
    race_id, cutoff = await _seed_race_with_finish_and_tail(storage, closed=False)
    await storage.trim_race(race_id, cutoff)
    cur = await storage._conn().execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM positions WHERE race_id = ? AND ts <= ?",
        (race_id, cutoff.isoformat()),
    )
    assert (await cur.fetchone())["n"] > 0


@pytest.mark.asyncio
async def test_trim_closed_race_moves_end_utc_back(storage: Storage) -> None:
    race_id, cutoff = await _seed_race_with_finish_and_tail(storage, closed=True)
    race_before = await storage.get_race(race_id)
    assert race_before is not None
    detached = await storage.trim_race(race_id, cutoff)
    assert detached > 0
    race_after = await storage.get_race(race_id)
    assert race_after is not None
    assert race_after.end_utc == cutoff
    assert race_after.end_utc < race_before.end_utc  # type: ignore[operator]


@pytest.mark.asyncio
async def test_trim_noop_when_cutoff_after_end_utc(storage: Storage) -> None:
    """Manual cutoff picked past the existing end_utc -> nothing to trim."""
    now = datetime.now(UTC)
    race = await storage.start_race("CYC", now - timedelta(hours=1), "2026-06-09", 1, "R1")
    await _insert_track_point(
        storage, race.id, now - timedelta(minutes=30), near=True, close_hauled=True
    )
    await storage.end_race(race.id, now - timedelta(minutes=1))
    race_before = await storage.get_race(race.id)
    assert race_before is not None and race_before.end_utc is not None

    detached = await storage.trim_race(race.id, race_before.end_utc + timedelta(minutes=5))
    assert detached == 0
    race_after = await storage.get_race(race.id)
    assert race_after is not None
    assert race_after.end_utc == race_before.end_utc


@pytest.mark.asyncio
async def test_trim_missing_race_returns_zero(storage: Storage) -> None:
    assert await storage.trim_race(99999, datetime.now(UTC)) == 0
