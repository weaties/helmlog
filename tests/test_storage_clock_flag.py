"""Storage persistence of GPS-clock provenance on races (#794)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

BASE = datetime(2026, 6, 16, 1, 0, 0, tzinfo=UTC)


def _pos(k: int) -> tuple[float, float]:
    # Distinct lat/lon per course index so a time shift is spatially visible.
    return (47.60 + k * 1e-4, -122.40 + k * 1e-4)


async def _seed_race_and_vakaros(storage: Storage, *, race_clock_skew_s: int) -> tuple[int, int]:
    """Seed a 900s live race + a same-course Vakaros session (GPS-correct).

    The race recording's clock is offset from true time by ``race_clock_skew_s``
    (negative = slow, the race-201 condition). Windows are long relative to the
    skew so the time-overlap matcher still links them. Returns (race_id, sid).
    """
    db = storage._conn()  # noqa: SLF001
    race = await storage.start_race(
        event="Test",
        start_utc=BASE + timedelta(seconds=race_clock_skew_s),
        date_str="2026-06-16",
        race_num=1,
        name=f"20260616-skew{race_clock_skew_s}-1",
    )
    await db.execute(
        "UPDATE races SET end_utc = ? WHERE id = ?",
        ((BASE + timedelta(seconds=900 + race_clock_skew_s)).isoformat(), race.id),
    )
    pos_rows = [
        (
            (BASE + timedelta(seconds=k + race_clock_skew_s)).isoformat(),
            0,
            *_pos(k),
            race.id,
        )
        for k in range(0, 900, 2)
    ]
    await db.executemany(
        "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
        " VALUES (?, ?, ?, ?, ?)",
        pos_rows,
    )
    cur = await db.execute(
        "INSERT INTO vakaros_sessions (source_hash, source_file, start_utc, end_utc,"
        " ingested_at) VALUES (?, ?, ?, ?, ?)",
        (
            f"h{race_clock_skew_s}".ljust(64, "0"),
            "skew.vkx",
            BASE.isoformat(),
            (BASE + timedelta(seconds=900)).isoformat(),
            BASE.isoformat(),
        ),
    )
    sid = int(cur.lastrowid)  # type: ignore[arg-type]
    vak_rows = [
        (sid, (BASE + timedelta(seconds=k)).isoformat(), *_pos(k), 3.0, 45.0)
        for k in range(0, 900, 2)
    ]
    await db.executemany(
        "INSERT INTO vakaros_positions (session_id, ts, latitude_deg, longitude_deg,"
        " sog_mps, cog_deg) VALUES (?, ?, ?, ?, ?, ?)",
        vak_rows,
    )
    await db.commit()
    return race.id, sid


@pytest.mark.asyncio
async def test_import_skew_flags_recording_unverified(storage: Storage) -> None:
    # Recording clock 5 min slow vs the GPS-correct Vakaros track.
    race_id, sid = await _seed_race_and_vakaros(storage, race_clock_skew_s=-300)
    linked = await storage.match_vakaros_session(sid)
    assert race_id in linked
    refreshed = await storage.get_race(race_id)
    assert refreshed is not None
    assert refreshed.clock_flag == "unverified"


@pytest.mark.asyncio
async def test_aligned_tracks_leave_clock_flag_untouched(storage: Storage) -> None:
    race_id, sid = await _seed_race_and_vakaros(storage, race_clock_skew_s=0)
    linked = await storage.match_vakaros_session(sid)
    assert race_id in linked
    refreshed = await storage.get_race(race_id)
    assert refreshed is not None
    assert refreshed.clock_flag is None


@pytest.mark.asyncio
async def test_clock_flag_defaults_to_none(storage: Storage) -> None:
    race = await storage.start_race(
        event="Test",
        start_utc=datetime(2026, 6, 16, 1, 27, tzinfo=UTC),
        date_str="2026-06-16",
        race_num=1,
        name="20260616-Test-1",
    )
    # A freshly started race has no clock provenance recorded yet (NULL),
    # so the UI shows no banner until the logger reports a flag.
    fetched = await storage.get_race(race.id)
    assert fetched is not None
    assert fetched.clock_flag is None


@pytest.mark.asyncio
async def test_set_and_read_clock_flag(storage: Storage) -> None:
    race = await storage.start_race(
        event="Test",
        start_utc=datetime(2026, 6, 16, 1, 27, tzinfo=UTC),
        date_str="2026-06-16",
        race_num=1,
        name="20260616-Test-1",
    )
    await storage.set_race_clock_flag(race.id, "corrected")
    fetched = await storage.get_race(race.id)
    assert fetched is not None
    assert fetched.clock_flag == "corrected"

    # Idempotent overwrite as the session's state evolves.
    await storage.set_race_clock_flag(race.id, "ref_lost")
    fetched = await storage.get_race(race.id)
    assert fetched is not None
    assert fetched.clock_flag == "ref_lost"
