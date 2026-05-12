"""Per-maneuver metric enrichment and ranking.

Given a detected maneuver (from ``maneuver_detector``) and slices of the
instrument timeseries around it, compute the entry/exit state, turn
geometry, and distance lost relative to an idealized instant-turn reference.

Distance loss model: forward progress along the entry COG vector. The
idealized "instant turn" boat continues at the entry SOG along the entry
heading for the full maneuver duration; the actual forward progress is the
projection of (exit_pos − entry_pos) onto that unit vector. Positive
``distance_loss_m`` means the boat gave up ground relative to that
reference — the simplest useful proxy for tacking loss, iterable later.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helmlog.storage import Storage

# Windows (seconds) used to sample steady-state entry / exit conditions.
# _SKIP skips the transition right at the maneuver boundary.
_ENTRY_WINDOW_S = 15
_EXIT_WINDOW_S = 15
_SKIP_S = 3

_KTS_TO_MS = 0.514444
_EARTH_R_M = 6371000.0


@dataclass(frozen=True)
class ManeuverMetrics:
    """Enriched metrics for a single maneuver."""

    entry_ts: datetime
    exit_ts: datetime | None
    duration_sec: float | None
    entry_bsp: float | None
    exit_bsp: float | None
    entry_hdg: float | None
    exit_hdg: float | None
    entry_twa: float | None
    exit_twa: float | None
    entry_tws: float | None
    exit_tws: float | None
    entry_sog: float | None
    min_bsp: float | None
    turn_angle_deg: float | None
    turn_rate_deg_s: float | None
    distance_loss_m: float | None
    time_to_head_to_wind_s: float | None
    time_to_recover_s: float | None
    head_to_wind_ts: datetime | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["entry_ts"] = self.entry_ts.isoformat() if self.entry_ts else None
        d["exit_ts"] = self.exit_ts.isoformat() if self.exit_ts else None
        d["head_to_wind_ts"] = self.head_to_wind_ts.isoformat() if self.head_to_wind_ts else None
        return d


def _mean_in_range(
    series: list[tuple[datetime, float]], start: datetime, end: datetime
) -> float | None:
    if not series:
        return None
    vals = [v for ts, v in series if start <= ts < end]
    if not vals:
        return None
    return statistics.fmean(vals)


def _median_in_range(
    series: list[tuple[datetime, float]], start: datetime, end: datetime
) -> float | None:
    """Median of scalar values in [start, end) — robust to a low-tail
    pre-tack speed bleed (#615). Used for entry-window aggregates."""
    if not series:
        return None
    vals = [v for ts, v in series if start <= ts < end]
    if not vals:
        return None
    return statistics.median(vals)


def _circular_mean_deg(
    series: list[tuple[datetime, float]], start: datetime, end: datetime
) -> float | None:
    """Mean of angular values in [start, end) in degrees, handling 0/360 wrap."""
    if not series:
        return None
    vals = [v for ts, v in series if start <= ts < end]
    if not vals:
        return None
    sx = sum(math.sin(math.radians(v)) for v in vals)
    sy = sum(math.cos(math.radians(v)) for v in vals)
    if sx == 0 and sy == 0:
        return None
    return (math.degrees(math.atan2(sx, sy)) + 360.0) % 360.0


def _signed_heading_delta(h1: float, h2: float) -> float:
    """Shortest-arc signed delta from h1 to h2 in (−180, 180]."""
    diff = (h2 - h1 + 360.0) % 360.0
    return diff if diff <= 180.0 else diff - 360.0


def _ll_to_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Equirectangular projection around (lat0, lon0) in metres."""
    lat0_rad = math.radians(lat0)
    x = _EARTH_R_M * math.radians(lon - lon0) * math.cos(lat0_rad)
    y = _EARTH_R_M * math.radians(lat - lat0)
    return x, y


def _position_at(
    positions: list[tuple[datetime, float, float]], target: datetime
) -> tuple[float, float] | None:
    """Return the position nearest to ``target`` (by absolute time delta)."""
    if not positions:
        return None
    best: tuple[float, float, float] | None = None  # (delta, lat, lon)
    for ts, lat, lon in positions:
        delta = abs((ts - target).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, lat, lon)
    if best is None:
        return None
    return best[1], best[2]


def _average_cog_between(
    positions: list[tuple[datetime, float, float]],
    start: datetime,
    end: datetime,
) -> float | None:
    """Bearing (degrees true) of the vector from the first to last sample in range."""
    pts = [(ts, lat, lon) for ts, lat, lon in positions if start <= ts <= end]
    if len(pts) < 2:
        return None
    _, lat0, lon0 = pts[0]
    _, lat1, lon1 = pts[-1]
    x, y = _ll_to_xy(lat0, lon0, lat1, lon1)
    if x == 0 and y == 0:
        return None
    bearing_rad = math.atan2(x, y)
    return (math.degrees(bearing_rad) + 360.0) % 360.0


_HTW_POST_WINDOW_S = 30  # used when exit_ts is unknown
_HTW_MIN_SAMPLES = 3


def _find_head_to_wind(
    maneuver_type: str | None,
    signed_twa: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
) -> datetime | None:
    """Return the head-to-wind timestamp for a maneuver, or None.

    For ``tack``: first signed-TWA zero crossing in ``[start, end]``; if
    no strict crossing is present (helm stalled), the sample with
    minimum ``|signed_twa|``.

    For ``gybe``: first ±180° crossing (detected as a wraparound between
    two opposite-signed samples whose magnitudes sum > 180°); if no
    wraparound, the sample with maximum ``|signed_twa|``.

    For any other type (``rounding``, ``maneuver``, None), return None.
    Returns None when the window has fewer than ``_HTW_MIN_SAMPLES``.
    """
    if maneuver_type not in ("tack", "gybe"):
        return None
    window = [(ts, v) for ts, v in signed_twa if start <= ts <= end]
    if len(window) < _HTW_MIN_SAMPLES:
        return None

    if maneuver_type == "tack":
        # First sign-flip between consecutive samples both within a
        # plausible close-hauled range (<90°).
        for i in range(len(window) - 1):
            ts_a, va = window[i]
            ts_b, vb = window[i + 1]
            if va * vb < 0 and abs(va) < 90.0 and abs(vb) < 90.0:
                return ts_a if abs(va) <= abs(vb) else ts_b
        # Stall: nearest-to-zero sample.
        return min(window, key=lambda p: abs(p[1]))[0]

    # Gybe: first ±180 wraparound between two opposite-signed samples
    # whose combined magnitudes exceed 180° (i.e., both are beyond
    # abeam and on opposite sides of the wind behind the boat).
    for i in range(len(window) - 1):
        ts_a, va = window[i]
        ts_b, vb = window[i + 1]
        if va * vb < 0 and (abs(va) + abs(vb)) > 180.0:
            return ts_a if abs(va) >= abs(vb) else ts_b
    # No wraparound captured in samples: fall back to the sample closest
    # to ±180° (equivalently, folded TWA maximum).
    return max(window, key=lambda p: abs(p[1]))[0]


def _entry_sog(
    positions: list[tuple[datetime, float, float]],
    start: datetime,
    end: datetime,
) -> float | None:
    """SOG (knots) estimated from the distance travelled between start and end positions."""
    pts = [(ts, lat, lon) for ts, lat, lon in positions if start <= ts <= end]
    if len(pts) < 2:
        return None
    ts0, lat0, lon0 = pts[0]
    ts1, lat1, lon1 = pts[-1]
    x, y = _ll_to_xy(lat0, lon0, lat1, lon1)
    dist = math.hypot(x, y)
    dt = (ts1 - ts0).total_seconds()
    if dt <= 0:
        return None
    return (dist / dt) / _KTS_TO_MS


def enrich_maneuver(
    *,
    maneuver_ts: datetime,
    exit_ts: datetime | None,
    hdg: list[tuple[datetime, float]],
    bsp: list[tuple[datetime, float]],
    twa: list[tuple[datetime, float]],
    tws: list[tuple[datetime, float]],
    positions: list[tuple[datetime, float, float]],
    signed_twa: list[tuple[datetime, float]] | None = None,
    maneuver_type: str | None = None,
    twd: list[tuple[datetime, float]] | None = None,
) -> ManeuverMetrics:
    """Compute entry/exit metrics, turn geometry, and distance loss.

    Inputs are sorted ``(datetime, value)`` pairs covering at least the
    entry pre-window through exit post-window. Positions are
    ``(datetime, lat_deg, lon_deg)``. Any missing series yields ``None`` for
    the fields it drives — the function never raises on missing data.
    """
    entry_end = maneuver_ts - timedelta(seconds=_SKIP_S)
    entry_start = entry_end - timedelta(seconds=_ENTRY_WINDOW_S)

    fallback_exit = exit_ts or (maneuver_ts + timedelta(seconds=_EXIT_WINDOW_S))
    exit_start = fallback_exit + timedelta(seconds=_SKIP_S)
    exit_end = exit_start + timedelta(seconds=_EXIT_WINDOW_S)

    duration = (exit_ts - maneuver_ts).total_seconds() if exit_ts else None

    # Entry-window aggregates use median (#615) so a pre-tack speed
    # bleed in the last few seconds of the window doesn't drag the
    # baseline down. Exit-window stays mean — the recovery dynamic IS
    # the signal there. Heading uses circular mean for both since
    # angular-median is more complexity than the simple cases warrant.
    entry_bsp = _median_in_range(bsp, entry_start, entry_end)
    exit_bsp = _mean_in_range(bsp, exit_start, exit_end)
    entry_hdg = _mean_in_range(hdg, entry_start, entry_end)
    exit_hdg = _mean_in_range(hdg, exit_start, exit_end)
    entry_twa_raw = _median_in_range(twa, entry_start, entry_end)
    exit_twa_raw = _mean_in_range(twa, exit_start, exit_end)
    entry_tws = _median_in_range(tws, entry_start, entry_end)
    exit_tws = _mean_in_range(tws, exit_start, exit_end)

    # Fold TWA to [0, 180] for readability.
    def _fold(v: float | None) -> float | None:
        if v is None:
            return None
        a = abs(v) % 360.0
        return a if a <= 180.0 else 360.0 - a

    entry_twa = _fold(entry_twa_raw)
    exit_twa = _fold(exit_twa_raw)

    # min_bsp during the maneuver window.
    min_bsp: float | None = None
    if bsp:
        window_vals = [v for ts, v in bsp if maneuver_ts <= ts <= fallback_exit]
        if window_vals:
            min_bsp = min(window_vals)

    # Turn geometry. Fall back to COG from positions if HDG unavailable.
    entry_bearing = entry_hdg
    exit_bearing = exit_hdg
    if entry_bearing is None:
        entry_bearing = _average_cog_between(positions, entry_start, entry_end)
    if exit_bearing is None:
        exit_bearing = _average_cog_between(positions, exit_start, exit_end)

    turn_angle: float | None = None
    turn_rate: float | None = None
    if entry_bearing is not None and exit_bearing is not None:
        turn_angle = _signed_heading_delta(entry_bearing, exit_bearing)
        if duration and duration > 0:
            turn_rate = abs(turn_angle) / duration

    # Ladder loss — the single, mark-directed loss metric. Ideal: what
    # a boat holding entry VMG for the full duration would make along
    # the mark axis (+wind for tacks, −wind for gybes). Actual: the
    # component of (exit_pos − entry_pos) along that same mark axis.
    # Positive = ground lost toward the mark; negative = ground gained
    # (rare — current push, favourable shift).
    distance_loss: float | None = None
    entry_sog = _entry_sog(positions, entry_start, entry_end)
    entry_pos = _position_at(positions, maneuver_ts)
    exit_pos = _position_at(positions, fallback_exit)
    mean_twd = _circular_mean_deg(twd or [], entry_start, entry_end)
    if (
        entry_sog is not None
        and entry_pos is not None
        and exit_pos is not None
        and duration is not None
        and duration > 0
        and entry_twa is not None
        and mean_twd is not None
    ):
        travelled = entry_sog * _KTS_TO_MS * duration
        # Folded entry_twa in [0, 180]. cos is positive for upwind,
        # negative for downwind; |cos| is the VMG fraction.
        twa_rad = math.radians(entry_twa)
        ideal_toward_mark = travelled * abs(math.cos(twa_rad))

        # Mark-direction unit vector in (east, north). TWD is the
        # direction the wind is blowing FROM — same as the upwind mark
        # bearing. For gybes the mark is downwind, so negate.
        mark_sign = 1.0 if entry_twa < 90.0 else -1.0
        twd_rad = math.radians(mean_twd)
        mark_ux = mark_sign * math.sin(twd_rad)
        mark_uy = mark_sign * math.cos(twd_rad)

        lat0, lon0 = entry_pos
        ex_x, ex_y = _ll_to_xy(lat0, lon0, exit_pos[0], exit_pos[1])
        actual_toward_mark = ex_x * mark_ux + ex_y * mark_uy
        distance_loss = ideal_toward_mark - actual_toward_mark

    # Head-to-wind timestamp (#613). Search window is [ts, exit_ts or ts+30s]
    # so slow tacks that don't fully recover still get a HTW.
    htw_end = exit_ts or (maneuver_ts + timedelta(seconds=_HTW_POST_WINDOW_S))
    head_to_wind_ts = _find_head_to_wind(
        maneuver_type,
        signed_twa or [],
        maneuver_ts,
        htw_end,
    )

    # Phase split (#614). Turn phase = entry → HTW; recovery phase = HTW
    # → exit. Diagnostic value: a slow turn / fast recovery is a helm
    # problem; a fast turn / slow recovery is a trim problem.
    time_to_head_to_wind: float | None = None
    time_to_recover: float | None = None
    if head_to_wind_ts is not None:
        time_to_head_to_wind = (head_to_wind_ts - maneuver_ts).total_seconds()
        if exit_ts is not None:
            time_to_recover = (exit_ts - head_to_wind_ts).total_seconds()

    return ManeuverMetrics(
        entry_ts=maneuver_ts,
        exit_ts=exit_ts,
        duration_sec=duration,
        entry_bsp=round(entry_bsp, 3) if entry_bsp is not None else None,
        exit_bsp=round(exit_bsp, 3) if exit_bsp is not None else None,
        entry_hdg=round(entry_hdg, 1) if entry_hdg is not None else None,
        exit_hdg=round(exit_hdg, 1) if exit_hdg is not None else None,
        entry_twa=round(entry_twa, 1) if entry_twa is not None else None,
        exit_twa=round(exit_twa, 1) if exit_twa is not None else None,
        entry_tws=round(entry_tws, 2) if entry_tws is not None else None,
        exit_tws=round(exit_tws, 2) if exit_tws is not None else None,
        entry_sog=round(entry_sog, 2) if entry_sog is not None else None,
        min_bsp=round(min_bsp, 3) if min_bsp is not None else None,
        turn_angle_deg=round(turn_angle, 1) if turn_angle is not None else None,
        turn_rate_deg_s=round(turn_rate, 2) if turn_rate is not None else None,
        distance_loss_m=round(distance_loss, 2) if distance_loss is not None else None,
        time_to_head_to_wind_s=(
            round(time_to_head_to_wind, 1) if time_to_head_to_wind is not None else None
        ),
        time_to_recover_s=round(time_to_recover, 1) if time_to_recover is not None else None,
        head_to_wind_ts=head_to_wind_ts,
    )


# Spread-aware ranking thresholds (#616). When a session's loss spread
# is too small to mean anything we label every maneuver ``consistent``
# instead of manufacturing a good/bad distinction.
#
# - Relative: stdev / median ≥ 0.5 keeps the spread meaningful as a
#   fraction of typical loss. In tight one-design fleets, mean tack
#   loss varies session-to-session by ~3–6 m; half-median captures
#   that without per-class calibration.
# - Absolute floor: total range ≥ 3 m. Below that, we're inside the
#   GPS positional-noise floor of the loss calc (SK multiplexed
#   antennas bucket to ~2–3 m — see ``_bucket_positions_per_second``).
# Both thresholds must pass for a quartile split to fire. If either
# fails, every rankable item is labeled ``consistent``.
_CONSISTENT_STDEV_FRAC = 0.5
_CONSISTENT_ABS_SPREAD_M = 3.0


def rank_maneuvers(
    items: list[dict[str, Any]],
    *,
    metric: str | None = None,
) -> list[dict[str, Any]]:
    """Attach a ``rank`` label and ``loss_percentile`` in-place (#616).

    Ranking is by ``metric`` when supplied, else ``distance_loss_m`` falling
    back to ``loss_kts``. When the set's loss spread is too small to be
    meaningful (low relative stdev OR — for ``distance_loss_m`` only — small
    absolute range), every rankable item gets rank ``consistent`` instead
    of the quartile split. Otherwise top quartile (lowest value) → ``good``,
    bottom quartile → ``bad``, middle half → ``avg``. Entries with no value
    for the chosen metric get rank ``None``.

    ``loss_percentile`` is the 0–100 within-set rank (lowest value = 0)
    on every rankable item, regardless of bucket. The UI renders it as a
    tooltip number so a user can see the underlying ordering even when the
    bucket label is ``consistent``.
    """
    if not items:
        return items

    def _key(m: dict[str, Any]) -> float | None:
        if metric is not None:
            v = m.get(metric)
            return float(v) if v is not None else None
        v = m.get("distance_loss_m")
        if v is not None:
            return float(v)
        v = m.get("loss_kts")
        return float(v) if v is not None else None

    ranked = [m for m in items if _key(m) is not None]
    unranked = [m for m in items if _key(m) is None]
    if not ranked:
        for m in items:
            m["rank"] = None
        return items

    sorted_items = sorted(ranked, key=lambda m: _key(m) or 0.0)
    n = len(sorted_items)

    # Loss percentile per rankable item: 0 for the lowest loss, scaled
    # to 100 across the set. Single-item sets get 0 to avoid div/0.
    for i, m in enumerate(sorted_items):
        m["loss_percentile"] = int(round(100 * i / n)) if n > 1 else 0

    losses = [_key(m) or 0.0 for m in sorted_items]
    spread = losses[-1] - losses[0]
    median_loss = statistics.median(losses)
    stdev_loss = statistics.pstdev(losses) if n > 1 else 0.0
    relative_passes = median_loss > 0 and (stdev_loss / median_loss) >= _CONSISTENT_STDEV_FRAC
    # The 3 m floor is unit-specific (GPS positional noise on
    # ``distance_loss_m``). For any other metric (knots, seconds) it would
    # be meaningless, so only the relative test gates those rows.
    using_distance = metric is None and all(
        m.get("distance_loss_m") is not None for m in sorted_items
    )
    absolute_passes = (not using_distance) or spread >= _CONSISTENT_ABS_SPREAD_M

    if not (relative_passes and absolute_passes):
        for m in ranked:
            m["rank"] = "consistent"
        for m in unranked:
            m["rank"] = None
            m.setdefault("loss_percentile", None)
        return items

    q1 = max(1, n // 4)
    q3 = max(q1, n - n // 4)
    good = {id(m) for m in sorted_items[:q1]}
    bad = {id(m) for m in sorted_items[q3:]}
    for m in ranked:
        if id(m) in good:
            m["rank"] = "good"
        elif id(m) in bad:
            m["rank"] = "bad"
        else:
            m["rank"] = "avg"
    for m in unranked:
        m["rank"] = None
        m.setdefault("loss_percentile", None)
    return items


# ---------------------------------------------------------------------------
# Storage integration
# ---------------------------------------------------------------------------


_ENRICH_PAD_S = 60  # seconds of instrument data to load beyond the session window

# Bump this when the shape of the enriched maneuver payload changes (new
# fields, recomputed ranks, changed ghost/track math). All cached payloads
# with a different code_version are treated as a cache miss and rebuilt.
# v3: adds head_to_wind_ts per maneuver (#613).
# v4: adds time_to_head_to_wind_s + redefines time_to_recover_s as the
#     real recovery phase (HTW → exit), not a duration alias (#614).
# v5: entry-window aggregates use median, not mean (#615). entry_bsp,
#     entry_twa, entry_tws (and downstream distance_loss_m) shift.
# v6: spread-aware ranking with new ``consistent`` label and numeric
#     ``loss_percentile`` (#616).
# v7: distance_loss_m switches from entry-COG projection to the single
#     ladder-axis projection (upwind for tacks, downwind for gybes).
#     Positive = ground lost toward the mark. (#619 follow-up.)
# v8: per-maneuver overlay arrays (overlay_bsp, overlay_heading_rate_deg_s,
#     overlay_twa) are precomputed at enrichment time so the cross-session
#     overlay endpoint serves from cache instead of rescanning instrument
#     tables for every request. Untagged sessions with multi-hour windows
#     made the previous reload path take 20+ s on a Pi.
ENRICH_CACHE_VERSION = 8


def _bucket_positions_per_second(
    positions: list[tuple[datetime, float, float]],
) -> list[tuple[datetime, float, float]]:
    """Collapse a list of (ts, lat, lon) fixes to one averaged fix per second.

    Signal K multiplexes two physical GPS antennas into the same stream, so
    raw position rows zig-zag between antennas ~3m apart. Bucketing to whole
    seconds and averaging lat/lon within each bucket collapses the zig-zag
    into a smooth mid-line — the same fix applied to ``/api/sessions/{id}/
    track`` in #516. Vakaros positions are already 1Hz from a single GPS,
    but running them through the same pipeline is idempotent and keeps the
    downstream shape uniform.
    """
    if not positions:
        return []
    buckets: dict[datetime, list[tuple[float, float]]] = {}
    order: list[datetime] = []
    for ts, lat, lon in positions:
        key = ts.replace(microsecond=0)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = [(lat, lon)]
            order.append(key)
        else:
            bucket.append((lat, lon))
    out: list[tuple[datetime, float, float]] = []
    for key in order:
        rows = buckets[key]
        avg_lat = sum(p[0] for p in rows) / len(rows)
        avg_lon = sum(p[1] for p in rows) / len(rows)
        out.append((key, avg_lat, avg_lon))
    return out


_TRACK_PRE_S = 20  # seconds of track before maneuver_ts
_TRACK_POST_S = 30  # seconds of track after exit_ts (or maneuver_ts if exit unknown)
# Normal tacks rotate the bow ~80–100°, gybes ~50–70°. Anything well above
# that is almost always a mark rounding — including "Mexican" roundings
# where pre/post TWA mode doesn't change. Threshold chosen at 130° so a
# clean tack stays a tack.
_ROUNDING_TURN_THRESHOLD_DEG = 130.0


def extract_local_track(
    *,
    maneuver_ts: datetime,
    exit_ts: datetime | None,
    entry_bearing_deg: float | None,
    positions: list[tuple[datetime, float, float]],
    bsp: list[tuple[datetime, float]],
    pre_s: int = _TRACK_PRE_S,
    post_s: int = _TRACK_POST_S,
) -> list[dict[str, float]]:
    """Return the boat track around the maneuver in a local entry-aligned frame.

    Points are translated so the maneuver-start position is at the origin,
    then rotated so the entry bearing points along +y (North up = entry
    direction). ``t`` is seconds relative to ``maneuver_ts``. If
    ``entry_bearing`` is None the track is returned in an east/north frame
    (still centered on entry). BSP is looked up by nearest-second for an
    optional colour channel in the UI.
    """
    if not positions:
        return []
    end_anchor = exit_ts or maneuver_ts
    win_start = maneuver_ts - timedelta(seconds=pre_s)
    win_end = end_anchor + timedelta(seconds=post_s)
    window = [(ts, lat, lon) for ts, lat, lon in positions if win_start <= ts <= win_end]
    if len(window) < 2:
        return []

    entry_pos = _position_at(positions, maneuver_ts)
    if entry_pos is None:
        return []
    lat0, lon0 = entry_pos

    if entry_bearing_deg is not None:
        br_rad = math.radians(entry_bearing_deg)
        cos_b = math.cos(br_rad)
        sin_b = math.sin(br_rad)
    else:
        cos_b, sin_b = 1.0, 0.0

    bsp_by_sec: dict[str, float] = {}
    for ts_b, bv in bsp:
        bsp_by_sec.setdefault(ts_b.isoformat()[:19], bv)

    out: list[dict[str, float]] = []
    for ts, lat, lon in window:
        # East (x) / North (y) in metres from entry position.
        ex, ny = _ll_to_xy(lat0, lon0, lat, lon)
        # Rotate so entry bearing → +y. Bearing is measured clockwise from
        # north, so the rotation from (E, N) into (cross, forward) is:
        #   forward = N*cos(b) + E*sin(b)
        #   cross   = E*cos(b) − N*sin(b)
        forward = ny * cos_b + ex * sin_b
        cross = ex * cos_b - ny * sin_b
        t_rel = (ts - maneuver_ts).total_seconds()
        bv_opt = bsp_by_sec.get(ts.isoformat()[:19])
        pt: dict[str, float] = {
            "t": round(t_rel, 1),
            "x": round(cross, 2),
            "y": round(forward, 2),
        }
        if bv_opt is not None:
            pt["bsp"] = round(bv_opt, 2)
        out.append(pt)
    return out


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace(" ", "T")).replace(tzinfo=UTC)


async def enrich_session_maneuvers(
    storage: Storage, session_id: int
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Load stored maneuvers for a session and attach analysis metrics + rank.

    Returns ``(maneuvers, video_sync)`` where ``video_sync`` is the first
    race video's ``{video_id, sync_utc, sync_offset_s, duration_s}`` or
    ``None`` if no video is linked. Maneuvers are returned as JSON-ready
    dicts with all metric fields rounded, plus ``lat``/``lon`` and — when
    a video is available — ``youtube_url``.

    The enriched payload is cached in ``maneuver_cache`` keyed on
    ``session_id``; the cache is invalidated when maneuvers are re-detected
    or the linked race video changes, and force-rebuilt whenever
    :data:`ENRICH_CACHE_VERSION` is bumped.
    """
    cached = await storage.get_cached_enriched_maneuvers(session_id, ENRICH_CACHE_VERSION)
    if cached is not None:
        return cached.get("maneuvers", []), cached.get("video_sync")

    rows = await storage.get_session_maneuvers(session_id)
    if not rows:
        return [], None

    db = storage._conn()
    race_cur = await db.execute(
        "SELECT start_utc, end_utc, vakaros_session_id FROM races WHERE id = ?",
        (session_id,),
    )
    race_row = await race_cur.fetchone()
    if race_row is None:
        return [], None

    start = _parse_iso(race_row["start_utc"])
    end = _parse_iso(race_row["end_utc"]) if race_row["end_utc"] else start + timedelta(hours=24)

    # Effective race start: if a matched Vakaros session logged a race_start
    # event inside or near the race window, prefer the *latest* one as the
    # authoritative gun time. Races with a general recall have the stored
    # start_utc pointing at the first attempt, while the actual gun is the
    # last race_start event — the reclassification filter and any downstream
    # "post-start" checks need that real gun time or pre-start practice
    # maneuvers leak through as post-start events.
    gun_cur = await db.execute(
        """
        SELECT vre.ts
        FROM races r
        JOIN vakaros_race_events vre ON vre.session_id = r.vakaros_session_id
        WHERE r.id = ?
          AND vre.event_type = 'race_start'
          AND vre.ts BETWEEN ? AND ?
        ORDER BY vre.ts DESC
        LIMIT 1
        """,
        (
            session_id,
            (start - timedelta(seconds=60)).isoformat(),
            (end + timedelta(seconds=60)).isoformat(),
        ),
    )
    gun_row = await gun_cur.fetchone()
    effective_start = _parse_iso(str(gun_row["ts"])) if gun_row is not None else start
    start_pad = start - timedelta(seconds=_ENRICH_PAD_S)
    end_pad = end + timedelta(seconds=_ENRICH_PAD_S)

    # Load all instrument series once, scoped to session where possible.
    async def _load(table: str) -> list[dict[str, Any]]:
        data = await storage.query_range(table, start_pad, end_pad, race_id=session_id)
        if not data:
            data = await storage.query_range(table, start_pad, end_pad)
        return data

    headings_raw = await _load("headings")
    speeds_raw = await _load("speeds")
    winds_raw = await _load("winds")
    positions_raw = await _load("positions")
    cogsog_raw = await _load("cogsog")

    def _ts_of(row: dict[str, Any]) -> datetime:
        return _parse_iso(str(row["ts"]))

    hdg: list[tuple[datetime, float]] = [(_ts_of(r), float(r["heading_deg"])) for r in headings_raw]
    if not hdg and cogsog_raw:
        hdg = [(_ts_of(r), float(r["cog_deg"])) for r in cogsog_raw]
    bsp: list[tuple[datetime, float]] = [(_ts_of(r), float(r["speed_kts"])) for r in speeds_raw]
    if not bsp and cogsog_raw:
        bsp = [(_ts_of(r), float(r["sog_kts"])) for r in cogsog_raw]

    # Build a heading lookup so we can convert north-referenced TWD to TWA.
    hdg_by_sec: dict[str, float] = {}
    for ts_h, hv in hdg:
        hdg_by_sec.setdefault(ts_h.isoformat()[:19], hv)

    twa: list[tuple[datetime, float]] = []
    # Pre-fold signed TWA in [-180, 180] (positive-starboard). Needed for
    # head-to-wind detection (#613): tacks cross zero, gybes wrap through
    # ±180°, and folding destroys the sign information.
    signed_twa_series: list[tuple[datetime, float]] = []
    tws: list[tuple[datetime, float]] = []
    # North-referenced true wind direction (TWD) per second. Used to
    # rotate per-maneuver tracks into a wind-up frame and to compute the
    # "climb the ladder" upwind-progress reference line.
    twd: list[tuple[datetime, float]] = []
    for r in winds_raw:
        ref_raw = r.get("reference")
        if ref_raw is None:
            continue
        try:
            ref = int(ref_raw)
        except (TypeError, ValueError):
            continue
        # 0 = boat-referenced TWA, 4 = north-referenced TWD. Both are "true wind".
        if ref not in (0, 4):
            continue
        ts = _ts_of(r)
        tws.append((ts, float(r["wind_speed_kts"])))
        if ref == 0:
            signed_twa = float(r["wind_angle_deg"])
            # Normalize to [-180, 180].
            signed_wrapped = ((signed_twa + 180.0) % 360.0) - 180.0
            signed_twa_series.append((ts, signed_wrapped))
            folded = abs(signed_twa) % 360.0
            twa.append((ts, folded if folded <= 180.0 else 360.0 - folded))
            # TWD = heading + signed TWA (wind_angle_deg is positive-starboard).
            hv_opt = hdg_by_sec.get(ts.isoformat()[:19])
            if hv_opt is not None:
                twd.append((ts, (hv_opt + signed_twa + 360.0) % 360.0))
        else:
            twd_val = float(r["wind_angle_deg"]) % 360.0
            twd.append((ts, twd_val))
            hv_opt = hdg_by_sec.get(ts.isoformat()[:19])
            if hv_opt is not None:
                raw = (twd_val - hv_opt + 360.0) % 360.0
                twa.append((ts, raw if raw <= 180.0 else 360.0 - raw))
                # Signed form: raw > 180 means wind on port (negative).
                signed_twa_series.append((ts, raw if raw <= 180.0 else raw - 360.0))

    positions_unbucketed: list[tuple[datetime, float, float]] = [
        (_ts_of(r), float(r["latitude_deg"]), float(r["longitude_deg"])) for r in positions_raw
    ]
    positions_unbucketed.sort(key=lambda p: p[0])
    # Smooth the SK GPS zig-zag caused by multiplexed antennas (#516).
    positions = _bucket_positions_per_second(positions_unbucketed)

    # Load Vakaros positions if this race is matched to a Vakaros session.
    # They're the same schema as SK positions once bucketed — used to render
    # an optional second trace on the per-maneuver overlay.
    vakaros_positions: list[tuple[datetime, float, float]] = []
    if race_row["vakaros_session_id"] is not None:
        vak_cur = await db.execute(
            "SELECT ts, latitude_deg, longitude_deg FROM vakaros_positions"
            " WHERE session_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (
                int(race_row["vakaros_session_id"]),
                start_pad.isoformat(),
                end_pad.isoformat(),
            ),
        )
        vak_rows = await vak_cur.fetchall()
        vakaros_positions = _bucket_positions_per_second(
            [
                (_parse_iso(str(r["ts"])), float(r["latitude_deg"]), float(r["longitude_deg"]))
                for r in vak_rows
            ]
        )

    hdg.sort(key=lambda p: p[0])
    bsp.sort(key=lambda p: p[0])
    twa.sort(key=lambda p: p[0])
    signed_twa_series.sort(key=lambda p: p[0])
    tws.sort(key=lambda p: p[0])
    twd.sort(key=lambda p: p[0])

    # Per-second lookup tables for the overlay alignment below. ``hdg_by_sec``
    # is already populated above (it's also used for TWD→TWA conversion).
    bsp_by_sec: dict[str, float] = {}
    for ts_b, bv in bsp:
        bsp_by_sec.setdefault(ts_b.isoformat()[:19], bv)
    twa_by_sec: dict[str, float] = {}
    for ts_t, tv in twa:
        twa_by_sec.setdefault(ts_t.isoformat()[:19], tv)

    # Video sync for deep-links. Pick the first race video.
    video_cur = await db.execute(
        "SELECT video_id, sync_utc, sync_offset_s, duration_s, youtube_url"
        " FROM race_videos WHERE race_id = ? ORDER BY id LIMIT 1",
        (session_id,),
    )
    video_row = await video_cur.fetchone()
    video_sync: dict[str, Any] | None = None
    if video_row is not None:
        video_sync = {
            "video_id": video_row["video_id"],
            "sync_utc": str(video_row["sync_utc"]),
            "sync_offset_s": float(video_row["sync_offset_s"] or 0.0),
            "duration_s": float(video_row["duration_s"] or 0.0),
            "youtube_url": video_row["youtube_url"],
        }
        video_sync_utc = _parse_iso(str(video_row["sync_utc"]))
    else:
        video_sync_utc = None

    # Build enriched output.
    enriched: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        raw_details = d.get("details")
        if isinstance(raw_details, str):
            try:
                d["details"] = json.loads(raw_details)
            except json.JSONDecodeError:
                d["details"] = {}

        m_ts = _parse_iso(str(d["ts"]))
        exit_ts = _parse_iso(str(d["end_ts"])) if d.get("end_ts") else None

        stored_type = str(d.get("type") or "")
        metrics = enrich_maneuver(
            maneuver_ts=m_ts,
            exit_ts=exit_ts,
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
            signed_twa=signed_twa_series,
            maneuver_type=stored_type,
            twd=twd,
        )
        md = metrics.to_dict()
        # Don't let entry_ts/exit_ts clobber the stored ts/end_ts fields.
        md.pop("entry_ts", None)
        md.pop("exit_ts", None)
        # Storage's duration_sec is already present; metrics duration matches it.
        md.pop("duration_sec", None)
        d.update(md)

        # Persist head-to-wind onto the maneuvers row so downstream callers
        # (phase-split metrics #614, overlay chart #619) can query the
        # column directly without going through the enrichment cache.
        if d.get("id") is not None:
            await storage.set_maneuver_head_to_wind_ts(
                int(d["id"]),
                metrics.head_to_wind_ts.isoformat() if metrics.head_to_wind_ts else None,
            )

        # Reclassify a wildly-large-turn gybe as a rounding. The detector
        # classifies by pre/post TWA mode, which misses "Mexican" roundings
        # where the boat stays downwind on both sides of a leeward mark but
        # the leg direction changes ~180°. A normal gybe swings the bow
        # 50–70°, so anything ≥ 130° is almost certainly a rounding.
        #
        # Only applied to maneuvers at or after the race start — pre-start
        # warmups commonly include big practice gybes and zig-zag drills
        # that aren't rounding anything, so reclassifying them as roundings
        # would flood the debrief view with false positives.
        #
        # NB: we do NOT upgrade large tacks — tacks legitimately swing
        # 80–100° already, and on a start-line approach or a big course
        # change an isolated tack can get up to ~135° without being a mark
        # rounding. The user explicitly flagged a 175° tack that was
        # mis-upgraded by an earlier heuristic.
        if (
            d.get("type") == "gybe"
            and metrics.turn_angle_deg is not None
            and abs(metrics.turn_angle_deg) >= _ROUNDING_TURN_THRESHOLD_DEG
            and m_ts >= effective_start
        ):
            if not isinstance(d.get("details"), dict):
                d["details"] = {}
            d["details"]["original_type"] = d["type"]
            d["type"] = "rounding"

        # Nearest position for the map marker.
        pos = _position_at(positions, m_ts)
        d["lat"] = pos[0] if pos else None
        d["lon"] = pos[1] if pos else None

        # Mean TWD around the maneuver (circular mean). The window spans
        # the whole pre/post diagnostic range so we get a stable wind axis
        # for rotating the overlay track into a wind-up frame.
        twd_window_start = m_ts - timedelta(seconds=_TRACK_PRE_S)
        twd_window_end = (exit_ts or m_ts) + timedelta(seconds=_TRACK_POST_S)
        mean_twd = _circular_mean_deg(twd, twd_window_start, twd_window_end)
        d["twd_deg"] = round(mean_twd, 1) if mean_twd is not None else None

        # Local track rotated so TWD → +y (upwind up). Falls back to
        # entry heading when TWD is unavailable.
        rot_bearing = mean_twd if mean_twd is not None else metrics.entry_hdg
        d["track"] = extract_local_track(
            maneuver_ts=m_ts,
            exit_ts=exit_ts,
            entry_bearing_deg=rot_bearing,
            positions=positions,
            bsp=bsp,
        )
        # Optional parallel track from the matched Vakaros session, rotated
        # into the same wind-up frame so it can be overlaid on top of the
        # SK-derived trace for comparison.
        if vakaros_positions:
            d["track_vakaros"] = extract_local_track(
                maneuver_ts=m_ts,
                exit_ts=exit_ts,
                entry_bearing_deg=rot_bearing,
                positions=vakaros_positions,
                bsp=bsp,
            )
        else:
            d["track_vakaros"] = None

        # "Climb the ladder" reference: distance the boat would have made
        # directly upwind if it had held VMG at entry SOG for the entire
        # maneuver duration, signed positive for upwind tacks and negative
        # for downwind gybes. With the wind-up rotation this becomes a
        # simple vertical line the UI can draw from (0,0) to (0,ghost).
        ghost: float | None = None
        if (
            metrics.duration_sec
            and metrics.duration_sec > 0
            and metrics.entry_sog is not None
            and metrics.entry_twa is not None
        ):
            travelled_m = metrics.entry_sog * 0.514444 * metrics.duration_sec
            twa_rad = math.radians(metrics.entry_twa)
            ghost_mag = travelled_m * math.cos(twa_rad)
            # Upwind (TWA < 90) → positive (toward +y == wind); downwind → negative.
            ghost = ghost_mag if metrics.entry_twa < 90.0 else -ghost_mag
        d["ghost_m"] = round(ghost, 2) if ghost is not None else None

        # Per-maneuver YouTube deep-link offset.
        if video_sync and video_sync_utc is not None:
            offset_s = video_sync["sync_offset_s"] + (m_ts - video_sync_utc).total_seconds()
            if 0 <= offset_s <= (video_sync["duration_s"] or offset_s + 1):
                d["video_offset_s"] = round(offset_s, 1)
                vid = video_sync["video_id"]
                d["youtube_url"] = f"https://www.youtube.com/watch?v={vid}&t={int(offset_s)}s"
            else:
                d["video_offset_s"] = None
                d["youtube_url"] = None
        else:
            d["video_offset_s"] = None
            d["youtube_url"] = None

        # Aligned per-maneuver overlay series (#619 follow-up). Pre-aligning
        # here means the cross-session overlay endpoint can serve straight
        # from the maneuver_cache row instead of re-scanning headings/speeds/
        # winds for every request. Aligned to ``head_to_wind_ts`` over the
        # fixed ``_OVERLAY_AXIS`` (-20..+30 s); maneuvers without an HTW
        # (roundings, stalled tacks) get None and are surfaced as excluded
        # by the overlay endpoint.
        htw_ts = metrics.head_to_wind_ts
        if htw_ts is not None:
            d["overlay_bsp"] = _align_series_at_htw(bsp_by_sec, htw_ts)
            d["overlay_heading_rate_deg_s"] = _heading_rate_series(hdg_by_sec, htw_ts)
            d["overlay_twa"] = _align_series_at_htw(twa_by_sec, htw_ts)
        else:
            d["overlay_bsp"] = None
            d["overlay_heading_rate_deg_s"] = None
            d["overlay_twa"] = None

        enriched.append(d)

    rank_maneuvers(enriched)
    await storage.put_cached_enriched_maneuvers(
        session_id,
        ENRICH_CACHE_VERSION,
        {"maneuvers": enriched, "video_sync": video_sync},
    )
    return enriched, video_sync


async def enrich_maneuvers_for_ids(
    storage: Storage, pairs: list[tuple[int, int]]
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any] | None]]:
    """Cross-session version of :func:`enrich_session_maneuvers` (#584).

    Takes a list of ``(session_id, maneuver_id)`` pairs and returns the
    enriched maneuver payloads across sessions, plus a mapping
    ``{session_id: video_sync}`` keyed by session. Each returned maneuver
    has ``session_id``, ``session_name``, ``session_slug``, and
    ``session_start_utc`` injected so the compare UI can label cells when
    mixing sessions.

    The underlying per-session enrichment is cached, so repeat calls for
    the same session only pay the enrichment cost once.
    """
    if not pairs:
        return [], {}

    by_session: dict[int, set[int]] = {}
    for sid, mid in pairs:
        by_session.setdefault(sid, set()).add(mid)

    out: list[dict[str, Any]] = []
    video_sync_by_session: dict[int, dict[str, Any] | None] = {}

    for session_id, wanted_ids in by_session.items():
        race = await storage.get_race(session_id)
        if race is None:
            continue
        enriched, video_sync = await enrich_session_maneuvers(storage, session_id)
        video_sync_by_session[session_id] = video_sync
        for m in enriched:
            if m.get("id") in wanted_ids:
                tagged = dict(m)
                tagged["session_id"] = session_id
                tagged["session_name"] = race.name
                tagged["session_slug"] = race.slug or ""
                tagged["session_start_utc"] = race.start_utc.isoformat() if race.start_utc else None
                out.append(tagged)

    return out, video_sync_by_session


# ---------------------------------------------------------------------------
# Multi-maneuver overlay series (#619)
# ---------------------------------------------------------------------------

# Window around head-to-wind (seconds). Covers the helm leading into the
# turn plus the recovery phase for most tacks; gybes generally fit too.
# Fixed length gives all selected maneuvers a common x-axis regardless of
# their individual durations.
_OVERLAY_PRE_S = 20
_OVERLAY_POST_S = 30
# Inclusive integer seconds from -PRE to +POST, one sample per second.
_OVERLAY_AXIS: list[int] = list(range(-_OVERLAY_PRE_S, _OVERLAY_POST_S + 1))


def _align_series_at_htw(series_by_sec: dict[str, float], htw_ts: datetime) -> list[float | None]:
    """Resample a per-second series to the overlay axis relative to HTW.

    ``series_by_sec`` is ``{iso_second: value}`` as built by the enrichment
    loader (``setdefault`` keyed on ``ts.isoformat()[:19]``). For each
    offset in the fixed overlay axis we compute the target absolute
    second and look it up; misses are ``None``. No interpolation — the
    detector already works on whole seconds so real data lands on the
    grid naturally.
    """
    out: list[float | None] = []
    for offset in _OVERLAY_AXIS:
        target = (htw_ts + timedelta(seconds=offset)).isoformat()[:19]
        v = series_by_sec.get(target)
        out.append(float(v) if v is not None else None)
    return out


def _heading_rate_series(hdg_by_sec: dict[str, float], htw_ts: datetime) -> list[float | None]:
    """First-difference of the heading series over the overlay window,
    handling angular wrap (e.g. 359 → 1 → rate = +2°/s, not -358)."""
    out: list[float | None] = []
    prev: float | None = None
    for offset in _OVERLAY_AXIS:
        target = (htw_ts + timedelta(seconds=offset)).isoformat()[:19]
        cur = hdg_by_sec.get(target)
        if cur is None or prev is None:
            out.append(None)
        else:
            out.append(round(_signed_heading_delta(prev, cur), 2))
        prev = cur if cur is not None else prev
    return out


async def build_maneuvers_overlay(storage: Storage, pairs: list[tuple[int, int]]) -> dict[str, Any]:
    """Build the overlay payload for ``GET /api/maneuvers/overlay``.

    Returns a dict shaped for the chart UI (#619):

    ``{
        "axis_s": [-20, -19, ..., 30],
        "channels": ["bsp", "heading_rate_deg_s", "twa"],
        "maneuvers": [
            {
                "session_id": 21,
                "maneuver_id": 392,
                "session_name": "...",
                "session_slug": "...",
                "type": "tack",
                "rank": "avg",
                "loss_percentile": 40,
                "head_to_wind_ts": "...",
                "bsp": [6.0, 6.0, ...],
                "heading_rate_deg_s": [0.1, ...],
                "twa": [40, ...],
            },
            ...
        ],
        "excluded_ids": ["21:393"],
    }``

    Maneuvers with ``head_to_wind_ts = None`` (roundings, stalled tacks,
    missing TWA) are excluded and their ``"<sid>:<mid>"`` string appended
    to ``excluded_ids`` so the UI can show a notice.

    The aligned per-maneuver series are precomputed at enrichment time and
    stored on each maneuver's cached payload (see
    :func:`enrich_session_maneuvers` and :data:`ENRICH_CACHE_VERSION`), so
    this function makes no fresh range-scans on the instrument tables.
    """
    if not pairs:
        return {"axis_s": _OVERLAY_AXIS, "channels": [], "maneuvers": [], "excluded_ids": []}

    by_session: dict[int, set[int]] = {}
    for sid, mid in pairs:
        by_session.setdefault(sid, set()).add(mid)

    out: list[dict[str, Any]] = []
    excluded: list[str] = []

    for session_id, wanted_ids in by_session.items():
        race = await storage.get_race(session_id)
        if race is None:
            for mid in wanted_ids:
                excluded.append(f"{session_id}:{mid}")
            continue

        enriched, _video = await enrich_session_maneuvers(storage, session_id)
        session_name = race.name
        session_slug = race.slug or ""

        for m in enriched:
            if m.get("id") not in wanted_ids:
                continue
            key = f"{session_id}:{m.get('id')}"
            htw_raw = m.get("head_to_wind_ts")
            if not htw_raw:
                excluded.append(key)
                continue
            bsp_aligned = m.get("overlay_bsp")
            hr_aligned = m.get("overlay_heading_rate_deg_s")
            twa_aligned = m.get("overlay_twa")
            if bsp_aligned is None or hr_aligned is None or twa_aligned is None:
                excluded.append(key)
                continue

            # One payload feeds three views on the overlay page (wind-up
            # track SVG, line charts, maneuver table) and the line-chart
            # panel on the session page. Fields are named explicitly
            # rather than splatting the enriched dict so the API
            # contract is discoverable from one place.
            out.append(
                {
                    "session_id": session_id,
                    "maneuver_id": m.get("id"),
                    "session_name": session_name,
                    "session_slug": session_slug,
                    "type": m.get("type"),
                    "rank": m.get("rank"),
                    "loss_percentile": m.get("loss_percentile"),
                    "head_to_wind_ts": m.get("head_to_wind_ts"),
                    # Precomputed aligned series for the line charts.
                    "bsp": bsp_aligned,
                    "heading_rate_deg_s": hr_aligned,
                    "twa": twa_aligned,
                    # Wind-up track + ghost reference for the overlay SVG.
                    "track": m.get("track"),
                    "track_vakaros": m.get("track_vakaros"),
                    "ghost_m": m.get("ghost_m"),
                    "twd_deg": m.get("twd_deg"),
                    # Metrics the table columns read.
                    "ts": m.get("ts"),
                    "end_ts": m.get("end_ts"),
                    "duration_sec": m.get("duration_sec"),
                    "turn_angle_deg": m.get("turn_angle_deg"),
                    "turn_rate_deg_s": m.get("turn_rate_deg_s"),
                    "distance_loss_m": m.get("distance_loss_m"),
                    "loss_kts": m.get("loss_kts"),
                    "entry_bsp": m.get("entry_bsp"),
                    "exit_bsp": m.get("exit_bsp"),
                    "min_bsp": m.get("min_bsp"),
                    "entry_twa": m.get("entry_twa"),
                    "exit_twa": m.get("exit_twa"),
                    "entry_tws": m.get("entry_tws"),
                    "exit_tws": m.get("exit_tws"),
                    "entry_sog": m.get("entry_sog"),
                    "time_to_head_to_wind_s": m.get("time_to_head_to_wind_s"),
                    "time_to_recover_s": m.get("time_to_recover_s"),
                    # Row-level extras used by the table (video link,
                    # lat/lon map marker, rounding mark classifier).
                    "youtube_url": m.get("youtube_url"),
                    "video_offset_s": m.get("video_offset_s"),
                    "lat": m.get("lat"),
                    "lon": m.get("lon"),
                    "mark": m.get("mark"),
                }
            )

    return {
        "axis_s": _OVERLAY_AXIS,
        "channels": ["bsp", "heading_rate_deg_s", "twa"],
        "maneuvers": out,
        "excluded_ids": excluded,
    }


# ---------------------------------------------------------------------------
# Eager backfill worker (#613)
# ---------------------------------------------------------------------------

# Checkpoint key — records the last session_id fully re-enriched, so a
# service restart mid-run resumes from the next session instead of
# recomputing from scratch.
_BACKFILL_CHECKPOINT_KEY = "maneuver_backfill_checkpoint"
# Seconds between sessions when sleep_s is not overridden. Keeps the
# event loop responsive while the web UI is serving requests.
_BACKFILL_YIELD_S = 0.05


async def backfill_stale_maneuver_cache(
    storage: Storage, *, yield_s: float = _BACKFILL_YIELD_S
) -> int:
    """Re-enrich every session whose cached maneuver payload is stale.

    Runs once at service start. Any session with stored maneuvers but a
    missing or older cache ``code_version`` is rebuilt oldest-first via
    :func:`enrich_session_maneuvers`, which also persists the new fields
    (e.g. ``head_to_wind_ts``) back onto the maneuvers row.

    The last processed ``session_id`` is checkpointed in ``app_settings``
    so a restart mid-run resumes from where it left off. The worker
    yields ``yield_s`` seconds between sessions to keep the web UI
    responsive while long backfills run. Returns the number of sessions
    processed in this call.
    """
    from loguru import logger

    stale = await storage.session_ids_with_stale_maneuver_cache(ENRICH_CACHE_VERSION)
    if not stale:
        logger.debug("maneuver backfill: nothing stale at code_version={}", ENRICH_CACHE_VERSION)
        return 0

    checkpoint_raw = await storage.get_setting(_BACKFILL_CHECKPOINT_KEY)
    try:
        checkpoint = int(checkpoint_raw) if checkpoint_raw else 0
    except ValueError:
        checkpoint = 0

    todo = [sid for sid in stale if sid > checkpoint]
    if not todo:
        # Checkpoint is ahead of everything still stale — either a prior
        # run completed, or we're resuming past the queue. Clear it.
        await storage.delete_setting(_BACKFILL_CHECKPOINT_KEY)
        todo = stale

    logger.info(
        "maneuver backfill: {} session(s) at code_version<{}", len(todo), ENRICH_CACHE_VERSION
    )
    processed = 0
    for sid in todo:
        try:
            await enrich_session_maneuvers(storage, sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("maneuver backfill: session {} failed: {}", sid, exc)
            continue
        processed += 1
        await storage.set_setting(_BACKFILL_CHECKPOINT_KEY, str(sid))
        logger.info("maneuver backfill: session {} done ({}/{})", sid, processed, len(todo))
        if yield_s > 0:
            await asyncio.sleep(yield_s)

    # Caught up — clear the checkpoint so the next bump starts from the
    # beginning of its own stale list.
    await storage.delete_setting(_BACKFILL_CHECKPOINT_KEY)
    logger.info("maneuver backfill: complete, {} session(s) processed", processed)
    return processed
