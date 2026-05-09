"""Tests for race_start.py — pure FSM, flag resolver, and line geometry.

Each test maps to a row in the spec decision tables / a transition in the
FSM diagram (see #644 spec comment).
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from helmlog.race_start import (
    GENERAL_RECALL_GRACE_S,
    IDLE,
    SOG_FLOOR_KN,
    ClassEntry,
    FlagState,
    LineMetrics,
    SequenceState,
    StartLine,
    abandon,
    arm,
    class_gap_seconds,
    flag_state,
    general_recall,
    line_metrics,
    nudge,
    postpone,
    prep_window_seconds,
    reset,
    restart_after_recall,
    resume_from_postponement,
    sync_to_gun,
    tick,
    warning_seconds,
)

# Reference t0 — 13:45:00 UTC
T0 = datetime(2026, 5, 1, 13, 45, 0, tzinfo=UTC)


def at(seconds: int) -> datetime:
    """Wall clock = t0 + seconds (negative = before t0)."""
    return T0 + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_warning_seconds_5410() -> None:
    assert warning_seconds("5-4-1-0") == 300


def test_warning_seconds_3210() -> None:
    assert warning_seconds("3-2-1-0") == 180


def test_warning_seconds_unknown_raises() -> None:
    with pytest.raises(ValueError):
        warning_seconds("10-6-5-4-1-0")  # type: ignore[arg-type]


def test_class_gap_matches_warning() -> None:
    assert class_gap_seconds("5-4-1-0") == 300
    assert class_gap_seconds("3-2-1-0") == 180


def test_prep_window_5410() -> None:
    assert prep_window_seconds("5-4-1-0") == (240, 60)


def test_prep_window_3210() -> None:
    assert prep_window_seconds("3-2-1-0") == (120, 60)


# ---------------------------------------------------------------------------
# ClassEntry validation
# ---------------------------------------------------------------------------


def test_class_entry_valid() -> None:
    c = ClassEntry(name="J/70", order=0, is_ours=True, prep_flag="P")
    assert c.is_ours
    assert c.prep_flag == "P"


def test_class_entry_blank_name_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ClassEntry(name="   ", order=0, is_ours=True)


def test_class_entry_negative_order_raises() -> None:
    with pytest.raises(ValueError, match="order"):
        ClassEntry(name="J/70", order=-1)


def test_class_entry_invalid_prep_flag_raises() -> None:
    with pytest.raises(ValueError, match="prep flag"):
        ClassEntry(name="J/70", order=0, prep_flag="Q")


# ---------------------------------------------------------------------------
# StartLine
# ---------------------------------------------------------------------------


def test_start_line_empty_is_incomplete() -> None:
    assert not StartLine().is_complete


def test_start_line_one_end_only_is_incomplete() -> None:
    line = StartLine(boat_end_lat=47.65, boat_end_lon=-122.40, boat_end_captured_at=T0)
    assert not line.is_complete


def test_start_line_both_ends_is_complete() -> None:
    line = StartLine(
        boat_end_lat=47.6500,
        boat_end_lon=-122.4000,
        boat_end_captured_at=T0,
        pin_end_lat=47.6510,
        pin_end_lon=-122.4000,
        pin_end_captured_at=T0,
    )
    assert line.is_complete


# ---------------------------------------------------------------------------
# FSM — arm
# ---------------------------------------------------------------------------


def test_arm_creates_armed_state() -> None:
    s = arm("5-4-1-0", T0)
    assert s.phase == "armed"
    assert s.kind == "5-4-1-0"
    assert s.t0_utc == T0


def test_arm_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        arm("10-6-5-4-1-0", T0)  # type: ignore[arg-type]


def test_arm_naive_datetime_raises() -> None:
    naive = datetime(2026, 5, 1, 13, 45, 0)  # noqa: DTZ001 — testing rejection
    with pytest.raises(ValueError, match="timezone-aware"):
        arm("5-4-1-0", naive)


def test_arm_with_classes_validates_orders_unique() -> None:
    bad = (
        ClassEntry(name="A", order=0, is_ours=True),
        ClassEntry(name="B", order=0),
    )
    with pytest.raises(ValueError, match="unique"):
        arm("5-4-1-0", T0, bad)


def test_arm_with_classes_validates_exactly_one_ours() -> None:
    none_ours = (
        ClassEntry(name="A", order=0),
        ClassEntry(name="B", order=1),
    )
    with pytest.raises(ValueError, match="exactly one"):
        arm("5-4-1-0", T0, none_ours)

    two_ours = (
        ClassEntry(name="A", order=0, is_ours=True),
        ClassEntry(name="B", order=1, is_ours=True),
    )
    with pytest.raises(ValueError, match="exactly one"):
        arm("5-4-1-0", T0, two_ours)


# ---------------------------------------------------------------------------
# FSM — tick
# ---------------------------------------------------------------------------


def test_tick_armed_before_t0_goes_to_counting_down() -> None:
    s = tick(arm("5-4-1-0", T0), at(-200))
    assert s.phase == "counting_down"


def test_tick_armed_at_or_after_t0_goes_to_started() -> None:
    s = tick(arm("5-4-1-0", T0), at(0))
    assert s.phase == "started"
    assert s.started_at_utc == T0


def test_tick_counting_down_to_started() -> None:
    s = tick(arm("5-4-1-0", T0), at(-100))  # counting_down
    s2 = tick(s, at(1))
    assert s2.phase == "started"


def test_tick_idle_unchanged() -> None:
    assert tick(IDLE, at(0)) is IDLE


def test_tick_postponed_unchanged() -> None:
    s = postpone(arm("5-4-1-0", T0))
    assert tick(s, at(0)) == s


# ---------------------------------------------------------------------------
# FSM — sync_to_gun
# ---------------------------------------------------------------------------


def test_sync_to_gun_re_anchors_t0() -> None:
    """Sync at warning signal: now + warning_s = new_t0."""
    s = arm("5-4-1-0", T0)
    # User taps sync 2 seconds late at the warning signal (which should
    # have been at T0 - 300s). New t0 should be wall-now + 300s.
    s2 = sync_to_gun(s, at(-298), expected_signal_offset_s=300)
    assert s2.phase == "counting_down"
    assert s2.t0_utc == at(-298) + timedelta(seconds=300)
    assert s2.t0_utc == at(2)
    assert s2.sync_offset_s == pytest.approx(2.0)
    assert s2.last_sync_at_utc == at(-298)


def test_sync_to_gun_at_start_signal() -> None:
    s = arm("5-4-1-0", T0)
    s2 = sync_to_gun(s, at(1), expected_signal_offset_s=0)
    assert s2.t0_utc == at(1)


def test_sync_to_gun_from_idle_raises() -> None:
    with pytest.raises(ValueError, match="cannot sync"):
        sync_to_gun(IDLE, at(0), 300)


def test_sync_to_gun_from_started_raises() -> None:
    s = tick(arm("5-4-1-0", T0), at(5))
    with pytest.raises(ValueError, match="cannot sync"):
        sync_to_gun(s, at(5), 0)


# ---------------------------------------------------------------------------
# FSM — nudge
# ---------------------------------------------------------------------------


def test_nudge_plus_60() -> None:
    s = arm("5-4-1-0", T0)
    s2 = nudge(s, 60)
    assert s2.t0_utc == T0 + timedelta(seconds=60)


def test_nudge_minus_60() -> None:
    s = arm("5-4-1-0", T0)
    s2 = nudge(s, -60)
    assert s2.t0_utc == T0 - timedelta(seconds=60)


def test_nudge_from_idle_raises() -> None:
    with pytest.raises(ValueError, match="cannot nudge"):
        nudge(IDLE, 60)


def test_nudge_from_started_updates_t0_and_started_at() -> None:
    """Nudging after the gun re-anchors the start moment retroactively."""
    s = tick(arm("5-4-1-0", T0), at(5))
    assert s.phase == "started"
    s2 = nudge(s, 3)
    assert s2.phase == "started"
    assert s2.t0_utc == T0 + timedelta(seconds=3)
    assert s2.started_at_utc == T0 + timedelta(seconds=3)


def test_nudge_from_postponed_raises() -> None:
    s = postpone(arm("5-4-1-0", T0))
    with pytest.raises(ValueError, match="cannot nudge"):
        nudge(s, 60)


# ---------------------------------------------------------------------------
# FSM — postpone / resume
# ---------------------------------------------------------------------------


def test_postpone_from_armed() -> None:
    s = postpone(arm("5-4-1-0", T0))
    assert s.phase == "postponed"


def test_postpone_from_counting_down() -> None:
    s = tick(arm("5-4-1-0", T0), at(-200))
    assert postpone(s).phase == "postponed"


def test_postpone_from_idle_raises() -> None:
    with pytest.raises(ValueError):
        postpone(IDLE)


def test_resume_from_postponement_sets_new_t0() -> None:
    s = postpone(arm("5-4-1-0", T0))
    new_t0 = T0 + timedelta(minutes=10)
    s2 = resume_from_postponement(s, new_t0)
    assert s2.phase == "counting_down"
    assert s2.t0_utc == new_t0


def test_resume_from_non_postponed_raises() -> None:
    with pytest.raises(ValueError):
        resume_from_postponement(arm("5-4-1-0", T0), T0 + timedelta(minutes=5))


# ---------------------------------------------------------------------------
# FSM — general recall
# ---------------------------------------------------------------------------


def test_general_recall_during_countdown() -> None:
    s = tick(arm("5-4-1-0", T0), at(-30))
    assert general_recall(s, at(-30)).phase == "general_recall"


def test_general_recall_within_grace_window_after_start() -> None:
    s = tick(arm("5-4-1-0", T0), at(0))
    s2 = general_recall(s, at(30))
    assert s2.phase == "general_recall"
    assert s2.started_at_utc is None


def test_general_recall_outside_grace_window_raises() -> None:
    s = tick(arm("5-4-1-0", T0), at(0))
    with pytest.raises(ValueError, match="window expired"):
        general_recall(s, at(int(GENERAL_RECALL_GRACE_S) + 1))


def test_general_recall_from_idle_raises() -> None:
    with pytest.raises(ValueError):
        general_recall(IDLE, T0)


def test_restart_after_recall_re_arms() -> None:
    s = tick(arm("5-4-1-0", T0), at(-30))
    s = general_recall(s, at(-30))
    new_t0 = at(60)
    s2 = restart_after_recall(s, new_t0)
    assert s2.phase == "armed"
    assert s2.t0_utc == new_t0


def test_restart_from_non_recall_raises() -> None:
    with pytest.raises(ValueError):
        restart_after_recall(arm("5-4-1-0", T0), T0)


# ---------------------------------------------------------------------------
# FSM — abandon / reset
# ---------------------------------------------------------------------------


def test_abandon_from_counting_down() -> None:
    s = tick(arm("5-4-1-0", T0), at(-30))
    assert abandon(s).phase == "abandoned"


def test_abandon_from_idle_raises() -> None:
    with pytest.raises(ValueError, match="nothing to abandon"):
        abandon(IDLE)


def test_reset_returns_idle() -> None:
    assert reset() == IDLE


# ---------------------------------------------------------------------------
# Flag resolver — single-class (no stack)
# ---------------------------------------------------------------------------


def _state_at(t_offset_s: int, kind: str = "5-4-1-0") -> SequenceState:
    """Helper: return a SequenceState that yields *t_offset_s* at wall=T0."""
    # We pass T0 as t0; tests call flag_state with now = T0 + t_offset.
    return tick(arm(kind, T0), T0 + timedelta(seconds=t_offset_s))


def test_flag_state_5410_warning_to_prep_no_stack() -> None:
    """t in [-300, -240) → ours flag up, prep down, special none."""
    fs = flag_state(_state_at(-280), at(-280))
    assert fs.class_flag_up == "ours"
    assert fs.prep_flag_up is None
    assert fs.special_flag_up is None


def test_flag_state_5410_prep_window_no_stack() -> None:
    """t in [-240, -60) → ours up, prep up."""
    fs = flag_state(_state_at(-100), at(-100))
    assert fs.class_flag_up == "ours"
    assert fs.prep_flag_up == "P"


def test_flag_state_5410_one_min_signal_no_stack() -> None:
    """t in [-60, 0) → ours up, prep down."""
    fs = flag_state(_state_at(-30), at(-30))
    assert fs.class_flag_up == "ours"
    assert fs.prep_flag_up is None


def test_flag_state_started_no_stack() -> None:
    fs = flag_state(_state_at(5), at(5))
    assert fs.class_flag_up is None
    assert fs.special_flag_up is None
    assert fs.note == "started"


def test_flag_state_3210_prep_window() -> None:
    fs = flag_state(_state_at(-100, "3-2-1-0"), at(-100))
    assert fs.class_flag_up == "ours"
    assert fs.prep_flag_up == "P"


def test_flag_state_3210_pre_warning() -> None:
    fs = flag_state(_state_at(-200, "3-2-1-0"), at(-200))
    assert fs.class_flag_up is None  # before our warning


# ---------------------------------------------------------------------------
# Flag resolver — special flags
# ---------------------------------------------------------------------------


def test_flag_state_postponed_shows_AP() -> None:
    s = postpone(arm("5-4-1-0", T0))
    fs = flag_state(s, at(-100))
    assert fs.special_flag_up == "AP"
    assert fs.class_flag_up is None


def test_flag_state_general_recall_shows_first_sub() -> None:
    s = tick(arm("5-4-1-0", T0), at(-30))
    s = general_recall(s, at(-30))
    fs = flag_state(s, at(-30))
    assert fs.special_flag_up == "First Sub"


def test_flag_state_abandoned_shows_N() -> None:
    s = tick(arm("5-4-1-0", T0), at(-30))
    s = abandon(s)
    fs = flag_state(s, at(-30))
    assert fs.special_flag_up == "N"


def test_flag_state_idle_shows_nothing() -> None:
    fs = flag_state(IDLE, T0)
    assert fs == FlagState(None, None, None, None, note="no active sequence")


# ---------------------------------------------------------------------------
# Flag resolver — multi-class stack (5-4-1-0, gap=300)
# ---------------------------------------------------------------------------


def _stack(kind: str = "5-4-1-0") -> tuple[ClassEntry, ...]:
    """3-class stack: PHRF-A first, J/70 (ours) second, PHRF-B third."""
    return (
        ClassEntry(name="PHRF-A", order=0, prep_flag="P"),
        ClassEntry(name="J/70", order=1, is_ours=True, prep_flag="I"),
        ClassEntry(name="PHRF-B", order=2, prep_flag="P"),
    )


def test_flag_state_stack_class_ahead_in_warning() -> None:
    """Our t0 - 400s = PHRF-A's t0 - 100s. Their flag should be up, in
    prep window (since 100s before their t0 falls in [-240, -60))."""
    s = arm("5-4-1-0", T0, _stack())
    fs = flag_state(tick(s, at(-400)), at(-400))
    assert fs.class_flag_up == "PHRF-A"
    assert fs.prep_flag_up == "P"


def test_flag_state_stack_our_warning_when_class_ahead_just_started() -> None:
    """At t = -300 (our warning signal): PHRF-A just started 0s ago, our
    flag goes up. Prep window for ours starts at -240, so prep is down."""
    s = arm("5-4-1-0", T0, _stack())
    fs = flag_state(tick(s, at(-299)), at(-299))
    assert fs.class_flag_up == "J/70"
    assert fs.prep_flag_up is None


def test_flag_state_stack_our_prep_window() -> None:
    s = arm("5-4-1-0", T0, _stack())
    fs = flag_state(tick(s, at(-100)), at(-100))
    assert fs.class_flag_up == "J/70"
    assert fs.prep_flag_up == "I"  # our prep flag


def test_flag_state_stack_after_our_start_class_behind_warning_up() -> None:
    """At our t = +100, PHRF-B is 200s before its t0 (in their warning,
    pre-prep window since prep starts at -240)."""
    s = arm("5-4-1-0", T0, _stack())
    fs = flag_state(tick(s, at(100)), at(100))
    # We've started; their warning was at our +0, their start at our +300.
    # At our +100 = their -200 → their flag up, prep down.
    # Note: our state.phase will be "started" — the resolver must still
    # show the trailing class's flag.
    # (Currently, our resolver returns "started" early if no class is in
    # warning AND state.phase == started. PHRF-B *is* in warning, so we
    # expect their flag up.)
    assert fs.class_flag_up == "PHRF-B"


def test_flag_state_stack_next_change_in_seconds() -> None:
    """At t = -100 (in our prep), next change is the 1-min signal (prep down)."""
    s = arm("5-4-1-0", T0, _stack())
    fs = flag_state(tick(s, at(-100)), at(-100))
    assert fs.next_change_in_s == pytest.approx(40.0)  # prep down at -60


# ---------------------------------------------------------------------------
# Geometry — line metrics
# ---------------------------------------------------------------------------


def _line_ew(length_m: float = 100.0) -> StartLine:
    """East-west line, boat-end at origin, pin-end ~length_m east."""
    # 1 deg longitude at 47.65°N ≈ 75 km. 100m ≈ 0.001349°.
    dlon = length_m / (111_320 * math.cos(math.radians(47.65)))
    return StartLine(
        boat_end_lat=47.65,
        boat_end_lon=-122.40,
        boat_end_captured_at=T0,
        pin_end_lat=47.65,
        pin_end_lon=-122.40 + dlon,
        pin_end_captured_at=T0,
    )


def test_line_metrics_incomplete_returns_none() -> None:
    assert (
        line_metrics(StartLine(), boat_lat=47.65, boat_lon=-122.40, sog_kn=5.0, twd_deg=0.0) is None
    )


def test_line_metrics_bearing_and_length() -> None:
    line = _line_ew(100.0)
    m = line_metrics(line, boat_lat=None, boat_lon=None, sog_kn=None, twd_deg=None)
    assert m is not None
    assert m.line_length_m == pytest.approx(100.0, abs=1.0)
    # East-west line: bearing from boat (west end) to pin (east end) ≈ 90°.
    assert m.line_bearing_deg == pytest.approx(90.0, abs=0.5)


def test_line_metrics_no_twd_omits_bias_and_side() -> None:
    line = _line_ew(100.0)
    m = line_metrics(line, boat_lat=47.65, boat_lon=-122.401, sog_kn=5.0, twd_deg=None)
    assert m is not None
    assert m.line_bias_deg is None
    assert m.favoured_end is None
    assert "TWD" in m.note


def test_line_metrics_pin_favoured_when_pin_to_windward() -> None:
    """East-west line, wind from due east (TWD=90°): pin end is straight
    upwind, so pin is massively favoured (+90° bias)."""
    line = _line_ew(100.0)
    m = line_metrics(line, boat_lat=47.65, boat_lon=-122.401, sog_kn=5.0, twd_deg=90.0)
    assert m is not None
    assert m.line_bias_deg == pytest.approx(90.0, abs=0.5)
    assert m.favoured_end == "pin"


def test_line_metrics_boat_favoured_when_boat_to_windward() -> None:
    """East-west line, wind from due west (TWD=270°): boat end favoured."""
    line = _line_ew(100.0)
    m = line_metrics(line, boat_lat=47.65, boat_lon=-122.401, sog_kn=5.0, twd_deg=270.0)
    assert m is not None
    assert m.line_bias_deg is not None
    assert m.line_bias_deg < 0
    assert m.favoured_end == "boat"


def test_line_metrics_neutral_bias() -> None:
    """East-west line, wind from due north (TWD=0°): square line, neutral."""
    line = _line_ew(100.0)
    m = line_metrics(line, boat_lat=47.65, boat_lon=-122.401, sog_kn=5.0, twd_deg=0.0)
    assert m is not None
    assert abs(m.line_bias_deg or 0) < 1.0
    assert m.favoured_end == "neutral"


def test_line_metrics_low_sog_suppresses_time_to_line() -> None:
    line = _line_ew(100.0)
    m = line_metrics(
        line,
        boat_lat=47.6505,
        boat_lon=-122.40,
        sog_kn=SOG_FLOOR_KN - 0.01,
        twd_deg=0.0,
    )
    assert m is not None
    assert m.time_to_line_s is None
    assert m.time_to_burn_s is None


def test_line_metrics_time_to_line_at_known_speed() -> None:
    """Boat 50m due-north of an east-west line, SOG=10kn (5.144 m/s) → ~9.7s."""
    line = _line_ew(100.0)
    # 50m due north of the boat-end ≈ +0.0004495° latitude.
    m = line_metrics(
        line,
        boat_lat=47.65 + 0.0004495,
        boat_lon=-122.40,
        sog_kn=10.0,
        twd_deg=0.0,
    )
    assert m is not None
    assert m.distance_to_line_m == pytest.approx(50.0, abs=1.0)
    assert m.time_to_line_s == pytest.approx(50.0 / (10.0 * 0.514444), abs=0.2)


def test_line_metrics_distance_zero_when_on_line() -> None:
    line = _line_ew(100.0)
    # On the line itself.
    m = line_metrics(
        line,
        boat_lat=47.65,
        boat_lon=-122.4005,
        sog_kn=3.0,
        twd_deg=0.0,
    )
    assert m is not None
    assert m.distance_to_line_m == pytest.approx(0.0, abs=1.0)
    assert m.side_of_line == "on_line"


# ---------------------------------------------------------------------------
# Returned types — sanity checks
# ---------------------------------------------------------------------------


def test_flag_state_is_frozen() -> None:
    fs = FlagState(None, None, None, None)
    with pytest.raises(FrozenInstanceError):
        fs.class_flag_up = "x"  # type: ignore[misc]


def test_sequence_state_is_frozen() -> None:
    s = SequenceState(phase="idle")
    with pytest.raises(FrozenInstanceError):
        s.phase = "armed"  # type: ignore[misc]


def test_line_metrics_is_frozen() -> None:
    line = _line_ew(100.0)
    m = line_metrics(line, boat_lat=47.65, boat_lon=-122.401, sog_kn=5.0, twd_deg=0.0)
    assert isinstance(m, LineMetrics)
    with pytest.raises(FrozenInstanceError):
        m.line_bearing_deg = 0.0  # type: ignore[misc]
