"""Tests for src/logger/polar.py — pure helpers and integration with Storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.nmea2000 import (
    PGN_POSITION_RAPID,
    PGN_SPEED_THROUGH_WATER,
    PGN_WIND_DATA,
    PositionRecord,
    SpeedRecord,
    WindRecord,
)
from helmlog.polar import (
    GRADE_GREEN_BELOW,
    GRADE_RED_BELOW,
    GRADE_SUSPICIOUS_AT,
    GRADE_YELLOW_BELOW,
    _compute_twa,
    _detect_finish_from_track,
    _distance_m,
    _grade_from_pct,
    _twa_bin,
    _tws_bin,
    build_polar_baseline,
    get_polar_baseline_version,
    grade_session_segments,
    lookup_polar,
    race_window,
    session_polar_comparison,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestTwsBin:
    def test_floor(self) -> None:
        assert _tws_bin(8.9) == 8

    def test_integer_boundary(self) -> None:
        assert _tws_bin(15.0) == 15

    def test_fractional(self) -> None:
        assert _tws_bin(15.5) == 15

    def test_zero(self) -> None:
        assert _tws_bin(0.0) == 0

    def test_below_zero_clamps(self) -> None:
        assert _tws_bin(-1.0) == 0


class TestTwaBin:
    def test_port_starboard_symmetry(self) -> None:
        """Port and starboard same angle → same bin."""
        assert _twa_bin(45.0) == _twa_bin(-45.0)

    def test_zero(self) -> None:
        assert _twa_bin(0.0) == 0

    def test_just_below_five(self) -> None:
        assert _twa_bin(4.9) == 0

    def test_exactly_five(self) -> None:
        assert _twa_bin(5.0) == 5

    def test_just_below_180(self) -> None:
        assert _twa_bin(179.9) == 175

    def test_exactly_180(self) -> None:
        # 180° → twa_abs = 180; floor(180/5)*5 = 180 but that's the max
        assert _twa_bin(180.0) == 180

    def test_beyond_180_folds(self) -> None:
        # 200° → twa_abs = 200; >180 → 360-200 = 160 → bin 160
        assert _twa_bin(200.0) == 160


class TestComputeTwa:
    def test_ref0_positive(self) -> None:
        result = _compute_twa(45.0, 0, None)
        assert result == pytest.approx(45.0)

    def test_ref0_negative(self) -> None:
        result = _compute_twa(-45.0, 0, None)
        assert result == pytest.approx(45.0)

    def test_ref0_ignores_heading(self) -> None:
        assert _compute_twa(30.0, 0, 180.0) == pytest.approx(30.0)

    def test_ref4_basic(self) -> None:
        # TWD=90, heading=45 → TWA=45
        result = _compute_twa(90.0, 4, 45.0)
        assert result == pytest.approx(45.0)

    def test_ref4_wraps_correctly(self) -> None:
        # TWD=10, heading=350 → raw=(10-350+360)%360=20 → TWA=20
        result = _compute_twa(10.0, 4, 350.0)
        assert result == pytest.approx(20.0)

    def test_ref4_no_heading_returns_none(self) -> None:
        assert _compute_twa(90.0, 4, None) is None

    def test_apparent_wind_ref_returns_none(self) -> None:
        """reference=2 (apparent) → should return None."""
        assert _compute_twa(45.0, 2, None) is None

    def test_unknown_ref_returns_none(self) -> None:
        assert _compute_twa(45.0, 99, None) is None


# ---------------------------------------------------------------------------
# Integration tests (use storage fixture from conftest.py)
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


async def _make_session(
    storage: Storage,
    race_num: int,
    bsp: float,
    tws: float,
    twa: float,
) -> None:
    """Insert a completed race + 10 matching speed and wind records."""
    start = _BASE_TS + timedelta(hours=race_num)
    end = start + timedelta(seconds=10)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES (?, 'TestEvent', ?, ?, ?, ?)",
        (
            f"TestEvent-R{race_num}",
            race_num,
            start.date().isoformat(),
            start.isoformat(),
            end.isoformat(),
        ),
    )
    await db.commit()

    for i in range(10):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, bsp))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, tws, twa, 0))


@pytest.mark.asyncio
async def test_returns_zero_no_sessions(storage: Storage) -> None:
    count = await build_polar_baseline(storage)
    assert count == 0


@pytest.mark.asyncio
async def test_builds_bins_from_three_sessions(storage: Storage) -> None:
    for i in range(1, 4):
        await _make_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    count = await build_polar_baseline(storage)
    assert count == 1


@pytest.mark.asyncio
async def test_min_sessions_guard(storage: Storage) -> None:
    """2 sessions with min_sessions=3 → no bins written."""
    for i in range(1, 3):
        await _make_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    count = await build_polar_baseline(storage, min_sessions=3)
    assert count == 0


@pytest.mark.asyncio
async def test_mean_bsp_correct(storage: Storage) -> None:
    """BSP=[4, 6, 8] across 3 sessions → mean≈6.0."""
    for i, bsp in enumerate([4.0, 6.0, 8.0], start=1):
        await _make_session(storage, i, bsp=bsp, tws=10.0, twa=45.0)
    await build_polar_baseline(storage)
    row = await storage.get_polar_point(_tws_bin(10.0), _twa_bin(45.0))
    assert row is not None
    assert row["mean_bsp"] == pytest.approx(6.0, rel=1e-3)


@pytest.mark.asyncio
async def test_baseline_excludes_prestart_via_vakaros_gun(storage: Storage) -> None:
    """When a race has a Vakaros race_start event, pre-start samples should
    not contribute to the baseline (#536). Pre-start BSP=1.0, racing BSP=6.0 —
    the baseline mean must reflect only the racing samples.
    """
    db = storage._conn()

    # Three races with identical structure: 10s pre-start (BSP=1.0) then
    # 10s "racing" (BSP=6.0). race_start event is at +10s.
    for race_num in range(1, 4):
        prestart_start = _BASE_TS + timedelta(hours=race_num)
        gun = prestart_start + timedelta(seconds=10)
        end = gun + timedelta(seconds=10)

        # vakaros_sessions row
        await db.execute(
            "INSERT INTO vakaros_sessions (source_hash, source_file, start_utc,"
            " end_utc, ingested_at) VALUES (?, ?, ?, ?, ?)",
            (
                f"hash-{race_num}",
                f"vak-{race_num}.csv",
                prestart_start.isoformat(),
                end.isoformat(),
                prestart_start.isoformat(),
            ),
        )
        vcur = await db.execute("SELECT id FROM vakaros_sessions ORDER BY id DESC LIMIT 1")
        vrow = await vcur.fetchone()
        vid = int(vrow["id"])

        await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc,"
            " end_utc, vakaros_session_id) VALUES (?, 'E', ?, ?, ?, ?, ?)",
            (
                f"R{race_num}",
                race_num,
                prestart_start.date().isoformat(),
                prestart_start.isoformat(),
                end.isoformat(),
                vid,
            ),
        )
        await db.execute(
            "INSERT INTO vakaros_race_events (session_id, ts, event_type,"
            " timer_value_s) VALUES (?, ?, 'race_start', 0)",
            (vid, gun.isoformat()),
        )
        await db.commit()

        # Pre-start samples — low BSP, should be excluded.
        for i in range(10):
            ts = prestart_start + timedelta(seconds=i)
            await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 1.0))
            await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))
        # Race samples — higher BSP, should form the baseline.
        for i in range(10):
            ts = gun + timedelta(seconds=i)
            await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.0))
            await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))

    await build_polar_baseline(storage)
    row = await storage.get_polar_point(_tws_bin(10.0), _twa_bin(45.0))
    assert row is not None
    assert row["mean_bsp"] == pytest.approx(6.0, rel=1e-3)


@pytest.mark.asyncio
async def test_baseline_falls_back_to_start_utc_without_vakaros(
    storage: Storage,
) -> None:
    """Races without a Vakaros session should still contribute the full
    [start_utc, end_utc] window to the baseline."""
    for i in range(1, 4):
        await _make_session(storage, i, bsp=5.5, tws=10.0, twa=45.0)
    await build_polar_baseline(storage)
    row = await storage.get_polar_point(_tws_bin(10.0), _twa_bin(45.0))
    assert row is not None
    assert row["mean_bsp"] == pytest.approx(5.5, rel=1e-3)


@pytest.mark.asyncio
async def test_port_starboard_fold(storage: Storage) -> None:
    """TWA=+45 and TWA=-45 should land in the same bin."""
    for i, twa in enumerate([45.0, -45.0, 45.0], start=1):
        await _make_session(storage, i, bsp=6.0, tws=10.0, twa=twa)
    count = await build_polar_baseline(storage)
    # All three sessions contribute to the same bin
    assert count == 1


@pytest.mark.asyncio
async def test_lookup_returns_none_no_data(storage: Storage) -> None:
    result = await lookup_polar(storage, 10.0, 45.0)
    assert result is None


@pytest.mark.asyncio
async def test_lookup_returns_row_when_sufficient(storage: Storage) -> None:
    for i in range(1, 4):
        await _make_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    await build_polar_baseline(storage)
    result = await lookup_polar(storage, 10.0, 45.0)
    assert result is not None
    assert result["mean_bsp"] == pytest.approx(6.0, rel=1e-3)


@pytest.mark.asyncio
async def test_lookup_guards_min_sessions(storage: Storage) -> None:
    """Row written with session_count=3 but lookup min_sessions=5 → None."""
    for i in range(1, 4):
        await _make_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    await build_polar_baseline(storage)
    result = await lookup_polar(storage, 10.0, 45.0, min_sessions=5)
    assert result is None


# ---------------------------------------------------------------------------
# Session polar comparison tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_polar_nonexistent(storage: Storage) -> None:
    """Non-existent session → None."""
    result = await session_polar_comparison(storage, 9999)
    assert result is None


@pytest.mark.asyncio
async def test_session_polar_unfinished(storage: Storage) -> None:
    """Session without end_utc → None."""
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc)"
        " VALUES ('Open', 'E', 1, '2024-06-01', '2024-06-01T12:00:00')",
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    result = await session_polar_comparison(storage, int(row["id"]))
    assert result is None


@pytest.mark.asyncio
async def test_session_polar_no_instrument_data(storage: Storage) -> None:
    """Completed session but no speed/wind data → empty cells."""
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Empty', 'E', 1, '2024-06-01',"
        " '2024-06-01T12:00:00', '2024-06-01T12:10:00')",
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    result = await session_polar_comparison(storage, int(row["id"]))
    assert result is not None
    assert result.cells == []
    assert result.session_sample_count == 0


@pytest.mark.asyncio
async def test_session_polar_bins_correctly(storage: Storage) -> None:
    """Session with known BSP/TWS/TWA should produce correct bins and session_mean."""
    # Create a session with BSP=6.5, TWS=10, TWA=45
    await _make_session(storage, race_num=10, bsp=6.5, tws=10.0, twa=45.0)
    cur = await storage._conn().execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    sid = int(row["id"])

    result = await session_polar_comparison(storage, sid)
    assert result is not None
    assert result.session_sample_count == 10
    assert len(result.cells) == 1

    cell = result.cells[0]
    assert cell.tws_bin == 10
    assert cell.twa_bin == 45
    assert cell.session_mean_bsp == pytest.approx(6.5, rel=1e-3)
    assert cell.session_sample_count == 10
    # No baseline built yet
    assert cell.baseline_mean_bsp is None
    assert cell.delta is None


@pytest.mark.asyncio
async def test_session_polar_delta_with_baseline(storage: Storage) -> None:
    """With a baseline present, delta = session_mean - baseline_mean."""
    # Build baseline from 3 sessions at BSP=6.0
    for i in range(1, 4):
        await _make_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    await build_polar_baseline(storage)

    # Create a 4th session at BSP=6.5 (faster than baseline)
    await _make_session(storage, race_num=10, bsp=6.5, tws=10.0, twa=45.0)
    cur = await storage._conn().execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    sid = int(row["id"])

    result = await session_polar_comparison(storage, sid)
    assert result is not None
    cell = result.cells[0]
    assert cell.baseline_mean_bsp == pytest.approx(6.0, rel=1e-3)
    assert cell.session_mean_bsp == pytest.approx(6.5, rel=1e-3)
    assert cell.delta == pytest.approx(0.5, rel=1e-2)


@pytest.mark.asyncio
async def test_session_polar_tws_and_twa_bins_sorted(storage: Storage) -> None:
    """tws_bins and twa_bins lists should be sorted."""
    # Session with two different wind conditions
    start = _BASE_TS + timedelta(hours=20)
    end = start + timedelta(seconds=20)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Multi', 'E', 20, ?, ?, ?)",
        (start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()

    for i in range(10):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.0))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))
    for i in range(10, 20):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 5.0))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 8.0, 90.0, 0))

    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    result = await session_polar_comparison(storage, int(row["id"]))
    assert result is not None
    assert result.tws_bins == sorted(result.tws_bins)
    assert result.twa_bins == sorted(result.twa_bins)
    assert 8 in result.tws_bins
    assert 10 in result.tws_bins
    assert 45 in result.twa_bins
    assert 90 in result.twa_bins


# ---------------------------------------------------------------------------
# Session polar splits (#534) — point-of-sail × tack
# ---------------------------------------------------------------------------


async def _make_session_split(
    storage: Storage,
    race_num: int,
    bsp: float,
    tws: float,
    wind_angle: float,  # ref=0 boat-relative, signed
) -> int:
    start = _BASE_TS + timedelta(hours=race_num)
    end = start + timedelta(seconds=10)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES (?, 'SplitEvt', ?, ?, ?, ?)",
        (
            f"Split-R{race_num}",
            race_num,
            start.date().isoformat(),
            start.isoformat(),
            end.isoformat(),
        ),
    )
    await db.commit()
    for i in range(10):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, bsp))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, tws, wind_angle, 0))
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    return int(row["id"])


@pytest.mark.asyncio
async def test_split_starboard_upwind(storage: Storage) -> None:
    sid = await _make_session_split(storage, 30, bsp=6.0, tws=10.0, wind_angle=45.0)
    result = await session_polar_comparison(storage, sid)
    assert result is not None
    assert len(result.cells) == 1
    c = result.cells[0]
    assert c.tack == "starboard"
    assert c.point_of_sail == "upwind"
    assert c.twa_bin == 45


@pytest.mark.asyncio
async def test_split_port_downwind(storage: Storage) -> None:
    sid = await _make_session_split(storage, 31, bsp=5.0, tws=12.0, wind_angle=-140.0)
    result = await session_polar_comparison(storage, sid)
    assert result is not None
    assert len(result.cells) == 1
    c = result.cells[0]
    assert c.tack == "port"
    assert c.point_of_sail == "downwind"
    # abs(140) → bin 140
    assert c.twa_bin == 140


@pytest.mark.asyncio
async def test_split_same_twa_bin_different_tacks(storage: Storage) -> None:
    """Port and starboard at the same angle produce two distinct cells."""
    start = _BASE_TS + timedelta(hours=32)
    end = start + timedelta(seconds=20)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Both', 'E', 32, ?, ?, ?)",
        (start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()
    for i in range(10):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.0))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))
    for i in range(10, 20):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 5.8))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, -45.0, 0))
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    row = await cur.fetchone()
    result = await session_polar_comparison(storage, int(row["id"]))
    assert result is not None
    assert len(result.cells) == 2
    by_tack = {c.tack: c for c in result.cells}
    assert set(by_tack) == {"port", "starboard"}
    assert by_tack["starboard"].session_mean_bsp == pytest.approx(6.0, rel=1e-3)
    assert by_tack["port"].session_mean_bsp == pytest.approx(5.8, rel=1e-3)
    assert all(c.twa_bin == 45 for c in result.cells)
    assert all(c.point_of_sail == "upwind" for c in result.cells)


@pytest.mark.asyncio
async def test_split_shares_symmetric_baseline(storage: Storage) -> None:
    """Baseline is still keyed on abs(TWA); both tacks read the same target."""
    for i in range(1, 4):
        await _make_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    await build_polar_baseline(storage)

    sid = await _make_session_split(storage, 33, bsp=6.5, tws=10.0, wind_angle=-45.0)
    result = await session_polar_comparison(storage, sid)
    assert result is not None
    assert len(result.cells) == 1
    c = result.cells[0]
    assert c.tack == "port"
    assert c.baseline_mean_bsp == pytest.approx(6.0, rel=1e-3)
    assert c.delta == pytest.approx(0.5, rel=1e-2)


# ---------------------------------------------------------------------------
# Per-segment grading (#469)
# ---------------------------------------------------------------------------


class TestGradeFromPct:
    def test_none_is_unknown(self) -> None:
        assert _grade_from_pct(None) == "unknown"

    def test_below_red_threshold(self) -> None:
        assert _grade_from_pct(0.5) == "red"
        assert _grade_from_pct(GRADE_RED_BELOW - 0.0001) == "red"

    def test_red_threshold_inclusive_yellow(self) -> None:
        assert _grade_from_pct(GRADE_RED_BELOW) == "yellow"

    def test_yellow_band(self) -> None:
        assert _grade_from_pct(0.95) == "yellow"

    def test_yellow_threshold_inclusive_green(self) -> None:
        assert _grade_from_pct(GRADE_YELLOW_BELOW) == "green"

    def test_green_band(self) -> None:
        assert _grade_from_pct(1.00) == "green"

    def test_green_extends_through_old_top(self) -> None:
        # 1.05 used to be the green ceiling but now stays green up to 1.20.
        assert _grade_from_pct(GRADE_GREEN_BELOW) == "green"
        assert _grade_from_pct(1.10) == "green"

    def test_suspicious_threshold(self) -> None:
        assert _grade_from_pct(GRADE_SUSPICIOUS_AT) == "suspicious"
        assert _grade_from_pct(2.0) == "suspicious"


async def _seed_session(
    storage: Storage,
    *,
    duration_s: int,
    bsp: float,
    tws: float,
    twa: float,
    with_positions: bool = True,
    with_winds: bool = True,
) -> int:
    """Create a completed race with 1 Hz instrument data and return its id."""
    start = datetime(2024, 7, 1, 12, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=duration_s)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Seg', 'E', 1, ?, ?, ?)",
        (start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    sid = int((await cur.fetchone())["id"])

    for i in range(duration_s):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, bsp))
        if with_winds:
            await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, tws, twa, 0))
        if with_positions:
            await storage.write(
                PositionRecord(PGN_POSITION_RAPID, 5, ts, 37.80 + i * 1e-5, -122.27 + i * 1e-5)
            )
    return sid


async def _build_baseline_at(storage: Storage, bsp: float, tws: float, twa: float) -> None:
    for i in range(1, 4):
        await _make_session(storage, race_num=100 + i, bsp=bsp, tws=tws, twa=twa)
    await build_polar_baseline(storage)


@pytest.mark.asyncio
async def test_grade_unfinished_session_returns_empty(storage: Storage) -> None:
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc)"
        " VALUES ('Open', 'E', 1, '2024-07-01', '2024-07-01T12:00:00')"
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    sid = int((await cur.fetchone())["id"])
    assert await grade_session_segments(storage, sid) == []


@pytest.mark.asyncio
async def test_grade_nonexistent_session(storage: Storage) -> None:
    assert await grade_session_segments(storage, 9999) == []


@pytest.mark.asyncio
async def test_grade_segment_count_matches_duration(storage: Storage) -> None:
    """60s session, 10s segments → 6 segments."""
    sid = await _seed_session(storage, duration_s=60, bsp=6.0, tws=10.0, twa=45.0)
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert len(segs) == 6
    assert segs[0].segment_index == 0
    assert segs[-1].segment_index == 5


@pytest.mark.asyncio
async def test_grade_unknown_when_no_baseline(storage: Storage) -> None:
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0)
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    # No baseline → target_bsp None → grade unknown
    assert all(s.grade == "unknown" for s in segs)
    assert all(s.target_bsp_kts is None for s in segs)


@pytest.mark.asyncio
async def test_grade_green_when_at_target(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0)
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert all(s.grade == "green" for s in segs)
    assert all(s.target_bsp_kts == pytest.approx(6.0, rel=1e-3) for s in segs)
    assert all(s.pct_target == pytest.approx(1.0, rel=1e-3) for s in segs)


@pytest.mark.asyncio
async def test_grade_red_when_far_below_target(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=4.0, tws=10.0, twa=45.0)
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert all(s.grade == "red" for s in segs)


@pytest.mark.asyncio
async def test_grade_suspicious_when_far_above_target(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=5.0, tws=10.0, twa=45.0)
    # 6.5 / 5.0 = 1.30 → suspicious
    sid = await _seed_session(storage, duration_s=30, bsp=6.5, tws=10.0, twa=45.0)
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert all(s.grade == "suspicious" for s in segs)


@pytest.mark.asyncio
async def test_grade_unknown_when_wind_missing(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0, with_winds=False)
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert all(s.grade == "unknown" for s in segs)
    assert all(s.tws_kts is None for s in segs)
    assert all(s.twa_deg is None for s in segs)


@pytest.mark.asyncio
async def test_grade_unknown_when_no_gps(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(
        storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0, with_positions=False
    )
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert all(s.lat is None and s.lon is None for s in segs)
    assert all(s.grade == "unknown" for s in segs)


@pytest.mark.asyncio
async def test_grade_position_interpolated(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0)
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    # First segment midpoint at +5s → lat ≈ 37.80005
    assert segs[0].lat is not None
    assert segs[0].lat == pytest.approx(37.80 + 5e-5, abs=1e-6)


@pytest.mark.asyncio
async def test_grade_cache_hit_does_not_recompute(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0)
    first = await grade_session_segments(storage, sid, segment_seconds=10)
    # Mutate underlying data; cache should hide it.
    db = storage._conn()
    await db.execute("DELETE FROM speeds")
    await db.commit()
    second = await grade_session_segments(storage, sid, segment_seconds=10)
    assert [s.grade for s in second] == [s.grade for s in first]


@pytest.mark.asyncio
async def test_grade_cache_invalidated_on_baseline_rebuild(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0)
    v1 = await get_polar_baseline_version(storage)
    await grade_session_segments(storage, sid, segment_seconds=10)
    # Rebuild → version bump → cache stale → recompute reflects new data
    await build_polar_baseline(storage)
    v2 = await get_polar_baseline_version(storage)
    assert v2 == v1 + 1
    # Clear the speeds and recompute; new run should see the empty data
    db = storage._conn()
    await db.execute("DELETE FROM speeds")
    await db.commit()
    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert all(s.bsp_kts is None for s in segs)
    assert all(s.grade == "unknown" for s in segs)


@pytest.mark.asyncio
async def test_grade_baseline_version_bumped_on_build(storage: Storage) -> None:
    v0 = await get_polar_baseline_version(storage)
    assert v0 == 0
    await build_polar_baseline(storage)
    assert await get_polar_baseline_version(storage) == v0 + 1
    await build_polar_baseline(storage)
    assert await get_polar_baseline_version(storage) == v0 + 2


@pytest.mark.asyncio
async def test_grade_distinct_polar_source_caches_separately(storage: Storage) -> None:
    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0)
    own = await grade_session_segments(storage, sid, polar_source="own", segment_seconds=10)
    ref = await grade_session_segments(storage, sid, polar_source="reference", segment_seconds=10)
    assert len(own) == len(ref) == 3


@pytest.mark.asyncio
async def test_grade_runs_in_worker_thread(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#603: the CPU-bound grading must be dispatched via asyncio.to_thread so
    the event loop stays responsive while a long session is being graded."""
    import asyncio as _asyncio
    from typing import Any

    import helmlog.polar as polar_mod

    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    sid = await _seed_session(storage, duration_s=30, bsp=6.0, tws=10.0, twa=45.0)

    calls: list[str] = []
    real_to_thread = _asyncio.to_thread

    async def spy_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(polar_mod.asyncio, "to_thread", spy_to_thread)

    segs = await grade_session_segments(storage, sid, segment_seconds=10)
    assert len(segs) == 3
    assert "_grade_segments_sync" in calls


@pytest.mark.asyncio
async def test_grade_does_not_block_event_loop(storage: Storage) -> None:
    """#603: other coroutines must be able to make progress while a grading
    call is in flight. Runs a ticker in parallel and asserts it ticked."""
    import asyncio as _asyncio

    await _build_baseline_at(storage, bsp=6.0, tws=10.0, twa=45.0)
    # Larger synthetic session so the sync work is non-trivial. Positions are
    # omitted so the #812 finish heuristic doesn't trim this unrealistic track
    # (the synthetic boat sails monotonically away from the start and never
    # returns); this test only cares that a long session grades fully while the
    # event loop stays responsive.
    sid = await _seed_session(
        storage, duration_s=600, bsp=6.0, tws=10.0, twa=45.0, with_positions=False
    )

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            await _asyncio.sleep(0)
            ticks += 1

    t = _asyncio.create_task(ticker())
    try:
        segs = await grade_session_segments(storage, sid, segment_seconds=10)
    finally:
        stop = True
        await t
    assert len(segs) == 60
    # If the grader held the loop, ticks would be 0-ish. With to_thread the
    # ticker runs freely while the thread computes.
    assert ticks > 10


# ---------------------------------------------------------------------------
# Race window: trim prestart (gun) + post-finish (track heuristic) — #812
# ---------------------------------------------------------------------------

_WIN_LAT = 37.80
_WIN_LON = -122.27


class TestDistanceHelper:
    def test_zero_distance(self) -> None:
        assert _distance_m(_WIN_LAT, _WIN_LON, _WIN_LAT, _WIN_LON) == pytest.approx(0.0)

    def test_known_offset_metres(self) -> None:
        # ~0.01 deg latitude ≈ 1.11 km.
        d = _distance_m(_WIN_LAT, _WIN_LON, _WIN_LAT + 0.01, _WIN_LON)
        assert d == pytest.approx(1113.0, rel=0.02)


class TestDetectFinishHeuristic:
    """Pure backward-scan finish detector (no Storage)."""

    gun = datetime(2024, 7, 1, 12, 0, 0, tzinfo=UTC)

    def _sample(self, offset_s: int, *, near: bool, close_hauled: bool) -> tuple:
        lat = _WIN_LAT + (0.001 if near else 0.03)  # ~140 m vs ~3.3 km from start
        twa = 40.0 if close_hauled else 140.0
        return (self.gun + timedelta(seconds=offset_s), lat, _WIN_LON + 0.001, twa)

    def test_trims_postfinish_tail(self) -> None:
        end = self.gun + timedelta(seconds=300)
        samples = [self._sample(t, near=True, close_hauled=True) for t in range(0, 201, 10)]
        # post-finish: sailed away, downwind
        samples += [self._sample(t, near=False, close_hauled=False) for t in range(210, 301, 10)]
        finish, source = _detect_finish_from_track(self.gun, end, _WIN_LAT, _WIN_LON, samples)
        assert source == "heuristic"
        assert finish == self.gun + timedelta(seconds=200)

    def test_no_trim_when_tail_shorter_than_threshold(self) -> None:
        # Close-hauled near start right up to t=295 → only 5 s of tail (< 30 s).
        end = self.gun + timedelta(seconds=300)
        samples = [self._sample(t, near=True, close_hauled=True) for t in range(0, 296, 5)]
        finish, source = _detect_finish_from_track(self.gun, end, _WIN_LAT, _WIN_LON, samples)
        assert source == "end_utc"
        assert finish == end

    def test_no_trim_when_finish_would_collapse_window(self) -> None:
        # Only the first 60 s are close-hauled-near-start; the rest is far/downwind.
        # Backward scan finds finish at t=60, but 60 s < 120 s min race duration.
        end = self.gun + timedelta(seconds=300)
        samples = [self._sample(t, near=True, close_hauled=True) for t in range(0, 61, 10)]
        samples += [self._sample(t, near=False, close_hauled=False) for t in range(70, 301, 10)]
        finish, source = _detect_finish_from_track(self.gun, end, _WIN_LAT, _WIN_LON, samples)
        assert source == "end_utc"
        assert finish == end

    def test_no_match_falls_back_to_end(self) -> None:
        # Never close-hauled-near-start → cannot detect a finish.
        end = self.gun + timedelta(seconds=300)
        samples = [self._sample(t, near=False, close_hauled=False) for t in range(0, 301, 10)]
        finish, source = _detect_finish_from_track(self.gun, end, _WIN_LAT, _WIN_LON, samples)
        assert source == "end_utc"
        assert finish == end

    def test_empty_samples_falls_back_to_end(self) -> None:
        end = self.gun + timedelta(seconds=300)
        finish, source = _detect_finish_from_track(self.gun, end, _WIN_LAT, _WIN_LON, [])
        assert source == "end_utc"
        assert finish == end


async def _seed_windowed_race(
    storage: Storage,
    *,
    prestart_s: int,
    race_s: int,
    tail_s: int,
    with_vakaros: bool,
    seq: int = 0,
) -> tuple[int, datetime, datetime]:
    """Seed a race with three phases and return (race_id, gun, finish).

    Phases (1 Hz), all with reference-0 (boat) wind so TWA == wind_angle:
      * pre-start [start_utc, gun): BSP 1.0, TWA 40, near start line
      * racing    [gun, finish]:    BSP 6.0, TWA 40 (close-hauled), near start
      * post-race (finish, end]:    BSP 6.5, TWA 140 (running), ~3 km away

    When ``with_vakaros`` the race is Vakaros-matched with a race_start event at
    the gun, so pre-start is trimmable; otherwise the gun falls back to
    start_utc (pre-start stays in-window, which is the point of that variant).
    """
    db = storage._conn()
    # Offset each seeded race by seq days so multi-race tests don't overlap in
    # the (time-ranged, not race_id-filtered) telemetry tables.
    start_utc = datetime(2024, 8, 1, 18, 0, 0, tzinfo=UTC) + timedelta(days=seq)
    gun = start_utc + timedelta(seconds=prestart_s)
    finish = gun + timedelta(seconds=race_s)
    end = finish + timedelta(seconds=tail_s)

    vid: int | None = None
    if with_vakaros:
        await db.execute(
            "INSERT INTO vakaros_sessions (source_hash, source_file, start_utc,"
            " end_utc, ingested_at) VALUES (?, ?, ?, ?, ?)",
            (
                f"h{seq}",
                f"v{seq}.csv",
                start_utc.isoformat(),
                end.isoformat(),
                start_utc.isoformat(),
            ),
        )
        vrow = await (
            await db.execute("SELECT id FROM vakaros_sessions ORDER BY id DESC LIMIT 1")
        ).fetchone()
        vid = int(vrow["id"])
        await db.execute(
            "INSERT INTO vakaros_race_events (session_id, ts, event_type, timer_value_s)"
            " VALUES (?, ?, 'race_start', 0)",
            (vid, gun.isoformat()),
        )

    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, vakaros_session_id)"
        " VALUES (?, 'E', ?, ?, ?, ?, ?)",
        (
            f"W{seq}",
            seq + 1,
            start_utc.date().isoformat(),
            start_utc.isoformat(),
            end.isoformat(),
            vid,
        ),
    )
    await db.commit()
    rid = int(
        (await (await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")).fetchone())["id"]
    )

    async def _phase(t0: datetime, n: int, bsp: float, twa: float, lat: float, lon: float) -> None:
        for i in range(n):
            ts = t0 + timedelta(seconds=i)
            await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, bsp))
            await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, twa, 0))
            await storage.write(PositionRecord(PGN_POSITION_RAPID, 5, ts, lat, lon))

    await _phase(start_utc, prestart_s, 1.0, 40.0, _WIN_LAT + 0.001, _WIN_LON + 0.001)
    await _phase(gun, race_s, 6.0, 40.0, _WIN_LAT + 0.001, _WIN_LON + 0.001)
    await _phase(
        finish + timedelta(seconds=1), tail_s, 6.5, 140.0, _WIN_LAT + 0.03, _WIN_LON + 0.03
    )
    # The last racing sample is at gun + (race_s - 1) — that's the timestamp the
    # backward-scan heuristic should land on as the finish.
    return rid, gun, gun + timedelta(seconds=race_s - 1)


class TestRaceWindow:
    @pytest.mark.asyncio
    async def test_vakaros_gun_and_heuristic_finish(self, storage: Storage) -> None:
        rid, gun, finish = await _seed_windowed_race(
            storage, prestart_s=60, race_s=200, tail_s=60, with_vakaros=True
        )
        row = await (
            await storage._conn().execute(
                "SELECT start_utc, end_utc, vakaros_session_id FROM races WHERE id = ?", (rid,)
            )
        ).fetchone()
        win = await race_window(
            storage,
            start_utc=str(row["start_utc"]),
            end_utc=str(row["end_utc"]),
            vakaros_session_id=row["vakaros_session_id"],
        )
        assert win.gun_source == "vakaros"
        assert win.gun == gun
        assert win.finish_source == "heuristic"
        assert win.finish == finish

    @pytest.mark.asyncio
    async def test_no_vakaros_gun_falls_back_to_start_utc(self, storage: Storage) -> None:
        rid, _gun, finish = await _seed_windowed_race(
            storage, prestart_s=0, race_s=200, tail_s=60, with_vakaros=False
        )
        row = await (
            await storage._conn().execute(
                "SELECT start_utc, end_utc, vakaros_session_id FROM races WHERE id = ?", (rid,)
            )
        ).fetchone()
        win = await race_window(
            storage,
            start_utc=str(row["start_utc"]),
            end_utc=str(row["end_utc"]),
            vakaros_session_id=row["vakaros_session_id"],
        )
        assert win.gun_source == "start_utc"
        assert win.finish_source == "heuristic"
        assert win.finish == finish


class TestSessionPolarWindow:
    @pytest.mark.asyncio
    async def test_comparison_excludes_prestart_and_postfinish(self, storage: Storage) -> None:
        """Only racing-phase samples (BSP 6.0, close-hauled) should bin. The
        pre-start (BSP 1.0) and post-finish (downwind BSP 6.5) phases must be
        trimmed by the gun and the finish heuristic respectively."""
        rid, gun, finish = await _seed_windowed_race(
            storage, prestart_s=60, race_s=200, tail_s=60, with_vakaros=True
        )
        data = await session_polar_comparison(storage, rid)
        assert data is not None
        assert data.window_start == gun
        assert data.window_end == finish
        assert data.gun_source == "vakaros"
        assert data.finish_source == "heuristic"
        # Every cell must be the racing bin: upwind, ~6.0 kt. No 1.0 (prestart)
        # and no downwind (140°) cell.
        assert data.cells
        for c in data.cells:
            assert c.point_of_sail == "upwind"
            assert c.session_mean_bsp == pytest.approx(6.0, rel=1e-3)

    @pytest.mark.asyncio
    async def test_baseline_excludes_postfinish_tail(self, storage: Storage) -> None:
        """Three Vakaros races, each with a downwind post-finish tail. The
        heuristic must trim the tail so the baseline mean reflects racing only
        (6.0), not a blend with the 6.5 kt post-finish running."""
        for seq in range(3):
            await _seed_windowed_race(
                storage, prestart_s=60, race_s=200, tail_s=60, with_vakaros=True, seq=seq
            )
        await build_polar_baseline(storage)
        # Racing was TWA 40 upwind; the post-finish TWA-140 bin must not exist.
        upwind = await storage.get_polar_point(_tws_bin(10.0), _twa_bin(40.0))
        downwind = await storage.get_polar_point(_tws_bin(10.0), _twa_bin(140.0))
        assert upwind is not None
        assert upwind["mean_bsp"] == pytest.approx(6.0, rel=1e-3)
        assert downwind is None


class TestGradeSegmentsWindow:
    @pytest.mark.asyncio
    async def test_grading_uses_racing_window(self, storage: Storage) -> None:
        """Per-segment grading is confined to [gun, finish] (#812): no segment
        starts before the gun or ends after the detected finish, and the count
        reflects the ~200 s racing window, not the full 320 s session."""
        rid, gun, finish = await _seed_windowed_race(
            storage, prestart_s=60, race_s=200, tail_s=60, with_vakaros=True
        )
        segs = await grade_session_segments(storage, rid, segment_seconds=10)
        assert segs
        assert segs[0].t_start >= gun
        assert segs[-1].t_end <= finish + timedelta(seconds=10)
        # Racing window ≈ 200 s → ~20 segments; the full session (320 s) would be
        # ~32. Comfortably under 25 confirms prestart + post-finish were trimmed.
        assert len(segs) < 25
