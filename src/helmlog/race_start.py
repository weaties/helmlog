"""Race start management — pure domain logic for #644.

State machine, multi-class flag resolver, and start-line geometry.
No DB or web dependencies — importable everywhere, fully unit-testable.

See `gh issue view 644` and the spec comment for the full decision tables
and EARS requirements that drive the implementation here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Constants — see spec §C and §E
# ---------------------------------------------------------------------------

SequenceKind = Literal["5-4-1-0", "3-2-1-0"]

#: Recognized standard sequences. Match-racing dial-up deferred to follow-up.
SEQUENCE_KINDS: tuple[SequenceKind, ...] = ("5-4-1-0", "3-2-1-0")

#: After this many seconds past t0 a general recall is no longer accepted.
GENERAL_RECALL_GRACE_S: float = 60.0

#: Below this SOG (knots) time-to-line and time-to-burn are unstable; suppress.
SOG_FLOOR_KN: float = 0.5

#: Earth radius in metres for haversine.
_EARTH_R_M: float = 6_371_008.8

#: Knot to m/s.
_KN_TO_MPS: float = 0.514444


def warning_seconds(kind: SequenceKind) -> int:
    """Length of the full warning sequence for *kind* (start signal at t=0)."""
    if kind == "5-4-1-0":
        return 300
    if kind == "3-2-1-0":
        return 180
    raise ValueError(f"unknown sequence kind: {kind!r}")


def class_gap_seconds(kind: SequenceKind) -> int:
    """Stagger between consecutive class starts for *kind*."""
    return warning_seconds(kind)


def prep_window_seconds(kind: SequenceKind) -> tuple[int, int]:
    """(prep_up_at, prep_down_at) seconds **before t0** for *kind*.

    Prep flag (P/I/Z/U/Black) goes up at the 4-min (or 2-min) signal and
    comes down at the 1-min signal.
    """
    if kind == "5-4-1-0":
        return (240, 60)
    if kind == "3-2-1-0":
        return (120, 60)
    raise ValueError(f"unknown sequence kind: {kind!r}")


# ---------------------------------------------------------------------------
# Phase enum — derived from SequenceState fields
# ---------------------------------------------------------------------------

Phase = Literal[
    "idle",
    "armed",
    "counting_down",
    "postponed",
    "general_recall",
    "started",
    "abandoned",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassEntry:
    """One class in the day's start stack.

    ``order`` is 0 for the first class to start, 1 for the second, etc.
    Exactly one entry should have ``is_ours=True``.
    """

    name: str
    order: int
    is_ours: bool = False
    prep_flag: str = "P"  # P, I, Z, U, Black

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("class name must be non-empty")
        if self.order < 0:
            raise ValueError("class order must be >= 0")
        if self.prep_flag not in {"P", "I", "Z", "U", "Black"}:
            raise ValueError(f"invalid prep flag: {self.prep_flag!r}")


@dataclass(frozen=True)
class StartLine:
    """Start line endpoints and capture timestamps.

    A line is "complete" only when both ends are pinged. Either end may be
    re-pinged independently; the latest values win.
    """

    boat_end_lat: float | None = None
    boat_end_lon: float | None = None
    boat_end_captured_at: datetime | None = None
    pin_end_lat: float | None = None
    pin_end_lon: float | None = None
    pin_end_captured_at: datetime | None = None

    @property
    def is_complete(self) -> bool:
        return (
            self.boat_end_lat is not None
            and self.boat_end_lon is not None
            and self.pin_end_lat is not None
            and self.pin_end_lon is not None
        )


@dataclass(frozen=True)
class SequenceState:
    """The live race-start sequence state (singleton in DB)."""

    phase: Phase
    kind: SequenceKind | None = None
    t0_utc: datetime | None = None
    sync_offset_s: float = 0.0
    last_sync_at_utc: datetime | None = None
    started_at_utc: datetime | None = None
    classes: tuple[ClassEntry, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# FSM transitions — pure functions returning new state
# ---------------------------------------------------------------------------


IDLE: SequenceState = SequenceState(phase="idle")


def _validate_classes(classes: tuple[ClassEntry, ...]) -> None:
    if not classes:
        return
    orders = [c.order for c in classes]
    if len(set(orders)) != len(orders):
        raise ValueError("class orders must be unique")
    ours_count = sum(1 for c in classes if c.is_ours)
    if ours_count != 1:
        raise ValueError(f"exactly one class must be ours, got {ours_count}")


def arm(
    kind: SequenceKind,
    t0_utc: datetime,
    classes: tuple[ClassEntry, ...] = (),
) -> SequenceState:
    """Arm a new sequence with start signal scheduled at *t0_utc*."""
    if kind not in SEQUENCE_KINDS:
        raise ValueError(f"unknown sequence kind: {kind!r}")
    if t0_utc.tzinfo is None:
        raise ValueError("t0_utc must be timezone-aware (UTC)")
    _validate_classes(classes)
    return SequenceState(
        phase="armed",
        kind=kind,
        t0_utc=t0_utc,
        classes=classes,
    )


def tick(state: SequenceState, now_utc: datetime) -> SequenceState:
    """Advance phase based on wall-clock time.

    Pure function — no I/O. Callers invoke this whenever they read state to
    derive the current phase. Idempotent.
    """
    if state.phase in {"idle", "postponed", "general_recall", "abandoned"}:
        return state
    if state.t0_utc is None:
        return state
    if state.phase == "armed":
        if now_utc >= state.t0_utc:
            return SequenceState(
                phase="started",
                kind=state.kind,
                t0_utc=state.t0_utc,
                sync_offset_s=state.sync_offset_s,
                last_sync_at_utc=state.last_sync_at_utc,
                started_at_utc=state.t0_utc,
                classes=state.classes,
            )
        return SequenceState(**{**state.__dict__, "phase": "counting_down"})
    if state.phase == "counting_down" and now_utc >= state.t0_utc:
        return SequenceState(
            phase="started",
            kind=state.kind,
            t0_utc=state.t0_utc,
            sync_offset_s=state.sync_offset_s,
            last_sync_at_utc=state.last_sync_at_utc,
            started_at_utc=state.t0_utc,
            classes=state.classes,
        )
    return state


def sync_to_gun(
    state: SequenceState,
    now_utc: datetime,
    expected_signal_offset_s: int,
) -> SequenceState:
    """Re-anchor t0 so the *current* moment maps to the warning/prep/start signal.

    *expected_signal_offset_s* is the seconds-before-t0 the user is syncing
    against (300 for the warning gun on 5-4-1-0, 240 for prep, 60 for the
    1-min signal, 0 for the start gun, etc.).
    """
    if state.phase not in {"armed", "counting_down"}:
        raise ValueError(f"cannot sync from phase {state.phase!r}")
    if state.t0_utc is None or state.kind is None:
        raise ValueError("state has no t0/kind")
    new_t0 = now_utc + timedelta(seconds=expected_signal_offset_s)
    offset = (new_t0 - state.t0_utc).total_seconds()
    return SequenceState(
        phase="counting_down",
        kind=state.kind,
        t0_utc=new_t0,
        sync_offset_s=state.sync_offset_s + offset,
        last_sync_at_utc=now_utc,
        classes=state.classes,
    )


def nudge(state: SequenceState, delta_s: int) -> SequenceState:
    """Shift t0 by *delta_s* seconds (positive = later).

    Allowed from armed, counting_down, and started. Nudging from started
    re-anchors the gun moment after the fact (e.g. crew realises the RC
    actually fired the gun 5s later than the FSM thinks); the FSM stays
    in started and ``started_at_utc`` is updated to match the new t0.
    """
    if state.phase not in {"armed", "counting_down", "started"}:
        raise ValueError(f"cannot nudge from phase {state.phase!r}")
    if state.t0_utc is None:
        raise ValueError("state has no t0")
    new_t0 = state.t0_utc + timedelta(seconds=delta_s)
    update: dict[str, Any] = {"t0_utc": new_t0}
    if state.phase == "started":
        update["started_at_utc"] = new_t0
    return SequenceState(**{**state.__dict__, **update})


def postpone(state: SequenceState) -> SequenceState:
    """Raise AP — pause countdown."""
    if state.phase not in {"armed", "counting_down"}:
        raise ValueError(f"cannot postpone from phase {state.phase!r}")
    return SequenceState(**{**state.__dict__, "phase": "postponed"})


def resume_from_postponement(state: SequenceState, new_t0_utc: datetime) -> SequenceState:
    """AP comes down — sequence resumes with a new t0."""
    if state.phase != "postponed":
        raise ValueError(f"cannot resume from phase {state.phase!r}")
    if new_t0_utc.tzinfo is None:
        raise ValueError("new_t0_utc must be timezone-aware (UTC)")
    return SequenceState(**{**state.__dict__, "phase": "counting_down", "t0_utc": new_t0_utc})


def general_recall(state: SequenceState, now_utc: datetime) -> SequenceState:
    """Raise First Substitute — countdown halted pending restart.

    Only valid while in counting_down, or within ``GENERAL_RECALL_GRACE_S``
    seconds after t0 if already started.
    """
    if state.phase == "counting_down":
        return SequenceState(**{**state.__dict__, "phase": "general_recall"})
    if state.phase == "started":
        if state.t0_utc is None:
            raise ValueError("state has no t0")
        elapsed = (now_utc - state.t0_utc).total_seconds()
        if elapsed > GENERAL_RECALL_GRACE_S:
            raise ValueError(
                f"general recall window expired ({elapsed:.0f}s > {GENERAL_RECALL_GRACE_S:.0f}s)"
            )
        return SequenceState(
            **{**state.__dict__, "phase": "general_recall", "started_at_utc": None}
        )
    raise ValueError(f"cannot general-recall from phase {state.phase!r}")


def restart_after_recall(state: SequenceState, new_t0_utc: datetime) -> SequenceState:
    """First Sub down, sequence restarts. Line pings are preserved (kept in
    the StartLine record outside this state)."""
    if state.phase != "general_recall":
        raise ValueError(f"cannot restart from phase {state.phase!r}")
    if state.kind is None:
        raise ValueError("state has no kind")
    return SequenceState(
        phase="armed",
        kind=state.kind,
        t0_utc=new_t0_utc,
        sync_offset_s=state.sync_offset_s,
        last_sync_at_utc=state.last_sync_at_utc,
        classes=state.classes,
    )


def abandon(state: SequenceState) -> SequenceState:
    """Raise N — race abandoned."""
    if state.phase == "idle":
        raise ValueError("nothing to abandon")
    return SequenceState(**{**state.__dict__, "phase": "abandoned"})


def reset() -> SequenceState:
    """Discard sequence — back to idle."""
    return IDLE


# ---------------------------------------------------------------------------
# Multi-class flag resolver — see spec §C
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagState:
    """What the RC boat *should* be flying right now, given the stack."""

    class_flag_up: str | None  # name of the class whose flag is currently up
    prep_flag_up: str | None  # P/I/Z/U/Black, None when down
    special_flag_up: str | None  # AP / N / First Sub / X (or None)
    next_change_in_s: float | None  # seconds to next flag transition
    note: str = ""


def _our_class(classes: tuple[ClassEntry, ...]) -> ClassEntry | None:
    for c in classes:
        if c.is_ours:
            return c
    return None


def flag_state(
    state: SequenceState,
    now_utc: datetime,
) -> FlagState:
    """Resolve which flags are up on the RC boat right now."""
    if state.phase == "abandoned":
        return FlagState(None, None, "N", None, note="race abandoned")
    if state.phase == "postponed":
        return FlagState(None, None, "AP", None, note="sequence postponed")
    if state.phase == "general_recall":
        return FlagState(None, None, "First Sub", None, note="general recall")
    if state.phase in {"idle", "armed"} or state.t0_utc is None or state.kind is None:
        return FlagState(None, None, None, None, note="no active sequence")

    ours = _our_class(state.classes)
    gap = class_gap_seconds(state.kind)
    warn_s = warning_seconds(state.kind)
    prep_up_s, prep_down_s = prep_window_seconds(state.kind)

    # Order of *our* class — 0 if we're first, etc. Defaults to 0 if no
    # stack configured.
    our_order = ours.order if ours else 0

    # t_offset is seconds since *our* t0 (negative before our start).
    t_offset_ours = (now_utc - state.t0_utc).total_seconds()

    # Each class N's t0 is at our_t0 + (N - our_order) * gap.
    # The class whose flag is currently up is the one whose warning has
    # fired but whose start has not yet (i.e. its own t_offset in [-warn_s, 0)).
    class_flag_up: str | None = None
    prep_flag_up: str | None = None
    next_change: float | None = None

    if state.classes:
        # Find the class whose own warning is up.
        for c in state.classes:
            t_offset_c = t_offset_ours + (our_order - c.order) * gap
            if -warn_s <= t_offset_c < 0:
                class_flag_up = c.name
                if -prep_up_s <= t_offset_c < -prep_down_s:
                    prep_flag_up = c.prep_flag
                # Compute next transition: prep up, prep down, or start gun
                candidates = [-prep_up_s, -prep_down_s, 0]
                deltas = [c_t - t_offset_c for c_t in candidates if c_t - t_offset_c > 0]
                if deltas:
                    next_change = min(deltas)
                break
        else:
            # No class warning currently up. We're either before the first
            # warning or after the last start.
            if state.phase == "started":
                # Last start has fired (ours, since this is our state).
                # If individual recall logic is added later, X comes here.
                return FlagState(None, None, None, None, note="started")
    else:
        # No stack configured — render as if it's a single-class start of ours.
        if -warn_s <= t_offset_ours < 0:
            class_flag_up = "ours"
            if -prep_up_s <= t_offset_ours < -prep_down_s:
                prep_flag_up = "P"
            candidates = [-prep_up_s, -prep_down_s, 0]
            deltas = [c_t - t_offset_ours for c_t in candidates if c_t - t_offset_ours > 0]
            if deltas:
                next_change = min(deltas)
        elif state.phase == "started":
            return FlagState(None, None, None, None, note="started")

    return FlagState(class_flag_up, prep_flag_up, None, next_change)


# ---------------------------------------------------------------------------
# Geometry — start line metrics
# ---------------------------------------------------------------------------


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (forward azimuth) from point 1 to point 2, [0, 360)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    brg = math.degrees(math.atan2(x, y))
    return (brg + 360.0) % 360.0


def _angle_diff_deg(a: float, b: float) -> float:
    """Signed shortest difference (a - b), wrapped to [-180, 180]."""
    d = (a - b + 540.0) % 360.0 - 180.0
    return d


def _cross_track_distance_m(
    lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Signed perpendicular distance from point to great-circle line.

    Positive when the point is to the *left* of the line direction
    (1 → 2). Result in metres, small-distance approximation acceptable for
    start-line scales (line length ~ tens to hundreds of metres).
    """
    d13 = _haversine_m(lat1, lon1, lat, lon) / _EARTH_R_M
    brg13 = math.radians(_initial_bearing_deg(lat1, lon1, lat, lon))
    brg12 = math.radians(_initial_bearing_deg(lat1, lon1, lat2, lon2))
    return math.asin(math.sin(d13) * math.sin(brg13 - brg12)) * _EARTH_R_M


@dataclass(frozen=True)
class LineMetrics:
    """Derived values from the start line + boat state. None for any value
    that cannot be computed (e.g. low SOG, missing TWD)."""

    line_bearing_deg: float
    line_length_m: float
    line_bias_deg: float | None  # +ve = pin-end favoured
    favoured_end: Literal["boat", "pin", "neutral"] | None
    distance_to_line_m: float | None  # absolute, perpendicular
    side_of_line: Literal["pre_start", "post_start", "on_line"] | None
    time_to_line_s: float | None
    time_to_burn_s: float | None
    note: str = ""


def line_metrics(
    line: StartLine,
    *,
    boat_lat: float | None,
    boat_lon: float | None,
    sog_kn: float | None,
    twd_deg: float | None,
    cog_deg: float | None = None,
) -> LineMetrics | None:
    """Compute live line metrics. Returns None if the line is incomplete.

    *cog_deg* is course over ground; used to determine pre/post-start side.
    *twd_deg* is true wind direction (the direction the wind is blowing
    *from*). Bias is positive when the pin end is favoured (closer to the
    wind), matching #528.
    """
    if not line.is_complete:
        return None

    assert line.boat_end_lat is not None  # narrowed by is_complete
    assert line.boat_end_lon is not None
    assert line.pin_end_lat is not None
    assert line.pin_end_lon is not None

    line_bearing = _initial_bearing_deg(
        line.boat_end_lat, line.boat_end_lon, line.pin_end_lat, line.pin_end_lon
    )
    line_length = _haversine_m(
        line.boat_end_lat, line.boat_end_lon, line.pin_end_lat, line.pin_end_lon
    )

    # Bias: bearing from pin → boat is line_bearing + 180; the perpendicular
    # ("up the course") bisects the bearing toward the wind. Bias is the
    # signed difference between the wind direction and the line normal
    # toward the course. Positive value means the pin end is closer to
    # head-to-wind, i.e. pin favoured.
    bias: float | None = None
    favoured: Literal["boat", "pin", "neutral"] | None = None
    if twd_deg is not None:
        # delta = signed angle between boat→pin bearing and wind direction.
        # |delta| < 90  → pin end is on the windward side (pin favoured, +ve)
        # |delta| > 90  → boat end is on the windward side (boat favoured, -ve)
        # |delta| == 90 → square line (neutral, 0°)
        delta = _angle_diff_deg(line_bearing, twd_deg)
        bias = 90.0 - abs(delta)
        if abs(bias) < 1.0:
            favoured = "neutral"
        elif bias > 0:
            favoured = "pin"
        else:
            favoured = "boat"

    distance: float | None = None
    side: Literal["pre_start", "post_start", "on_line"] | None = None
    time_to_line: float | None = None
    time_to_burn: float | None = None

    if boat_lat is not None and boat_lon is not None:
        signed = _cross_track_distance_m(
            boat_lat,
            boat_lon,
            line.boat_end_lat,
            line.boat_end_lon,
            line.pin_end_lat,
            line.pin_end_lon,
        )
        distance = abs(signed)
        # Determine side relative to wind. Without TWD we can still say
        # left/right; "pre_start" requires knowing which side the wind is
        # coming from. We treat positive cross-track (boat to left of
        # boat→pin vector) as one side; with TWD we assign pre/post.
        if abs(signed) < 1.0:
            side = "on_line"
        elif twd_deg is not None:
            # Pre-start side is the side the wind is coming from.
            rel = _angle_diff_deg(line_bearing, twd_deg)
            wind_on_left = rel > 0  # wind is to the left of boat→pin
            on_left = signed > 0
            side = "pre_start" if on_left == wind_on_left else "post_start"

        if sog_kn is not None and sog_kn >= SOG_FLOOR_KN:
            speed_mps = sog_kn * _KN_TO_MPS
            time_to_line = distance / speed_mps
            # Time-to-burn: if we are on pre-start side, this is also
            # time_to_line. We expose both — UI uses time_to_burn for the
            # "kill speed" indicator.
            time_to_burn = time_to_line

    note = ""
    if twd_deg is None:
        note = "TWD needed for bias and side"

    return LineMetrics(
        line_bearing_deg=line_bearing,
        line_length_m=line_length,
        line_bias_deg=bias,
        favoured_end=favoured,
        distance_to_line_m=distance,
        side_of_line=side,
        time_to_line_s=time_to_line,
        time_to_burn_s=time_to_burn,
        note=note,
    )
