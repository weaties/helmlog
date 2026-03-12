"""Tests for maneuver_detector.py — tack/gybe/rounding detection from 1 Hz instrument data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.maneuver_detector import (
    _heading_change,
    _peak_change_index,
    detect_all,
    detect_gybes,
    detect_maneuvers,
    detect_mark_roundings,
    detect_tacks,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)


def _make_hdg_series(
    start_hdg: float, end_hdg: float, n_steps: int, offset_s: int = 0
) -> list[tuple[datetime, float]]:
    """Linear heading sweep from start_hdg to end_hdg over n_steps seconds."""
    result = []
    for i in range(n_steps):
        frac = i / max(n_steps - 1, 1)
        diff = ((end_hdg - start_hdg + 180) % 360) - 180
        hdg = (start_hdg + frac * diff) % 360
        result.append((_BASE_TS + timedelta(seconds=offset_s + i), hdg))
    return result


def _make_tack_series(
    pre_hdg: float,
    post_hdg: float,
    pre_steps: int = 30,
    tack_steps: int = 10,
    post_steps: int = 30,
) -> list[tuple[datetime, float]]:
    """Build a series with steady sailing, a fast tack, then recovery."""
    pre = [(_BASE_TS + timedelta(seconds=i), pre_hdg) for i in range(pre_steps)]
    tack = _make_hdg_series(pre_hdg, post_hdg, tack_steps, offset_s=pre_steps)
    post = [
        (_BASE_TS + timedelta(seconds=pre_steps + tack_steps + i), post_hdg)
        for i in range(post_steps)
    ]
    return pre + tack + post


def _const(ts_series: list[tuple[datetime, float]], value: float) -> list[tuple[datetime, float]]:
    """Return a constant value series matching the timestamps in ts_series."""
    return [(ts, value) for ts, _ in ts_series]


def _transition_twa(
    ts_series: list[tuple[datetime, float]],
    pre_val: float,
    post_val: float,
    pre_steps: int = 30,
    turn_steps: int = 10,
) -> list[tuple[datetime, float]]:
    """TWA that transitions from pre_val to post_val during the turn."""
    result = []
    for i, (ts, _) in enumerate(ts_series):
        if i < pre_steps:
            result.append((ts, pre_val))
        elif i < pre_steps + turn_steps:
            frac = (i - pre_steps) / max(turn_steps - 1, 1)
            result.append((ts, pre_val + frac * (post_val - pre_val)))
        else:
            result.append((ts, post_val))
    return result


# ---------------------------------------------------------------------------
# Pure helper: _heading_change
# ---------------------------------------------------------------------------


class TestHeadingChange:
    def test_simple_positive(self) -> None:
        assert _heading_change(0.0, 90.0) == pytest.approx(90.0)

    def test_simple_negative(self) -> None:
        assert _heading_change(90.0, 0.0) == pytest.approx(-90.0)

    def test_wrap_from_350_to_10(self) -> None:
        assert _heading_change(350.0, 10.0) == pytest.approx(20.0)

    def test_wrap_from_10_to_350(self) -> None:
        assert _heading_change(10.0, 350.0) == pytest.approx(-20.0)

    def test_no_change(self) -> None:
        assert _heading_change(45.0, 45.0) == pytest.approx(0.0)

    def test_180_degrees(self) -> None:
        result = abs(_heading_change(0.0, 180.0))
        assert result == pytest.approx(180.0)


class TestPeakChangeIndex:
    def test_peak_in_middle(self) -> None:
        hdg = [40.0, 40.0, 40.0, 120.0, 120.0, 120.0]
        assert _peak_change_index(hdg) == 3

    def test_single_element(self) -> None:
        assert _peak_change_index([90.0]) == 0

    def test_empty(self) -> None:
        assert _peak_change_index([]) == 0


# ---------------------------------------------------------------------------
# detect_tacks — pure function
# ---------------------------------------------------------------------------


class TestDetectTacks:
    def test_clear_tack_detected(self) -> None:
        """80° heading change upwind within 15 s → one tack."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.5)
        twa = _const(hdg, 45.0)  # upwind (TWA < 90°)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        assert maneuvers[0].type == "tack"

    def test_gybe_not_classified_as_tack(self) -> None:
        """80° heading change downwind → not a tack."""
        hdg = _make_tack_series(180.0, 260.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 7.0)
        twa = _const(hdg, 150.0)  # downwind (TWA > 90°)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_no_maneuver_steady_state(self) -> None:
        """No heading change → empty result."""
        hdg = [((_BASE_TS + timedelta(seconds=i)), 45.0) for i in range(60)]
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_insufficient_hdg_change_no_tack(self) -> None:
        """30° heading change upwind → below threshold, no tack."""
        hdg = _make_tack_series(45.0, 75.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 50.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_hdg_wraparound_handled(self) -> None:
        """Tack crossing 0° (350° → 70°, 80° across north) → detected."""
        hdg = _make_tack_series(350.0, 70.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1

    def test_noisy_hdg_no_false_positive(self) -> None:
        """±2° jitter around steady heading → no maneuvers detected."""
        import math

        hdg = [
            (_BASE_TS + timedelta(seconds=i), 90.0 + 2.0 * math.sin(i * 0.5)) for i in range(120)
        ]
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_multiple_tacks_all_detected(self) -> None:
        """Two fast tacks separated by 30 s of steady sailing → both detected."""
        t1 = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=10)
        gap_start = 30 + 10 + 10
        gap = [(_BASE_TS + timedelta(seconds=gap_start + i), 320.0) for i in range(20)]
        tack2_start = gap_start + 20
        tack2_pts = _make_hdg_series(320.0, 40.0, 10, offset_s=tack2_start)
        post2 = [(_BASE_TS + timedelta(seconds=tack2_start + 10 + i), 40.0) for i in range(30)]
        hdg = t1 + gap + tack2_pts + post2
        bsp = _const(hdg, 6.5)
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 2

    def test_bsp_loss_calculated(self) -> None:
        """BSP dips during tack → loss_kts reflects the drop."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp_vals = [(ts, 4.0 if 30 <= i <= 39 else 6.5) for i, (ts, _) in enumerate(hdg)]
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp_vals, twa)
        assert len(maneuvers) == 1
        m = maneuvers[0]
        assert m.loss_kts is not None
        assert m.loss_kts > 0.0

    def test_tws_twa_bins_assigned(self) -> None:
        """Bins are correctly assigned from wind data."""
        from helmlog.polar import _twa_bin

        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        m = maneuvers[0]
        assert m.twa_bin == _twa_bin(45.0)

    def test_port_tack_twa_above_180_classified_as_tack(self) -> None:
        """TWA reported as 300° (port tack, = 60° folded) must be classified as tack."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.5)
        twa = _const(hdg, 300.0)  # 300° = 60° folded → upwind
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        assert maneuvers[0].type == "tack"

    def test_port_tack_twa_above_180_not_gybe(self) -> None:
        """TWA=300° (60° folded, upwind) must NOT be classified as a gybe."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.5)
        twa = _const(hdg, 300.0)
        maneuvers = detect_gybes(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_maneuver_timestamp_within_session(self) -> None:
        """Maneuver timestamp falls within the HDG series window."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        m = maneuvers[0]
        assert hdg[0][0] <= m.ts <= hdg[-1][0]

    def test_maneuver_timestamp_at_peak_heading_change(self) -> None:
        """Maneuver ts should land at the peak heading change, not the window start."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 45.0)
        maneuvers = detect_tacks(hdg, bsp, twa)
        assert len(maneuvers) == 1
        m = maneuvers[0]
        tack_start = hdg[30][0]
        tack_end = hdg[39][0]
        assert tack_start <= m.ts <= tack_end, (
            f"Maneuver ts {m.ts} should be within tack interval [{tack_start}, {tack_end}]"
        )


# ---------------------------------------------------------------------------
# detect_gybes — pure function
# ---------------------------------------------------------------------------


class TestDetectGybes:
    def test_clear_gybe_detected(self) -> None:
        """70° heading change downwind within 15 s → one gybe."""
        hdg = _make_tack_series(180.0, 250.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 8.0)
        twa = _const(hdg, 150.0)  # downwind
        maneuvers = detect_gybes(hdg, bsp, twa)
        assert len(maneuvers) == 1
        assert maneuvers[0].type == "gybe"

    def test_tack_not_classified_as_gybe(self) -> None:
        """80° heading change upwind → not a gybe."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.0)
        twa = _const(hdg, 45.0)  # upwind
        maneuvers = detect_gybes(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_no_maneuver_steady_downwind(self) -> None:
        """Steady downwind → no gybes."""
        hdg = [(_BASE_TS + timedelta(seconds=i), 180.0) for i in range(60)]
        bsp = _const(hdg, 8.0)
        twa = _const(hdg, 150.0)
        maneuvers = detect_gybes(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_gybe_type_is_gybe(self) -> None:
        hdg = _make_tack_series(170.0, 240.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 8.0)
        twa = _const(hdg, 160.0)
        maneuvers = detect_gybes(hdg, bsp, twa)
        assert all(m.type == "gybe" for m in maneuvers)


# ---------------------------------------------------------------------------
# detect_mark_roundings — pure function
# ---------------------------------------------------------------------------


class TestDetectMarkRoundings:
    def test_windward_mark_rounding_detected(self) -> None:
        """Heading change with TWA crossing from upwind to downwind → rounding."""
        hdg = _make_tack_series(0.0, 90.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.5)
        twa = _transition_twa(hdg, 45.0, 135.0)
        maneuvers = detect_mark_roundings(hdg, bsp, twa)
        assert len(maneuvers) >= 1
        assert all(m.type == "rounding" for m in maneuvers)

    def test_leeward_mark_rounding_detected(self) -> None:
        """Heading change with TWA crossing from downwind to upwind → rounding."""
        hdg = _make_tack_series(180.0, 90.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 7.0)
        twa = _transition_twa(hdg, 150.0, 50.0)
        maneuvers = detect_mark_roundings(hdg, bsp, twa)
        assert len(maneuvers) >= 1
        assert all(m.type == "rounding" for m in maneuvers)

    def test_tack_not_classified_as_rounding(self) -> None:
        """Heading change with TWA staying upwind → not a rounding."""
        hdg = _make_tack_series(40.0, 320.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.5)
        twa = _const(hdg, 45.0)
        maneuvers = detect_mark_roundings(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_gybe_not_classified_as_rounding(self) -> None:
        """Heading change with TWA staying downwind → not a rounding."""
        hdg = _make_tack_series(180.0, 250.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 8.0)
        twa = _const(hdg, 150.0)
        maneuvers = detect_mark_roundings(hdg, bsp, twa)
        assert len(maneuvers) == 0

    def test_windward_mark_not_classified_as_tack(self) -> None:
        """A mark rounding (TWA crosses 90°) must NOT appear as a tack."""
        hdg = _make_tack_series(0.0, 90.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.5)
        twa = _transition_twa(hdg, 45.0, 135.0)
        tacks = detect_tacks(hdg, bsp, twa)
        assert len(tacks) == 0

    def test_windward_mark_not_classified_as_gybe(self) -> None:
        """A mark rounding (TWA crosses 90°) must NOT appear as a gybe."""
        hdg = _make_tack_series(0.0, 90.0, pre_steps=30, tack_steps=10, post_steps=30)
        bsp = _const(hdg, 6.5)
        twa = _transition_twa(hdg, 45.0, 135.0)
        gybes = detect_gybes(hdg, bsp, twa)
        assert len(gybes) == 0


# ---------------------------------------------------------------------------
# detect_all — unified detection
# ---------------------------------------------------------------------------


class TestDetectAll:
    def test_mixed_series_finds_all_types(self) -> None:
        """A series with a tack, rounding, and gybe finds all three."""
        hdg, bsp, twa = _build_tack_rounding_gybe_sequence()
        maneuvers = detect_all(hdg, bsp, twa)
        types = [m.type for m in maneuvers]
        assert "tack" in types, f"Expected tack in {types}"
        assert "rounding" in types, f"Expected rounding in {types}"
        assert "gybe" in types, f"Expected gybe in {types}"

    def test_no_tack_gybe_transition_without_rounding(self) -> None:
        """A tack→gybe transition must always have a rounding between them."""
        hdg, bsp, twa = _build_two_lap_race()
        maneuvers = detect_all(hdg, bsp, twa)
        assert len(maneuvers) >= 3, f"Expected ≥3 maneuvers, got {len(maneuvers)}"

        for i in range(1, len(maneuvers)):
            prev_type = maneuvers[i - 1].type
            curr_type = maneuvers[i].type
            if (
                prev_type in ("tack", "gybe")
                and curr_type in ("tack", "gybe")
                and prev_type != curr_type
            ):
                pytest.fail(
                    f"Maneuver #{i - 1} ({prev_type} at {maneuvers[i - 1].ts}) "
                    f"→ #{i} ({curr_type} at {maneuvers[i].ts}) "
                    f"without an intervening rounding"
                )


def _build_tack_rounding_gybe_sequence() -> tuple[
    list[tuple[datetime, float]],
    list[tuple[datetime, float]],
    list[tuple[datetime, float]],
]:
    """Build: steady upwind → tack → steady → rounding → steady downwind → gybe → steady."""
    hdg: list[tuple[datetime, float]] = []
    twa: list[tuple[datetime, float]] = []
    t = 0

    def _steady(heading: float, twa_val: float, secs: int) -> None:
        nonlocal t
        for _ in range(secs):
            ts = _BASE_TS + timedelta(seconds=t)
            hdg.append((ts, heading))
            twa.append((ts, twa_val))
            t += 1

    def _turn(h1: float, h2: float, twa1: float, twa2: float, secs: int) -> None:
        nonlocal t
        for i in range(secs):
            frac = i / max(secs - 1, 1)
            diff_h = ((h2 - h1 + 180) % 360) - 180
            diff_t = twa2 - twa1
            ts = _BASE_TS + timedelta(seconds=t)
            hdg.append((ts, (h1 + frac * diff_h) % 360))
            twa.append((ts, twa1 + frac * diff_t))
            t += 1

    # Upwind leg
    _steady(50.0, 45.0, 30)
    _turn(50.0, 310.0, 45.0, 45.0, 10)  # tack (stays upwind)
    _steady(310.0, 45.0, 30)

    # Windward mark rounding (upwind → downwind)
    _turn(310.0, 220.0, 45.0, 150.0, 10)
    _steady(220.0, 150.0, 30)

    # Downwind gybe
    _turn(220.0, 140.0, 150.0, 150.0, 10)  # gybe (stays downwind)
    _steady(140.0, 150.0, 30)

    bsp = _const(hdg, 6.5)
    return hdg, bsp, twa


def _build_two_lap_race() -> tuple[
    list[tuple[datetime, float]],
    list[tuple[datetime, float]],
    list[tuple[datetime, float]],
]:
    """Synthesise a 2-lap windward/leeward race.

    Pattern: tack leg → windward rounding → gybe leg → leeward rounding → repeat.
    Returns (hdg, bsp, twa) aligned 1 Hz series.
    """
    hdg: list[tuple[datetime, float]] = []
    twa: list[tuple[datetime, float]] = []
    t = 0

    def _steady(heading: float, twa_val: float, secs: int) -> None:
        nonlocal t
        for _ in range(secs):
            ts = _BASE_TS + timedelta(seconds=t)
            hdg.append((ts, heading))
            twa.append((ts, twa_val))
            t += 1

    def _turn(h1: float, h2: float, twa1: float, twa2: float, secs: int) -> None:
        nonlocal t
        for i in range(secs):
            frac = i / max(secs - 1, 1)
            diff_h = ((h2 - h1 + 180) % 360) - 180
            diff_t = twa2 - twa1
            ts = _BASE_TS + timedelta(seconds=t)
            hdg.append((ts, (h1 + frac * diff_h) % 360))
            twa.append((ts, twa1 + frac * diff_t))
            t += 1

    for _ in range(2):  # 2 laps
        # Upwind leg: tack at TWA 45°
        _steady(50.0, 45.0, 30)
        _turn(50.0, 310.0, 45.0, 45.0, 10)  # tack (80° heading, stays upwind)
        _steady(310.0, 45.0, 30)

        # Windward mark rounding: TWA crosses 90° (upwind→downwind)
        _turn(310.0, 220.0, 45.0, 150.0, 10)  # rounding
        _steady(220.0, 150.0, 10)

        # Downwind leg: gybe at TWA 150°
        _steady(220.0, 150.0, 30)
        _turn(220.0, 140.0, 150.0, 150.0, 10)  # gybe (80° heading, stays downwind)
        _steady(140.0, 150.0, 30)

        # Leeward mark rounding: TWA crosses 90° (downwind→upwind)
        _turn(140.0, 50.0, 150.0, 45.0, 10)  # rounding
        _steady(50.0, 45.0, 10)

    bsp = _const(hdg, 6.5)
    return hdg, bsp, twa


# ---------------------------------------------------------------------------
# detect_maneuvers — integration with Storage
# ---------------------------------------------------------------------------


async def _seed_tack_session(storage: Storage) -> int:
    """Seed a session with a clear tack in the instrument data. Returns session_id."""
    from helmlog.nmea2000 import (
        PGN_SPEED_THROUGH_WATER,
        PGN_VESSEL_HEADING,
        PGN_WIND_DATA,
        HeadingRecord,
        SpeedRecord,
        WindRecord,
    )

    base = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    session_end = base + timedelta(seconds=120)

    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Test-R1', 'TestEvent', 1, '2024-06-15', ?, ?)",
        (base.isoformat(), session_end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    session_id = int(row[0])

    # Pre-tack steady sailing (t=0..29): HDG=40, BSP=6.5, TWA=45 (upwind)
    for i in range(30):
        ts = base + timedelta(seconds=i)
        await storage.write(HeadingRecord(PGN_VESSEL_HEADING, 5, ts, 40.0, None, None))
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.5))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 12.0, 45.0, 0))

    # Tack (t=30..39): HDG sweeps 40→320 over 10 seconds, BSP dips
    for i in range(10):
        ts = base + timedelta(seconds=30 + i)
        frac = i / 9
        diff = ((320.0 - 40.0 + 180) % 360) - 180  # = -80°
        tack_hdg = (40.0 + frac * diff) % 360
        bsp = 4.5 if 2 <= i <= 8 else 6.5  # BSP dips during tack
        await storage.write(HeadingRecord(PGN_VESSEL_HEADING, 5, ts, tack_hdg, None, None))
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, bsp))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 12.0, 45.0, 0))

    # Post-tack recovery (t=40..119): HDG=320, BSP=6.5, TWA=45
    for i in range(80):
        ts = base + timedelta(seconds=40 + i)
        await storage.write(HeadingRecord(PGN_VESSEL_HEADING, 5, ts, 320.0, None, None))
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.5))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 12.0, 45.0, 0))

    return session_id


@pytest.mark.asyncio
async def test_detect_maneuvers_finds_tack(storage: Storage) -> None:
    """Seeded session with a tack → detect_maneuvers returns ≥1 tack."""
    session_id = await _seed_tack_session(storage)
    maneuvers = await detect_maneuvers(storage, session_id)
    tacks = [m for m in maneuvers if m.type == "tack"]
    assert len(tacks) >= 1


@pytest.mark.asyncio
async def test_detect_maneuvers_writes_to_db(storage: Storage) -> None:
    """detect_maneuvers persists results to the maneuvers table."""
    session_id = await _seed_tack_session(storage)
    maneuvers = await detect_maneuvers(storage, session_id)
    assert len(maneuvers) > 0

    stored = await storage.get_session_maneuvers(session_id)
    assert len(stored) == len(maneuvers)
    assert stored[0]["type"] in ("tack", "gybe", "rounding", "maneuver")


@pytest.mark.asyncio
async def test_detect_maneuvers_idempotent(storage: Storage) -> None:
    """Running detection twice replaces previous results (no duplicates)."""
    session_id = await _seed_tack_session(storage)
    first = await detect_maneuvers(storage, session_id)
    second = await detect_maneuvers(storage, session_id)

    stored = await storage.get_session_maneuvers(session_id)
    assert len(stored) == len(second)
    assert len(stored) == len(first)


@pytest.mark.asyncio
async def test_detect_maneuvers_empty_session(storage: Storage) -> None:
    """Session with no instrument data → empty result, no error."""
    base = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Empty-R1', 'TestEvent', 1, '2024-06-15', ?, ?)",
        (base.isoformat(), (base + timedelta(hours=1)).isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    session_id = int(row[0])

    maneuvers = await detect_maneuvers(storage, session_id)
    assert maneuvers == []


@pytest.mark.asyncio
async def test_get_session_maneuvers_returns_list(storage: Storage) -> None:
    """get_session_maneuvers returns list (empty when none stored)."""
    base = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('NoManeuvers-R1', 'TestEvent', 2, '2024-06-15', ?, ?)",
        (base.isoformat(), (base + timedelta(hours=1)).isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    session_id = int(row[0])

    result = await storage.get_session_maneuvers(session_id)
    assert result == []


@pytest.mark.asyncio
async def test_detect_maneuvers_twd_reference4(storage: Storage) -> None:
    """Wind stored as TWD (reference=4, B&G fallback) → tack still detected."""
    from helmlog.nmea2000 import (
        PGN_SPEED_THROUGH_WATER,
        PGN_VESSEL_HEADING,
        PGN_WIND_DATA,
        HeadingRecord,
        SpeedRecord,
        WindRecord,
    )

    base = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    session_end = base + timedelta(seconds=120)

    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('TWD-R1', 'TestEvent', 3, '2024-06-15', ?, ?)",
        (base.isoformat(), session_end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    session_id = int(row[0])

    TWD = 355.0

    for i in range(30):
        ts = base + timedelta(seconds=i)
        await storage.write(HeadingRecord(PGN_VESSEL_HEADING, 5, ts, 40.0, None, None))
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.5))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 12.0, TWD, 4))

    for i in range(10):
        ts = base + timedelta(seconds=30 + i)
        frac = i / 9
        diff = ((320.0 - 40.0 + 180) % 360) - 180
        tack_hdg = (40.0 + frac * diff) % 360
        bsp = 4.5 if 2 <= i <= 8 else 6.5
        await storage.write(HeadingRecord(PGN_VESSEL_HEADING, 5, ts, tack_hdg, None, None))
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, bsp))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 12.0, TWD, 4))

    for i in range(80):
        ts = base + timedelta(seconds=40 + i)
        await storage.write(HeadingRecord(PGN_VESSEL_HEADING, 5, ts, 320.0, None, None))
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.5))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 12.0, TWD, 4))

    maneuvers = await detect_maneuvers(storage, session_id)
    tacks = [m for m in maneuvers if m.type == "tack"]
    assert len(tacks) >= 1, "Expected at least one tack when wind is stored as TWD (reference=4)"


# ---------------------------------------------------------------------------
# detect_maneuvers — COG/SOG fallback (GPS-only sessions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_maneuvers_falls_back_to_cogsog(storage: Storage) -> None:
    """When headings/speeds tables are empty, falls back to COG/SOG from cogsog."""
    from helmlog.nmea2000 import PGN_COG_SOG_RAPID, COGSOGRecord

    base = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    session_end = base + timedelta(seconds=120)

    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('COGOnly-R1', 'TestEvent', 4, '2024-06-15', ?, ?)",
        (base.isoformat(), session_end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    session_id = int(row[0])

    for i in range(30):
        ts = base + timedelta(seconds=i)
        await storage.write(COGSOGRecord(PGN_COG_SOG_RAPID, 5, ts, 40.0, 6.5))

    for i in range(10):
        ts = base + timedelta(seconds=30 + i)
        frac = i / 9
        diff = ((320.0 - 40.0 + 180) % 360) - 180
        cog = (40.0 + frac * diff) % 360
        await storage.write(COGSOGRecord(PGN_COG_SOG_RAPID, 5, ts, cog, 5.0))

    for i in range(80):
        ts = base + timedelta(seconds=40 + i)
        await storage.write(COGSOGRecord(PGN_COG_SOG_RAPID, 5, ts, 320.0, 6.5))

    maneuvers = await detect_maneuvers(storage, session_id)
    assert len(maneuvers) >= 1, "Should detect course change using COG/SOG fallback"
    assert all(m.type == "maneuver" for m in maneuvers), (
        "Without TWA, maneuvers should be typed 'maneuver' not 'tack'/'gybe'"
    )
