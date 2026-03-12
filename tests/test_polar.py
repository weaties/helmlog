"""Tests for src/logger/polar.py — pure helpers and integration with Storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.nmea2000 import (
    PGN_SPEED_THROUGH_WATER,
    PGN_WIND_DATA,
    SpeedRecord,
    WindRecord,
)
from helmlog.polar import (
    _compute_twa,
    _twa_bin,
    _tws_bin,
    build_polar_baseline,
    lookup_polar,
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
