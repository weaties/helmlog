"""Maneuver detection from 1 Hz instrument data.

Detects tacks, gybes, and mark roundings from heading (HDG), boat speed (BSP),
and true wind angle (TWA) data stored in SQLite. No hardware dependencies —
input is decoded data structures.

Algorithm (tack example):
1. Align HDG, BSP, TWA by truncated-second timestamp key.
2. Slide a 15 s window; sum signed heading changes (handles wrap-around).
3. |ΔHDG| > 70° and mean TWA < 90° → tack candidate.
4. BSP baseline: mean of pre-maneuver 30 s window.
5. BSP loss: baseline − min(BSP in window).
6. Duration: from first inflection to 90% BSP recovery.

Mark rounding detection:
  A heading change where TWA transitions across the 90° boundary (upwind ↔
  downwind) is classified as a mark rounding rather than a tack or gybe.
  The first and last thirds of the detection window are compared: if one is
  upwind and the other downwind, it's a rounding.

Phase 1 defaults (conservative): 70° tack threshold, 60° gybe threshold.
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

_TACK_HDG_THRESHOLD: float = 70.0  # minimum total heading change for a tack (degrees)
_GYBE_HDG_THRESHOLD: float = 60.0  # minimum total heading change for a gybe (degrees)
_ROUNDING_HDG_THRESHOLD: float = 60.0  # minimum total heading change for a mark rounding
_DETECTION_WINDOW_S: int = 15  # sliding window to accumulate heading change (seconds)
_PRE_WINDOW_S: int = 30  # look-back for BSP baseline (seconds)
_BSP_RECOVERY_FRACTION: float = 0.90  # fraction of baseline BSP to call "recovered"
_MIN_MANEUVER_GAP_S: int = 20  # minimum gap between consecutive maneuvers (seconds)

# Wind reference codes (matches polar.py / storage.py convention)
_WIND_REF_BOAT = 0  # wind_angle_deg is TWA (boat-referenced)
_WIND_REF_NORTH = 4  # wind_angle_deg is TWD (north-referenced)


# ---------------------------------------------------------------------------
# Public API constants (tests import these)
# ---------------------------------------------------------------------------


def _tack_threshold() -> float:
    return _TACK_HDG_THRESHOLD


def _gybe_threshold() -> float:
    return _GYBE_HDG_THRESHOLD


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Maneuver:
    """A single detected sailing maneuver."""

    type: str  # tack | gybe | rounding
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


# ---------------------------------------------------------------------------
# Core detection: pure functions (testable without Storage)
# ---------------------------------------------------------------------------


def detect_tacks(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect tacks from aligned 1 Hz heading, BSP, and TWA series.

    Each argument is a list of (datetime, value) pairs, sorted by time.
    Returns detected Maneuver objects (type="tack").
    """
    return _detect(hdg, bsp, twa, "tack", _TACK_HDG_THRESHOLD, upwind=True)


def detect_gybes(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect gybes from aligned 1 Hz heading, BSP, and TWA series.

    Each argument is a list of (datetime, value) pairs, sorted by time.
    Returns detected Maneuver objects (type="gybe").
    """
    return _detect(hdg, bsp, twa, "gybe", _GYBE_HDG_THRESHOLD, upwind=False)


def detect_course_changes(
    cog: list[tuple[datetime, float]],
    sog: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect significant course changes when no true wind data is available.

    Uses COG and SOG (GPS-derived) instead of compass heading and boat speed.
    Maneuvers are typed as ``"maneuver"`` because tack/gybe classification
    requires TWA, which is absent in GPS-only sessions.

    Uses the lower of the tack/gybe heading thresholds so that both types
    of maneuver are captured.
    """
    threshold = min(_TACK_HDG_THRESHOLD, _GYBE_HDG_THRESHOLD)
    return _detect(cog, sog, [], "maneuver", threshold, upwind=None)


def detect_mark_roundings(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
) -> list[Maneuver]:
    """Detect mark roundings from aligned 1 Hz heading, BSP, and TWA series.

    A mark rounding is a significant heading change where the TWA transitions
    across the 90° boundary — the boat moves from upwind to downwind (windward
    mark) or from downwind to upwind (leeward mark).  This distinguishes
    roundings from tacks (which stay upwind) and gybes (which stay downwind).

    Each argument is a list of (datetime, value) pairs, sorted by time.
    Returns detected Maneuver objects (type="rounding").
    """
    return _detect(
        hdg,
        bsp,
        twa,
        "rounding",
        _ROUNDING_HDG_THRESHOLD,
        upwind=None,
        require_twa_crossing=True,
    )


_TWA_CROSSING_MARGIN: float = 5.0  # min distance from 90° on each side to confirm crossing


def _twa_crosses_90(twa_values: list[float]) -> bool:
    """Return True if TWA transitions across the 90° boundary within the window.

    Compares the mean TWA of the first third against the last third.  If one
    is clearly upwind (<90° − margin) and the other clearly downwind
    (>90° + margin), the boat is rounding a mark rather than performing a
    tack or gybe.  The margin prevents noise near beam reach from triggering
    false mark roundings.
    """
    if len(twa_values) < 3:
        return False
    third = max(len(twa_values) // 3, 1)
    first_mean = sum(twa_values[:third]) / third
    last_mean = sum(twa_values[-third:]) / third
    # Both means must be clearly on opposite sides of 90°
    upwind_thresh = 90.0 - _TWA_CROSSING_MARGIN
    downwind_thresh = 90.0 + _TWA_CROSSING_MARGIN
    first_upwind = first_mean < upwind_thresh
    first_downwind = first_mean > downwind_thresh
    last_upwind = last_mean < upwind_thresh
    last_downwind = last_mean > downwind_thresh
    return (first_upwind and last_downwind) or (first_downwind and last_upwind)


def _twa_regime_changed(
    pre_twa: list[float],
    post_twa: list[float],
) -> bool:
    """Return True if the TWA regime changes from pre-window to post-window.

    If pre-maneuver sailing is clearly upwind (mean TWA < 90° − margin) and
    post-maneuver is clearly downwind (mean TWA > 90° + margin), or vice
    versa, the event is a mark rounding.  The margin prevents noise near beam
    reach from triggering false roundings.
    """
    if not pre_twa or not post_twa:
        return False
    pre_mean = sum(pre_twa) / len(pre_twa)
    post_mean = sum(post_twa) / len(post_twa)
    upwind_thresh = 90.0 - _TWA_CROSSING_MARGIN
    downwind_thresh = 90.0 + _TWA_CROSSING_MARGIN
    pre_upwind = pre_mean < upwind_thresh
    pre_downwind = pre_mean > downwind_thresh
    post_upwind = post_mean < upwind_thresh
    post_downwind = post_mean > downwind_thresh
    return (pre_upwind and post_downwind) or (pre_downwind and post_upwind)


def _detect(
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
    maneuver_type: str,
    threshold: float,
    upwind: bool | None,
    *,
    require_twa_crossing: bool = False,
) -> list[Maneuver]:
    """Shared sliding-window detector for tacks, gybes, and mark roundings.

    When *upwind* is True/False, the wind-angle filter selects upwind/downwind
    windows and rejects any where TWA crosses the 90° boundary (those are mark
    roundings).  When *upwind* is None and *require_twa_crossing* is False, no
    wind-angle filter is applied (GPS-only course changes).  When
    *require_twa_crossing* is True, only windows where TWA crosses the 90°
    boundary are kept (mark rounding detection).
    """
    if len(hdg) < _DETECTION_WINDOW_S + 2:
        return []

    # Build fast index: timestamp → value
    bsp_by_ts: dict[datetime, float] = dict(bsp)
    twa_by_ts: dict[datetime, float] = dict(twa)

    maneuvers: list[Maneuver] = []
    last_maneuver_ts: datetime | None = None
    n = len(hdg)

    i = 0
    while i <= n - _DETECTION_WINDOW_S:
        window_ts = [hdg[j][0] for j in range(i, i + _DETECTION_WINDOW_S)]
        window_hdg = [hdg[j][1] for j in range(i, i + _DETECTION_WINDOW_S)]

        total_change = _abs_total_change(window_hdg)

        if total_change < threshold:
            i += 1
            continue

        # Wind-angle filtering:
        # - tack (upwind=True): mean TWA < 90° and no regime change
        # - gybe (upwind=False): mean TWA > 90° and no regime change
        # - rounding (require_twa_crossing=True): TWA must cross the 90° boundary
        # - course change (upwind=None, require_twa_crossing=False): no filter
        if upwind is not None or require_twa_crossing:
            window_twa_raw = [twa_by_ts[ts] for ts in window_ts if ts in twa_by_ts]
            # Fold to [0, 180] — Signal K may report boat-referenced TWA in [0, 360).
            window_twa = [v if v <= 180 else 360 - v for v in window_twa_raw]
            if not window_twa:
                i += 1
                continue

            crosses_in_window = _twa_crosses_90(window_twa)

            # Also check pre-window vs post-window TWA regime to catch cases
            # where the window starts after the crossing has already begun.
            pre_idx = max(0, i - _PRE_WINDOW_S)
            pre_twa_vals = [
                (v if v <= 180 else 360 - v)
                for j in range(pre_idx, i)
                if hdg[j][0] in twa_by_ts
                for v in [twa_by_ts[hdg[j][0]]]
            ]
            post_end = min(n, i + _DETECTION_WINDOW_S + _PRE_WINDOW_S)
            post_twa_vals = [
                (v if v <= 180 else 360 - v)
                for j in range(i + _DETECTION_WINDOW_S, post_end)
                if hdg[j][0] in twa_by_ts
                for v in [twa_by_ts[hdg[j][0]]]
            ]
            regime_changed = _twa_regime_changed(pre_twa_vals, post_twa_vals)

            if require_twa_crossing:
                # Mark rounding: TWA must cross 90° in-window or regime must change
                if not crosses_in_window and not regime_changed:
                    i += 1
                    continue
            elif upwind is not None:
                # Tack/gybe: reject if TWA crosses 90° or regime changes
                if crosses_in_window or regime_changed:
                    i += 1
                    continue
                mean_twa = sum(window_twa) / len(window_twa)
                if upwind and mean_twa >= 90.0:
                    i += 1
                    continue
                if not upwind and mean_twa <= 90.0:
                    i += 1
                    continue

        # Use the point of peak heading change as the maneuver timestamp so
        # the GPS marker lands on the actual turn, not the window start.
        peak_idx = _peak_change_index(window_hdg)
        maneuver_start_ts = window_ts[peak_idx]

        # Enforce gap between consecutive maneuvers
        if last_maneuver_ts is not None:
            gap = (maneuver_start_ts - last_maneuver_ts).total_seconds()
            if gap < _MIN_MANEUVER_GAP_S:
                i += 1
                continue

        # BSP metrics
        pre_start = maneuver_start_ts - timedelta(seconds=_PRE_WINDOW_S)
        pre_bsp = [v for ts, v in bsp if pre_start <= ts < maneuver_start_ts]
        window_bsp = [bsp_by_ts[ts] for ts in window_ts if ts in bsp_by_ts]
        loss = _bsp_loss(pre_bsp, window_bsp)

        baseline = (
            sum(pre_bsp) / len(pre_bsp)
            if pre_bsp
            else (sum(window_bsp) / len(window_bsp) if window_bsp else 0.0)
        )

        # BSP recovery: look in post-window data
        post_window_end = i + _DETECTION_WINDOW_S
        post_ts_bsp = [
            (hdg[j][0], bsp_by_ts[hdg[j][0]])
            for j in range(post_window_end, min(post_window_end + _PRE_WINDOW_S, n))
            if hdg[j][0] in bsp_by_ts
        ]
        end_ts, duration = _bsp_recovery_ts(baseline, post_ts_bsp)

        # TWA/TWS bins
        twa_vals = [twa_by_ts[ts] for ts in window_ts if ts in twa_by_ts]
        twa_val = sum(twa_vals) / len(twa_vals) if twa_vals else None
        twa_bin = _twa_bin_value(twa_val) if twa_val is not None else None

        maneuver = Maneuver(
            type=maneuver_type,
            ts=maneuver_start_ts,
            end_ts=end_ts,
            duration_sec=duration,
            loss_kts=loss,
            vmg_loss_kts=None,
            tws_bin=None,  # populated by detect_maneuvers when TWS data is available
            twa_bin=twa_bin,
            details={"hdg_change_deg": round(total_change, 1)},
        )
        maneuvers.append(maneuver)
        last_maneuver_ts = maneuver_start_ts

        # Skip past this window to avoid re-detecting the same event
        i += _DETECTION_WINDOW_S

    return maneuvers


# ---------------------------------------------------------------------------
# Storage integration
# ---------------------------------------------------------------------------


async def detect_maneuvers(storage: Storage, session_id: int) -> list[Maneuver]:
    """Detect all maneuvers in a completed session and persist them.

    Reads instrument data from storage, runs tack + gybe detection, writes
    results to the maneuvers table (replaces any previous results for the
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

    # Load instrument data; fall back to cogsog (GPS) when direct sensor data absent
    headings_raw = await storage.query_range("headings", start, end)
    speeds_raw = await storage.query_range("speeds", start, end)
    winds_raw = await storage.query_range("winds", start, end)

    # Fetch cogsog only if needed — avoids unnecessary query when instruments are present
    cogsog_raw: list[dict[str, Any]] = []
    if not headings_raw or not speeds_raw:
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

    # Build time-keyed series (first record per second wins); prefer instrument
    # data and fall back to GPS-derived cogsog when instruments are absent.
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

    tacks = detect_tacks(hdg_list, bsp_list, twa_list)
    gybes = detect_gybes(hdg_list, bsp_list, twa_list)
    roundings = detect_mark_roundings(hdg_list, bsp_list, twa_list)

    # Cross-type deduplication: tacks/gybes are more specific than roundings,
    # so when a rounding overlaps temporally with a tack or gybe, drop it.
    tack_gybe_times = {m.ts for m in tacks + gybes}
    deduped_roundings: list[Maneuver] = []
    for r in roundings:
        too_close = any(
            abs((r.ts - tg_ts).total_seconds()) < _MIN_MANEUVER_GAP_S for tg_ts in tack_gybe_times
        )
        if not too_close:
            deduped_roundings.append(r)

    # Remove consecutive roundings — in a real race a rounding is always
    # followed by a tack or gybe leg before the next rounding.  When two
    # roundings appear in a row, keep the one with the larger heading change.
    if len(deduped_roundings) > 1:
        deduped_roundings.sort(key=lambda m: m.ts)
        # Merge all maneuver types and sort to check interleaving
        tg_sorted = sorted(tacks + gybes, key=lambda m: m.ts)
        filtered_roundings: list[Maneuver] = []
        for i, rnd in enumerate(deduped_roundings):
            if i == 0:
                filtered_roundings.append(rnd)
                continue
            prev = filtered_roundings[-1]
            # Check if there is a tack or gybe between prev rounding and this one
            has_intervening = any(prev.ts < tg.ts < rnd.ts for tg in tg_sorted)
            if has_intervening:
                filtered_roundings.append(rnd)
            else:
                # Consecutive roundings — keep the one with larger heading change
                prev_hdg = prev.details.get("hdg_change_deg", 0)
                rnd_hdg = rnd.details.get("hdg_change_deg", 0)
                if rnd_hdg > prev_hdg:
                    filtered_roundings[-1] = rnd
        deduped_roundings = filtered_roundings

    # Annotate with TWS bin where available
    all_maneuvers: list[Maneuver] = []
    for m in tacks + gybes + deduped_roundings:
        ts_key = m.ts.isoformat()[:19]
        tws_val = tws_series.get(ts_key)
        if tws_val is not None:
            import math

            tws_bin = max(0, int(math.floor(tws_val)))
        else:
            tws_bin = None
        all_maneuvers.append(
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

    # Sort by timestamp
    all_maneuvers.sort(key=lambda m: m.ts)

    await storage.write_maneuvers(session_id, all_maneuvers)
    logger.info(
        "detect_maneuvers: session {} → {} tacks, {} gybes, {} roundings",
        session_id,
        len(tacks),
        len(gybes),
        len(deduped_roundings),
    )
    return all_maneuvers
