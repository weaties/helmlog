"""Maneuver detection from 1 Hz instrument data.

Detects tacks, gybes, and mark roundings from heading (HDG), boat speed (BSP),
and true wind angle (TWA) data stored in SQLite. No hardware dependencies —
input is decoded data structures.

Two-phase algorithm:
  Phase 1 — Event detection: slide a window over heading data and find all
  significant heading changes (≥ threshold). One event per heading change.

  Phase 2 — Classification via sailing state: for each event, measure the
  mean TWA *before* and *after* the event (with a buffer to skip the
  transition zone). If the boat stays upwind on both sides → tack. Stays
  downwind → gybe. Crosses 90° → mark rounding.

This naturally prevents tack→gybe transitions without an intervening rounding
because a state change is always classified as a rounding.

Phase 1 defaults (conservative): 60° heading-change threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Constants (Phase 1 conservative defaults — §23 of federation-design.md)
# ---------------------------------------------------------------------------

_HDG_THRESHOLD: float = 60.0  # minimum heading change to detect any maneuver (degrees)
_DETECTION_WINDOW_S: int = 15  # sliding window to accumulate heading change (seconds)
_PRE_WINDOW_S: int = 30  # look-back for BSP baseline (seconds)
_BSP_RECOVERY_FRACTION: float = 0.90  # fraction of baseline BSP to call "recovered"
_MIN_MANEUVER_GAP_S: int = 20  # minimum gap between consecutive maneuvers (seconds)

# State-classification buffers: measure TWA this many seconds before/after
# the event peak, skipping _STATE_SKIP_S on each side to avoid the transition.
_STATE_MEASURE_S: int = 10  # seconds of TWA to average for state determination
_STATE_SKIP_S: int = 3  # seconds to skip on each side of the event peak

# Wind reference codes (matches polar.py / storage.py convention)
_WIND_REF_BOAT = 0  # wind_angle_deg is TWA (boat-referenced)
_WIND_REF_NORTH = 4  # wind_angle_deg is TWD (north-referenced)


# ---------------------------------------------------------------------------
# Public API constants (tests import these)
# ---------------------------------------------------------------------------


def _tack_threshold() -> float:
    return _HDG_THRESHOLD


def _gybe_threshold() -> float:
    return _HDG_THRESHOLD


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Maneuver:
    """A single detected sailing maneuver."""

    type: str  # tack | gybe | rounding | maneuver
    ts: datetime  # UTC start of maneuver
    end_ts: datetime | None  # UTC end (BSP recovery), or None
    duration_sec: float | None  # seconds from start to recovery
    loss_kts: float | None  # BSP loss vs pre-maneuver baseline (kts)
    vmg_loss_kts: float | None  # VMG loss (future use)
    tws_bin: int | None  # TWS bin at maneuver time
    twa_bin: int | None  # TWA bin at maneuver time (folded [0,180])
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _heading_change(h1: float, h2: float) -> float:
    """Signed heading change from h1 to h2 along the shortest arc, in (−180, 180]."""
    diff = (h2 - h1 + 360.0) % 360.0
    return diff if diff <= 180.0 else diff - 360.0


def _abs_total_change(hdg_series: list[float]) -> float:
    """Absolute value of the summed signed heading changes in a series."""
    if len(hdg_series) < 2:
        return 0.0
    total = sum(
        _heading_change(hdg_series[i - 1], hdg_series[i]) for i in range(1, len(hdg_series))
    )
    return abs(total)


def _peak_change_index(hdg_series: list[float]) -> int:
    """Index of the maximum absolute per-step heading change in a series.

    Returns the index of the sample *after* the largest single-step change,
    i.e. the point where the boat is turning fastest.  Falls back to 0 if
    the series is too short.
    """
    if len(hdg_series) < 2:
        return 0
    best_idx = 1
    best_val = 0.0
    for i in range(1, len(hdg_series)):
        change = abs(_heading_change(hdg_series[i - 1], hdg_series[i]))
        if change > best_val:
            best_val = change
            best_idx = i
    return best_idx


def _bsp_loss(pre_bsp: list[float], window_bsp: list[float]) -> float | None:
    """Return baseline_bsp − min(window_bsp), or None if insufficient data."""
    if not pre_bsp or not window_bsp:
        return None
    baseline = sum(pre_bsp) / len(pre_bsp)
    minimum = min(window_bsp)
    loss = baseline - minimum
    return round(max(loss, 0.0), 3)


def _bsp_recovery_ts(
    baseline: float,
    post_ts_bsp: list[tuple[datetime, float]],
) -> tuple[datetime | None, float | None]:
    """Find the first timestamp where BSP recovers to _BSP_RECOVERY_FRACTION of baseline.

    Returns (recovery_ts, duration_sec from first point) or (None, None).
    """
    if not post_ts_bsp:
        return None, None
    target = baseline * _BSP_RECOVERY_FRACTION
    start_ts = post_ts_bsp[0][0]
    for ts, bsp in post_ts_bsp:
        if bsp >= target:
            duration = (ts - start_ts).total_seconds()
            return ts, round(duration, 1)
    return None, None


def _twa_bin_value(twa_deg: float) -> int:
    """5° TWA bin, folded to [0, 180] — matches polar.py convention."""
    import math

    twa_abs = abs(twa_deg) % 360
    if twa_abs > 180:
        twa_abs = 360 - twa_abs
    return int(math.floor(twa_abs / 5)) * 5


def _fold_twa(v: float) -> float:
    """Fold a TWA value from [0, 360) to [0, 180]."""
    return v if v <= 180 else 360 - v


# ---------------------------------------------------------------------------
# Phase 1: Detect heading-change events (type-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HeadingEvent:
    """A raw heading-change event before classification."""

    ts: datetime  # timestamp at peak heading change
    window_start_idx: int  # index into hdg list where the window started
    hdg_change_deg: float  # absolute total heading change in window
    loss_kts: float | None  # BSP loss vs pre-maneuver baseline
    end_ts: datetime | None  # BSP recovery timestamp
    duration_sec: float | None  # time to BSP recovery
    twa_bin: int | None  # TWA bin at event time
    tws_bin: int | None  # TWS bin at event time (populated later)


def _detect_heading_events(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
    threshold: float = _HDG_THRESHOLD,
) -> list[_HeadingEvent]:
    """Find all significant heading changes in the series.

    Returns a list of heading-change events, each with timing and BSP metrics.
    No classification (tack/gybe/rounding) is done here.
    """
    if len(hdg) < _DETECTION_WINDOW_S + 2:
        return []

    bsp_by_ts: dict[datetime, float] = dict(bsp)
    twa_by_ts: dict[datetime, float] = dict(twa)

    events: list[_HeadingEvent] = []
    last_event_ts: datetime | None = None
    n = len(hdg)

    i = 0
    while i <= n - _DETECTION_WINDOW_S:
        window_ts = [hdg[j][0] for j in range(i, i + _DETECTION_WINDOW_S)]
        window_hdg = [hdg[j][1] for j in range(i, i + _DETECTION_WINDOW_S)]

        total_change = _abs_total_change(window_hdg)

        if total_change < threshold:
            i += 1
            continue

        # Use the point of peak heading change as the event timestamp
        peak_idx = _peak_change_index(window_hdg)
        event_ts = window_ts[peak_idx]

        # Enforce gap between consecutive events
        if last_event_ts is not None:
            gap = (event_ts - last_event_ts).total_seconds()
            if gap < _MIN_MANEUVER_GAP_S:
                i += 1
                continue

        # BSP metrics
        pre_start = event_ts - timedelta(seconds=_PRE_WINDOW_S)
        pre_bsp = [v for ts, v in bsp if pre_start <= ts < event_ts]
        window_bsp = [bsp_by_ts[ts] for ts in window_ts if ts in bsp_by_ts]
        loss = _bsp_loss(pre_bsp, window_bsp)

        baseline = (
            sum(pre_bsp) / len(pre_bsp)
            if pre_bsp
            else (sum(window_bsp) / len(window_bsp) if window_bsp else 0.0)
        )

        # BSP recovery
        post_window_end = i + _DETECTION_WINDOW_S
        post_ts_bsp = [
            (hdg[j][0], bsp_by_ts[hdg[j][0]])
            for j in range(post_window_end, min(post_window_end + _PRE_WINDOW_S, n))
            if hdg[j][0] in bsp_by_ts
        ]
        end_ts, duration = _bsp_recovery_ts(baseline, post_ts_bsp)

        # TWA bin
        twa_vals = [twa_by_ts[ts] for ts in window_ts if ts in twa_by_ts]
        twa_val = sum(twa_vals) / len(twa_vals) if twa_vals else None
        twa_bin = _twa_bin_value(twa_val) if twa_val is not None else None

        events.append(
            _HeadingEvent(
                ts=event_ts,
                window_start_idx=i,
                hdg_change_deg=round(total_change, 1),
                loss_kts=loss,
                end_ts=end_ts,
                duration_sec=duration,
                twa_bin=twa_bin,
                tws_bin=None,
            )
        )
        last_event_ts = event_ts

        # Skip past this window to avoid re-detecting the same event
        i += _DETECTION_WINDOW_S

    return events


# ---------------------------------------------------------------------------
# Phase 2: Classify events using sailing state before/after
# ---------------------------------------------------------------------------


def _classify_events(
    events: list[_HeadingEvent],
    twa: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Classify heading-change events as tack, gybe, or rounding.

    For each event, measures the mean TWA in a window *before* and *after*
    the event (skipping a buffer around the peak to avoid the transition).

    - Both sides upwind (< 90°) → tack
    - Both sides downwind (> 90°) → gybe
    - Sides differ → rounding (mark rounding)
    """
    if not events:
        return []

    # Build a sorted list for efficient range queries
    twa_sorted = sorted(twa, key=lambda x: x[0])
    twa_times = [t for t, _ in twa_sorted]
    twa_values = [_fold_twa(v) for _, v in twa_sorted]

    def _mean_twa_in_range(start: datetime, end: datetime) -> float | None:
        """Mean folded TWA in [start, end). Returns None if no data."""
        import bisect

        lo = bisect.bisect_left(twa_times, start)
        hi = bisect.bisect_left(twa_times, end)
        if lo >= hi:
            return None
        vals = twa_values[lo:hi]
        return sum(vals) / len(vals)

    maneuvers: list[Maneuver] = []
    for event in events:
        # Measure sailing state BEFORE the event (skip buffer around peak)
        pre_end = event.ts - timedelta(seconds=_STATE_SKIP_S)
        pre_start = pre_end - timedelta(seconds=_STATE_MEASURE_S)
        pre_mean = _mean_twa_in_range(pre_start, pre_end)

        # Measure sailing state AFTER the event
        post_start = event.ts + timedelta(seconds=_STATE_SKIP_S)
        post_end = post_start + timedelta(seconds=_STATE_MEASURE_S)
        post_mean = _mean_twa_in_range(post_start, post_end)

        # Classify
        if pre_mean is not None and post_mean is not None:
            pre_upwind = pre_mean < 90.0
            post_upwind = post_mean < 90.0

            if pre_upwind == post_upwind:
                # Same sailing mode → tack or gybe
                maneuver_type = "tack" if pre_upwind else "gybe"
            else:
                # Mode changed → mark rounding
                maneuver_type = "rounding"
        elif pre_mean is not None:
            # Only have pre-data; classify by what we know
            maneuver_type = "tack" if pre_mean < 90.0 else "gybe"
        elif post_mean is not None:
            maneuver_type = "tack" if post_mean < 90.0 else "gybe"
        else:
            # No TWA data at all
            maneuver_type = "maneuver"

        maneuvers.append(
            Maneuver(
                type=maneuver_type,
                ts=event.ts,
                end_ts=event.end_ts,
                duration_sec=event.duration_sec,
                loss_kts=event.loss_kts,
                vmg_loss_kts=None,
                tws_bin=event.tws_bin,
                twa_bin=event.twa_bin,
                details={"hdg_change_deg": event.hdg_change_deg},
            )
        )

    return maneuvers


# ---------------------------------------------------------------------------
# Core detection: pure functions (testable without Storage)
# ---------------------------------------------------------------------------


def detect_all(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
    threshold: float = _HDG_THRESHOLD,
) -> list[Maneuver]:
    """Detect and classify all maneuvers from aligned 1 Hz instrument data.

    Two-phase approach:
    1. Find all significant heading changes (≥ threshold)
    2. Classify each using the sailing state (TWA) before/after the event

    Each argument is a list of (datetime, value) pairs, sorted by time.
    Returns detected Maneuver objects sorted by timestamp.
    """
    events = _detect_heading_events(hdg, bsp, twa, threshold)
    return _classify_events(events, twa)


def detect_tacks(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect tacks from aligned 1 Hz heading, BSP, and TWA series."""
    return [m for m in detect_all(hdg, bsp, twa) if m.type == "tack"]


def detect_gybes(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect gybes from aligned 1 Hz heading, BSP, and TWA series."""
    return [m for m in detect_all(hdg, bsp, twa) if m.type == "gybe"]


def detect_mark_roundings(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect mark roundings from aligned 1 Hz heading, BSP, and TWA series."""
    return [m for m in detect_all(hdg, bsp, twa) if m.type == "rounding"]


def detect_course_changes(
    cog: list[tuple[datetime, float]],
    sog: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect significant course changes when no true wind data is available.

    Uses COG and SOG (GPS-derived) instead of compass heading and boat speed.
    Maneuvers are typed as ``"maneuver"`` because tack/gybe classification
    requires TWA, which is absent in GPS-only sessions.
    """
    events = _detect_heading_events(cog, sog, [], _HDG_THRESHOLD)
    # No TWA → all events are unclassified "maneuver"
    return [
        Maneuver(
            type="maneuver",
            ts=e.ts,
            end_ts=e.end_ts,
            duration_sec=e.duration_sec,
            loss_kts=e.loss_kts,
            vmg_loss_kts=None,
            tws_bin=None,
            twa_bin=None,
            details={"hdg_change_deg": e.hdg_change_deg},
        )
        for e in events
    ]


# ---------------------------------------------------------------------------
# Storage integration
# ---------------------------------------------------------------------------


async def detect_maneuvers(storage: Storage, session_id: int) -> list[Maneuver]:
    """Detect all maneuvers in a completed session and persist them.

    Reads instrument data from storage, runs unified detection + classification,
    writes results to the maneuvers table (replaces any previous results for the
    session — idempotent).

    Returns the list of detected Maneuver objects.
    """
    db = storage._conn()
    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    race = await cur.fetchone()
    if race is None:
        logger.warning("detect_maneuvers: session {} not found", session_id)
        return []

    if not race["end_utc"]:
        logger.warning("detect_maneuvers: session {} has no end_utc, skipping", session_id)
        return []

    try:
        start = datetime.fromisoformat(str(race["start_utc"])).replace(tzinfo=UTC)
        end = datetime.fromisoformat(str(race["end_utc"])).replace(tzinfo=UTC)
    except ValueError:
        logger.warning("detect_maneuvers: session {} has invalid timestamps", session_id)
        return []

    # Load instrument data scoped to session_id where possible (race_id column).
    # This prevents data from overlapping synthesized sessions being mixed.
    # Fall back to unscoped query for real sailing data where race_id is NULL.
    headings_raw = await storage.query_range("headings", start, end, race_id=session_id)
    if not headings_raw:
        headings_raw = await storage.query_range("headings", start, end)
    speeds_raw = await storage.query_range("speeds", start, end, race_id=session_id)
    if not speeds_raw:
        speeds_raw = await storage.query_range("speeds", start, end)
    winds_raw = await storage.query_range("winds", start, end, race_id=session_id)
    if not winds_raw:
        winds_raw = await storage.query_range("winds", start, end)

    # Fetch cogsog only if needed
    cogsog_raw: list[dict[str, Any]] = []
    if not headings_raw or not speeds_raw:
        cogsog_raw = await storage.query_range("cogsog", start, end, race_id=session_id)
        if not cogsog_raw:
            cogsog_raw = await storage.query_range("cogsog", start, end)

    if not headings_raw and not cogsog_raw:
        logger.info(
            "detect_maneuvers: session {} has no heading or COG data",
            session_id,
        )
        await storage.write_maneuvers(session_id, [])
        return []

    if not speeds_raw and not cogsog_raw:
        logger.info(
            "detect_maneuvers: session {} has no speed or SOG data",
            session_id,
        )
        await storage.write_maneuvers(session_id, [])
        return []

    # Build time-keyed series (first record per second wins)
    hdg_series: dict[str, float] = {}
    if headings_raw:
        for r in headings_raw:
            key = str(r["ts"])[:19]
            hdg_series.setdefault(key, float(r["heading_deg"]))
    else:
        logger.info(
            "detect_maneuvers: session {} using COG as heading fallback (no HDG data)",
            session_id,
        )
        for r in cogsog_raw:
            key = str(r["ts"])[:19]
            hdg_series.setdefault(key, float(r["cog_deg"]))

    bsp_series: dict[str, float] = {}
    if speeds_raw:
        for r in speeds_raw:
            key = str(r["ts"])[:19]
            bsp_series.setdefault(key, float(r["speed_kts"]))
    else:
        logger.info(
            "detect_maneuvers: session {} using SOG as speed fallback (no BSP data)",
            session_id,
        )
        for r in cogsog_raw:
            key = str(r["ts"])[:19]
            bsp_series.setdefault(key, float(r["sog_kts"]))

    # Winds: filter to true-wind references only
    twa_series: dict[str, float] = {}
    tws_series: dict[str, float] = {}
    for r in winds_raw:
        ref = int(r.get("reference", -1))
        if ref not in (_WIND_REF_BOAT, _WIND_REF_NORTH):
            continue
        key = str(r["ts"])[:19]
        if ref == _WIND_REF_BOAT:
            raw_twa = abs(float(r["wind_angle_deg"])) % 360
            twa_val = raw_twa if raw_twa <= 180 else 360 - raw_twa
            twa_series.setdefault(key, twa_val)
            tws_series.setdefault(key, float(r["wind_speed_kts"]))
        else:
            # reference=4: north-referenced TWD — convert to TWA using heading
            twd = float(r["wind_angle_deg"]) % 360
            hdg_val = hdg_series.get(key)
            if hdg_val is not None:
                raw_twa = (twd - hdg_val + 360) % 360
                twa = raw_twa if raw_twa <= 180 else 360 - raw_twa
                twa_series.setdefault(key, twa)
            tws_series.setdefault(key, float(r["wind_speed_kts"]))

    # Build aligned sorted series
    all_keys = sorted(set(hdg_series) & set(bsp_series))
    if len(all_keys) < _DETECTION_WINDOW_S + 2:
        logger.info(
            "detect_maneuvers: session {} too short ({} aligned points)",
            session_id,
            len(all_keys),
        )
        await storage.write_maneuvers(session_id, [])
        return []

    def _parse_key(k: str) -> datetime:
        return datetime.fromisoformat(k).replace(tzinfo=UTC)

    hdg_list = [(_parse_key(k), hdg_series[k]) for k in all_keys]
    bsp_list = [(_parse_key(k), bsp_series[k]) for k in all_keys]

    # TWA: use aligned keys, fall back to interpolated constant if missing
    twa_keys = sorted(twa_series)
    if twa_keys:
        # Build a simple lookup with forward-fill for missing seconds
        twa_filled: dict[str, float] = {}
        last_twa = list(twa_series.values())[0]
        for k in all_keys:
            if k in twa_series:
                last_twa = twa_series[k]
            twa_filled[k] = last_twa
        twa_list = [(_parse_key(k), twa_filled[k]) for k in all_keys]
    else:
        # No true wind data — detect course changes without tack/gybe classification
        logger.info(
            "detect_maneuvers: session {} has no true wind data, "
            "detecting course changes from {} only",
            session_id,
            "COG" if not headings_raw else "HDG",
        )
        course_changes = detect_course_changes(hdg_list, bsp_list)
        course_changes.sort(key=lambda m: m.ts)
        await storage.write_maneuvers(session_id, course_changes)
        logger.info(
            "detect_maneuvers: session {} → {} course changes (no wind data)",
            session_id,
            len(course_changes),
        )
        return course_changes

    # Unified detection + classification
    all_maneuvers = detect_all(hdg_list, bsp_list, twa_list)

    # Annotate with TWS bin where available
    annotated: list[Maneuver] = []
    for m in all_maneuvers:
        ts_key = m.ts.isoformat()[:19]
        tws_val = tws_series.get(ts_key)
        if tws_val is not None:
            import math

            tws_bin = max(0, int(math.floor(tws_val)))
        else:
            tws_bin = None
        annotated.append(
            Maneuver(
                type=m.type,
                ts=m.ts,
                end_ts=m.end_ts,
                duration_sec=m.duration_sec,
                loss_kts=m.loss_kts,
                vmg_loss_kts=m.vmg_loss_kts,
                tws_bin=tws_bin,
                twa_bin=m.twa_bin,
                details=m.details,
            )
        )

    annotated.sort(key=lambda m: m.ts)

    await storage.write_maneuvers(session_id, annotated)
    tacks = sum(1 for m in annotated if m.type == "tack")
    gybes = sum(1 for m in annotated if m.type == "gybe")
    roundings = sum(1 for m in annotated if m.type == "rounding")
    logger.info(
        "detect_maneuvers: session {} → {} tacks, {} gybes, {} roundings",
        session_id,
        tacks,
        gybes,
        roundings,
    )
    return annotated
