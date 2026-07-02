"""Polar performance baseline: build and query BSP vs (TWS, TWA) buckets.

Historical (TWS, TWA, BSP) triplets from completed race sessions are bucketed,
then mean and p90 BSP are stored per bin. Live BSP can then be compared against
this baseline to show whether the boat is over or under-performing.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TWA_BIN_SIZE = 5

# wind reference values that appear in the winds table
_WIND_REF_BOAT = 0  # wind_angle_deg IS the TWA (true wind, boat-referenced)
_WIND_REF_NORTH = 4  # wind_angle_deg is TWD; need heading to derive TWA

# Replay-grading config (#469). All tunable so we can adjust without a
# schema change. Segment width is read from env so deployments can tweak
# it without code changes.
POLAR_SEGMENT_SECONDS: int = int(os.environ.get("POLAR_SEGMENT_SECONDS", "10"))

# pct_target thresholds for the grade decision table.
GRADE_RED_BELOW: float = 0.90
GRADE_YELLOW_BELOW: float = 0.97
GRADE_GREEN_BELOW: float = 1.05  # [0.97, 1.05) → green
GRADE_SUSPICIOUS_AT: float = 1.20  # ≥ 1.20 → suspicious (likely bad baseline cell)

# Cache invalidation: bumped whenever the polar baseline is rebuilt.
POLAR_BASELINE_VERSION_KEY: str = "polar_baseline_version"

GradeLabel = Literal["green", "yellow", "red", "suspicious", "unknown"]

PointOfSail = Literal["upwind", "downwind"]
Tack = Literal["port", "starboard"]

# Upwind/downwind cutoff (#534). TWAs strictly below this are upwind.
UPWIND_CUTOFF_DEG: float = 90.0

# ---------------------------------------------------------------------------
# Pure helpers (all unit-testable without Storage)
# ---------------------------------------------------------------------------


def _tws_bin(tws_kts: float) -> int:
    """Return the integer TWS bin (floor of knots, min 0)."""
    return max(0, int(math.floor(tws_kts)))


def _twa_bin(twa_deg: float) -> int:
    """Return the TWA bin: fold to [0, 180) and floor to nearest _TWA_BIN_SIZE."""
    twa_abs = abs(twa_deg) % 360
    if twa_abs > 180:
        twa_abs = 360 - twa_abs
    return int(math.floor(twa_abs / _TWA_BIN_SIZE)) * _TWA_BIN_SIZE


def _compute_twa(
    wind_angle_deg: float,
    reference: int,
    heading_deg: float | None,
) -> float | None:
    """Derive TWA magnitude from a wind record.

    Returns the absolute TWA in [0, 180], or None if the reference is
    unsupported or the required heading is absent.
    """
    if reference == _WIND_REF_BOAT:
        return abs(wind_angle_deg) % 360
    if reference == _WIND_REF_NORTH:
        if heading_deg is None:
            return None
        twa_raw = (wind_angle_deg - heading_deg + 360) % 360
        return twa_raw if twa_raw <= 180 else 360 - twa_raw
    return None  # apparent wind or unknown reference


def _compute_twa_with_tack(
    wind_angle_deg: float,
    reference: int,
    heading_deg: float | None,
) -> tuple[float, Tack] | None:
    """Return (abs_twa, tack) or None.

    Positive signed TWA (wind from the starboard side) → starboard tack.
    """
    if reference == _WIND_REF_BOAT:
        signed = wind_angle_deg % 360
    elif reference == _WIND_REF_NORTH:
        if heading_deg is None:
            return None
        signed = (wind_angle_deg - heading_deg) % 360
    else:
        return None
    if signed > 180:
        signed -= 360  # now in (-180, 180]
    tack: Tack = "starboard" if signed >= 0 else "port"
    return abs(signed), tack


def _point_of_sail(abs_twa_deg: float) -> PointOfSail:
    return "upwind" if abs_twa_deg < UPWIND_CUTOFF_DEG else "downwind"


# ---------------------------------------------------------------------------
# Race window: trim prestart + post-finish so polars only see racing data (#812)
# ---------------------------------------------------------------------------
#
# The polar diagram and the baseline must only see data from when we were
# actually racing. Prestart maneuvering and post-finish sailing otherwise bias
# both the per-race comparison and the shared baseline.
#
# Gun: the Vakaros ``race_start`` event when the race is Vakaros-matched, else
# ``start_utc`` (which the 5-4-1-0 start-timer FSM already anchors to the gun,
# #644, when the crew runs the timer).
#
# Finish: no device event is trusted here, so we detect it from the track. For
# a windward/leeward course the finish is an upwind finish by the committee
# boat; after crossing, the boat bears away and sails off. Walking backward
# from the session end, the finish is the last moment we were close-hauled in
# the vicinity of the start area. The finish line sits just past the RC, so the
# vicinity radius is deliberately generous.

# ≤ this absolute TWA (deg) counts as close-hauled / beating.
_FINISH_CLOSE_HAULED_MAX_ABS_TWA: float = 55.0
# "vicinity of the start" radius (m); generous so the finish just past the RC
# still counts.
_FINISH_VICINITY_RADIUS_M: float = 500.0
# Only trim when the post-finish tail is worth trimming.
_FINISH_MIN_TAIL_TRIM_S: float = 30.0
# Never collapse the racing window below this — guards against a finish detected
# too early (e.g. an upwind pass near the line mid-race).
_FINISH_MIN_RACE_DURATION_S: float = 120.0

GunSource = Literal["vakaros", "start_utc"]
FinishSource = Literal["heuristic", "end_utc"]


@dataclass(frozen=True)
class RaceWindow:
    """Effective racing window [gun, finish] plus the provenance of each edge."""

    gun: datetime
    finish: datetime
    gun_source: GunSource
    finish_source: FinishSource


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    r = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _detect_finish_from_track(
    gun: datetime,
    end: datetime,
    start_lat: float,
    start_lon: float,
    samples: list[tuple[datetime, float, float, float]],
) -> tuple[datetime, FinishSource]:
    """Backward-scan finish heuristic.

    ``samples`` is ``(ts, lat, lon, abs_twa)`` sorted ascending by ts, covering
    ``[gun, end]``. Walking backward, the finish is the latest sample where the
    boat was close-hauled *and* within the start vicinity — i.e. the last beat
    to the finish by the committee boat, before it bore away and sailed off.

    Returns ``(finish_ts, source)``. Falls back to ``(end, "end_utc")`` when no
    plausible finish is found or a guardrail rejects the trim (tail too short to
    bother, or the detected finish would collapse the racing window).
    """
    finish_ts: datetime | None = None
    for ts, lat, lon, abs_twa in reversed(samples):
        if ts <= gun or ts > end:
            continue
        if (
            abs_twa <= _FINISH_CLOSE_HAULED_MAX_ABS_TWA
            and _distance_m(lat, lon, start_lat, start_lon) <= _FINISH_VICINITY_RADIUS_M
        ):
            finish_ts = ts
            break
    if finish_ts is None:
        return end, "end_utc"
    if (end - finish_ts).total_seconds() < _FINISH_MIN_TAIL_TRIM_S:
        return end, "end_utc"
    if (finish_ts - gun).total_seconds() < _FINISH_MIN_RACE_DURATION_S:
        return end, "end_utc"
    return finish_ts, "heuristic"


async def race_window(
    storage: Storage,
    *,
    start_utc: str,
    end_utc: str,
    vakaros_session_id: int | None,
) -> RaceWindow:
    """Resolve the effective racing window [gun, finish] for one race.

    ``start_utc`` / ``end_utc`` are the race row's ISO timestamps; the caller is
    responsible for ensuring ``end_utc`` is non-null. Raises ``ValueError`` on
    unparseable timestamps (callers already guard those).
    """
    db = storage._conn()
    start = datetime.fromisoformat(str(start_utc)).replace(tzinfo=UTC)
    end = datetime.fromisoformat(str(end_utc)).replace(tzinfo=UTC)

    # --- Gun: prefer the Vakaros race_start event (#536) ---
    gun = start
    gun_source: GunSource = "start_utc"
    if vakaros_session_id is not None:
        gun_cur = await db.execute(
            "SELECT vre.ts FROM vakaros_race_events vre"
            " WHERE vre.session_id = ? AND vre.event_type = 'race_start'"
            "   AND vre.ts BETWEEN ? AND ?"
            " ORDER BY vre.ts DESC LIMIT 1",
            (vakaros_session_id, start_utc, end_utc),
        )
        gun_row = await gun_cur.fetchone()
        if gun_row is not None:
            with contextlib.suppress(ValueError):
                gun = datetime.fromisoformat(str(gun_row["ts"])).replace(tzinfo=UTC)
                gun_source = "vakaros"

    # --- Finish: track heuristic over [gun, end] ---
    # Defensive: a single race with odd data must never abort a baseline build,
    # so any failure falls back to end_utc (the pre-#812 behaviour).
    finish: datetime = end
    finish_source: FinishSource = "end_utc"
    try:
        finish, finish_source = await _detect_finish(storage, gun, end)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Polar: finish heuristic failed, using end_utc: {}", exc)
    return RaceWindow(gun=gun, finish=finish, gun_source=gun_source, finish_source=finish_source)


async def _detect_finish(
    storage: Storage, gun: datetime, end: datetime
) -> tuple[datetime, FinishSource]:
    """Fetch track + wind over [gun, end], build (ts, lat, lon, abs_twa) samples,
    and hand them to the pure ``_detect_finish_from_track`` heuristic."""
    positions = await storage.query_range("positions", gun, end)
    if not positions:
        return end, "end_utc"
    winds = await storage.query_range("winds", gun, end)
    headings = await storage.query_range("headings", gun, end)

    hdg_by_s: dict[str, dict[str, Any]] = {}
    for h in headings:
        hdg_by_s.setdefault(str(h["ts"])[:19], h)
    tw_by_s: dict[str, dict[str, Any]] = {}
    for w in winds:
        if int(w.get("reference", -1)) not in (_WIND_REF_BOAT, _WIND_REF_NORTH):
            continue
        tw_by_s.setdefault(str(w["ts"])[:19], w)

    # First fix at/after the gun marks the start area (positions are ts-ordered).
    start_lat = float(positions[0]["latitude_deg"])
    start_lon = float(positions[0]["longitude_deg"])

    samples: list[tuple[datetime, float, float, float]] = []
    for p in positions:
        wind_row = tw_by_s.get(str(p["ts"])[:19])
        if wind_row is None:
            continue
        hdg_row = hdg_by_s.get(str(p["ts"])[:19])
        heading = float(hdg_row["heading_deg"]) if hdg_row else None
        twa = _compute_twa(
            float(wind_row["wind_angle_deg"]), int(wind_row.get("reference", -1)), heading
        )
        if twa is None:
            continue
        ts = datetime.fromisoformat(str(p["ts"])).replace(tzinfo=UTC)
        samples.append((ts, float(p["latitude_deg"]), float(p["longitude_deg"]), twa))

    if not samples:
        return end, "end_utc"
    return _detect_finish_from_track(gun, end, start_lat, start_lon, samples)


# ---------------------------------------------------------------------------
# Baseline builder
# ---------------------------------------------------------------------------


async def build_polar_baseline(storage: Storage, min_sessions: int = 3) -> int:
    """Compute polar baseline from all completed race sessions and persist it.

    For each (tws_bin, twa_bin) cell, the baseline is only written when data
    from at least *min_sessions* distinct races contributed.

    Returns:
        Number of bins written to polar_baseline.
    """
    db = storage._conn()

    # 1. Fetch all completed races. vakaros_session_id is pulled so we can
    #    narrow the window to [race_gun, end] for Vakaros-matched races and
    #    exclude pre-start maneuvering from the baseline (#536).
    cur = await db.execute(
        "SELECT id, start_utc, end_utc, vakaros_session_id FROM races WHERE end_utc IS NOT NULL"
    )
    races = list(await cur.fetchall())
    if not races:
        logger.info("Polar: no completed races found; baseline not built")
        await storage.upsert_polar_baseline([], datetime.now(UTC).isoformat())
        current = await storage.get_setting(POLAR_BASELINE_VERSION_KEY)
        next_version = (int(current) if current and current.isdigit() else 0) + 1
        await storage.set_setting(POLAR_BASELINE_VERSION_KEY, str(next_version))
        return 0

    # bin_samples[(tws_bin, twa_bin)] = list of (race_id, bsp_kts)
    bin_samples: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)

    for race_row in races:
        race_id = int(race_row["id"])
        # Narrow to the effective racing window [gun, finish] so pre-start
        # maneuvering (#536) and post-finish sailing (#812) don't bias the
        # baseline. gun = Vakaros race_start / start_utc; finish = track
        # heuristic / end_utc.
        try:
            win = await race_window(
                storage,
                start_utc=str(race_row["start_utc"]),
                end_utc=str(race_row["end_utc"]),
                vakaros_session_id=race_row["vakaros_session_id"],
            )
        except ValueError:
            logger.warning("Polar: skipping race {} — bad timestamps", race_id)
            continue
        start, end = win.gun, win.finish

        speeds = await storage.query_range("speeds", start, end)
        winds = await storage.query_range("winds", start, end)
        headings = await storage.query_range("headings", start, end)

        # Index by truncated second key (first 19 chars of ISO string)
        spd_by_s: dict[str, dict[str, Any]] = {}
        for s in speeds:
            key = str(s["ts"])[:19]
            spd_by_s.setdefault(key, s)

        hdg_by_s: dict[str, dict[str, Any]] = {}
        for h in headings:
            key = str(h["ts"])[:19]
            hdg_by_s.setdefault(key, h)

        # Filter winds: only reference 0 (boat) and 4 (north); skip apparent (2)
        tw_by_s: dict[str, dict[str, Any]] = {}
        for w in winds:
            if int(w.get("reference", -1)) not in (_WIND_REF_BOAT, _WIND_REF_NORTH):
                continue
            key = str(w["ts"])[:19]
            tw_by_s.setdefault(key, w)

        for sk, spd_row in spd_by_s.items():
            wind_row = tw_by_s.get(sk)
            if wind_row is None:
                continue

            ref = int(wind_row.get("reference", -1))
            wind_angle = float(wind_row["wind_angle_deg"])
            tws_kts = float(wind_row["wind_speed_kts"])
            bsp_kts = float(spd_row["speed_kts"])

            hdg_row = hdg_by_s.get(sk)
            heading = float(hdg_row["heading_deg"]) if hdg_row else None

            twa = _compute_twa(wind_angle, ref, heading)
            if twa is None:
                continue

            tb = _tws_bin(tws_kts)
            ab = _twa_bin(twa)
            bin_samples[(tb, ab)].append((race_id, bsp_kts))

    # 2. Compute statistics per bin; enforce min_sessions
    rows_to_write: list[dict[str, Any]] = []
    for (tws_bin, twa_bin), samples in bin_samples.items():
        unique_races = {s[0] for s in samples}
        if len(unique_races) < min_sessions:
            continue
        bsp_values = sorted(s[1] for s in samples)
        n = len(bsp_values)
        mean_bsp = sum(bsp_values) / n
        p90_idx = max(0, math.ceil(0.9 * n) - 1)
        p90_bsp = bsp_values[p90_idx]
        rows_to_write.append(
            {
                "tws_bin": tws_bin,
                "twa_bin": twa_bin,
                "mean_bsp": round(mean_bsp, 4),
                "p90_bsp": round(p90_bsp, 4),
                "session_count": len(unique_races),
                "sample_count": n,
            }
        )

    built_at = datetime.now(UTC).isoformat()
    await storage.upsert_polar_baseline(rows_to_write, built_at)
    # Bump the baseline version so any cached per-segment grades become stale.
    current = await storage.get_setting(POLAR_BASELINE_VERSION_KEY)
    next_version = (int(current) if current and current.isdigit() else 0) + 1
    await storage.set_setting(POLAR_BASELINE_VERSION_KEY, str(next_version))
    logger.info(
        "Polar baseline built: {} bins from {} races (baseline_version={})",
        len(rows_to_write),
        len(races),
        next_version,
    )
    return len(rows_to_write)


async def get_polar_baseline_version(storage: Storage) -> int:
    """Return the current polar baseline version (0 if never built)."""
    raw = await storage.get_setting(POLAR_BASELINE_VERSION_KEY)
    return int(raw) if raw and raw.isdigit() else 0


# ---------------------------------------------------------------------------
# Live lookup
# ---------------------------------------------------------------------------


async def lookup_polar(
    storage: Storage,
    tws_kts: float,
    twa_deg: float,
    min_sessions: int = 3,
) -> dict[str, Any] | None:
    """Return the polar_baseline row for the given wind condition, or None.

    Returns None if no row exists or the row doesn't meet the *min_sessions*
    threshold (guards against sparse baseline data).
    """
    tb = _tws_bin(tws_kts)
    ab = _twa_bin(twa_deg)
    row = await storage.get_polar_point(tb, ab)
    if row is None:
        return None
    if int(row["session_count"]) < min_sessions:
        return None
    return row


# ---------------------------------------------------------------------------
# Session polar comparison
# ---------------------------------------------------------------------------


@dataclass
class PolarCell:
    """One (TWS, TWA, point-of-sail, tack) cell with baseline and session data.

    The baseline is symmetric across port/starboard (keyed on abs TWA), but
    the session cell carries its own point-of-sail and tack so the panel can
    show asymmetries without rebuilding the baseline (#534).
    """

    tws_bin: int
    twa_bin: int
    point_of_sail: PointOfSail
    tack: Tack
    baseline_mean_bsp: float | None
    baseline_p90_bsp: float | None
    session_mean_bsp: float | None
    session_sample_count: int
    delta: float | None


@dataclass
class SessionPolarData:
    """Full polar comparison for a session."""

    cells: list[PolarCell]
    tws_bins: list[int]
    twa_bins: list[int]
    session_sample_count: int
    # Effective racing window this comparison was computed over, plus how each
    # edge was resolved — so the UI can show the trim is honest (#812).
    window_start: datetime
    window_end: datetime
    gun_source: GunSource
    finish_source: FinishSource


async def session_polar_comparison(
    storage: Storage,
    session_id: int,
) -> SessionPolarData | None:
    """Compare a session's BSP performance against the polar baseline.

    Only data from the effective racing window [gun, finish] is used, so
    prestart maneuvering and post-finish sailing don't skew the comparison
    (#812).

    Returns None if the session doesn't exist or hasn't ended.
    Returns a SessionPolarData with empty cells if no instrument data is available.
    """
    db = storage._conn()

    cur = await db.execute(
        "SELECT start_utc, end_utc, vakaros_session_id FROM races WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    if row is None or row["end_utc"] is None:
        return None

    try:
        win = await race_window(
            storage,
            start_utc=str(row["start_utc"]),
            end_utc=str(row["end_utc"]),
            vakaros_session_id=row["vakaros_session_id"],
        )
    except ValueError:
        return None
    start, end = win.gun, win.finish

    # Load instrument data for this session
    speeds = await storage.query_range("speeds", start, end)
    winds = await storage.query_range("winds", start, end)
    headings = await storage.query_range("headings", start, end)

    spd_by_s: dict[str, dict[str, Any]] = {}
    for s in speeds:
        spd_by_s.setdefault(str(s["ts"])[:19], s)

    hdg_by_s: dict[str, dict[str, Any]] = {}
    for h in headings:
        hdg_by_s.setdefault(str(h["ts"])[:19], h)

    tw_by_s: dict[str, dict[str, Any]] = {}
    for w in winds:
        if int(w.get("reference", -1)) not in (_WIND_REF_BOAT, _WIND_REF_NORTH):
            continue
        tw_by_s.setdefault(str(w["ts"])[:19], w)

    # Bin session samples by (tws, twa, point_of_sail, tack)
    SplitKey = tuple[int, int, PointOfSail, Tack]
    bin_samples: dict[SplitKey, list[float]] = defaultdict(list)
    for sk, spd_row in spd_by_s.items():
        wind_row = tw_by_s.get(sk)
        if wind_row is None:
            continue

        ref = int(wind_row.get("reference", -1))
        wind_angle = float(wind_row["wind_angle_deg"])
        tws_kts = float(wind_row["wind_speed_kts"])
        bsp_kts = float(spd_row["speed_kts"])

        hdg_row = hdg_by_s.get(sk)
        heading = float(hdg_row["heading_deg"]) if hdg_row else None

        twa_tack = _compute_twa_with_tack(wind_angle, ref, heading)
        if twa_tack is None:
            continue
        twa, tack = twa_tack

        tb = _tws_bin(tws_kts)
        ab = _twa_bin(twa)
        pos = _point_of_sail(twa)
        bin_samples[(tb, ab, pos, tack)].append(bsp_kts)

    # Load full baseline (symmetric, no min_sessions gate — #534)
    baseline: dict[tuple[int, int], dict[str, Any]] = {}
    try:
        bcur = await db.execute("SELECT tws_bin, twa_bin, mean_bsp, p90_bsp FROM polar_baseline")
        for br in await bcur.fetchall():
            baseline[(int(br["tws_bin"]), int(br["twa_bin"]))] = dict(br)
    except Exception:
        pass  # no baseline table on un-migrated DB

    cells: list[PolarCell] = []
    total_samples = 0

    for key, samples in bin_samples.items():
        tws_b, twa_b, pos, tack = key
        bl = baseline.get((tws_b, twa_b))

        session_mean = round(sum(samples) / len(samples), 4)
        bl_mean = float(bl["mean_bsp"]) if bl else None
        bl_p90 = float(bl["p90_bsp"]) if bl else None
        delta = round(session_mean - bl_mean, 4) if bl_mean is not None else None

        n = len(samples)
        total_samples += n

        cells.append(
            PolarCell(
                tws_bin=tws_b,
                twa_bin=twa_b,
                point_of_sail=pos,
                tack=tack,
                baseline_mean_bsp=bl_mean,
                baseline_p90_bsp=bl_p90,
                session_mean_bsp=session_mean,
                session_sample_count=n,
                delta=delta,
            )
        )

    cells.sort(key=lambda c: (c.tws_bin, c.point_of_sail, c.tack, c.twa_bin))
    tws_bins = sorted({c.tws_bin for c in cells})
    twa_bins = sorted({c.twa_bin for c in cells})

    return SessionPolarData(
        cells=cells,
        tws_bins=tws_bins,
        twa_bins=twa_bins,
        session_sample_count=total_samples,
        window_start=win.gun,
        window_end=win.finish,
        gun_source=win.gun_source,
        finish_source=win.finish_source,
    )


# ---------------------------------------------------------------------------
# Per-segment grading for race replay (#469)
# ---------------------------------------------------------------------------


@dataclass
class GradedSegment:
    """One time-window's worth of polar grading along a session track."""

    segment_index: int
    t_start: datetime
    t_end: datetime
    lat: float | None
    lon: float | None
    tws_kts: float | None
    twa_deg: float | None
    bsp_kts: float | None
    target_bsp_kts: float | None
    pct_target: float | None
    delta_kts: float | None
    grade: GradeLabel

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["t_start"] = self.t_start.isoformat()
        d["t_end"] = self.t_end.isoformat()
        return d


def _grade_from_pct(pct: float | None) -> GradeLabel:
    """Map pct_target → grade per the decision table."""
    if pct is None:
        return "unknown"
    if pct >= GRADE_SUSPICIOUS_AT:
        return "suspicious"
    if pct < GRADE_RED_BELOW:
        return "red"
    if pct < GRADE_YELLOW_BELOW:
        return "yellow"
    return "green"  # [0.97, 1.20)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _interp_position(
    positions: list[dict[str, Any]], t_mid: datetime
) -> tuple[float | None, float | None]:
    """Return interpolated (lat, lon) at *t_mid*, or nearest fix within ±2s."""
    if not positions:
        return None, None
    # Find bracketing fixes
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    for p in positions:
        ts = datetime.fromisoformat(str(p["ts"])).replace(tzinfo=UTC)
        if ts <= t_mid:
            before = {**p, "_dt": ts}
        else:
            after = {**p, "_dt": ts}
            break
    if before is not None and after is not None:
        span = (after["_dt"] - before["_dt"]).total_seconds()
        if span <= 0:
            return float(before["latitude_deg"]), float(before["longitude_deg"])
        f = (t_mid - before["_dt"]).total_seconds() / span
        lat = float(before["latitude_deg"]) + f * (
            float(after["latitude_deg"]) - float(before["latitude_deg"])
        )
        lon = float(before["longitude_deg"]) + f * (
            float(after["longitude_deg"]) - float(before["longitude_deg"])
        )
        return lat, lon
    # Fall back to nearest fix within ±2s
    candidates = [
        (abs((datetime.fromisoformat(str(p["ts"])).replace(tzinfo=UTC) - t_mid).total_seconds()), p)
        for p in positions
    ]
    candidates.sort(key=lambda x: x[0])
    nearest_dt, nearest = candidates[0]
    if nearest_dt <= 2.0:
        return float(nearest["latitude_deg"]), float(nearest["longitude_deg"])
    return None, None


def _grade_segments_sync(
    start: datetime,
    end: datetime,
    width: int,
    speeds: list[dict[str, Any]],
    winds: list[dict[str, Any]],
    headings: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    polar_map: dict[tuple[int, int], dict[str, Any]],
    min_sessions: int = 3,
) -> tuple[list[GradedSegment], dict[str, int]]:
    """Pure-Python segmentation + grading. No I/O; safe to run in a worker thread.

    *polar_map* is the full (tws_bin, twa_bin) → baseline row mapping, pre-fetched
    so no ``await`` is needed inside the loop (#603).
    """

    def _ts_of(rec: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(str(rec["ts"])).replace(tzinfo=UTC)

    speeds_dt = [(_ts_of(r), r) for r in speeds]
    winds_dt = [
        (_ts_of(r), r)
        for r in winds
        if int(r.get("reference", -1)) in (_WIND_REF_BOAT, _WIND_REF_NORTH)
    ]
    hdg_dt = [(_ts_of(r), r) for r in headings]

    segments: list[GradedSegment] = []
    grade_hist: dict[str, int] = defaultdict(int)

    n_segments = math.ceil((end - start).total_seconds() / width)
    for idx in range(n_segments):
        seg_start = start + timedelta(seconds=idx * width)
        seg_end = min(end, seg_start + timedelta(seconds=width))
        t_mid = seg_start + (seg_end - seg_start) / 2

        spd_in = [float(r["speed_kts"]) for ts, r in speeds_dt if seg_start <= ts < seg_end]
        wind_in = [(ts, r) for ts, r in winds_dt if seg_start <= ts < seg_end]
        hdg_in = [float(r["heading_deg"]) for ts, r in hdg_dt if seg_start <= ts < seg_end]

        lat, lon = _interp_position(positions, t_mid) if positions else (None, None)

        bsp = _mean(spd_in)
        tws = _mean([float(r["wind_speed_kts"]) for _, r in wind_in])
        twa: float | None = None
        if wind_in:
            wind_angle_mean = sum(float(r["wind_angle_deg"]) for _, r in wind_in) / len(wind_in)
            ref = int(wind_in[0][1].get("reference", -1))
            heading_mean = _mean(hdg_in) if ref == _WIND_REF_NORTH else None
            twa = _compute_twa(wind_angle_mean, ref, heading_mean)

        target: float | None = None
        pct: float | None = None
        delta: float | None = None
        if bsp is not None and tws is not None and twa is not None:
            lp = polar_map.get((_tws_bin(tws), _twa_bin(twa)))
            if lp is not None and int(lp["session_count"]) >= min_sessions:
                target = float(lp["mean_bsp"])
                pct = bsp / target if target > 0 else None
                delta = round(bsp - target, 4)

        if bsp is not None and tws is not None and twa is not None:
            grade = _grade_from_pct(pct)
        else:
            grade = "unknown"
        if lat is None or lon is None:
            grade = "unknown"

        seg = GradedSegment(
            segment_index=idx,
            t_start=seg_start,
            t_end=seg_end,
            lat=lat,
            lon=lon,
            tws_kts=round(tws, 4) if tws is not None else None,
            twa_deg=round(twa, 4) if twa is not None else None,
            bsp_kts=round(bsp, 4) if bsp is not None else None,
            target_bsp_kts=round(target, 4) if target is not None else None,
            pct_target=round(pct, 4) if pct is not None else None,
            delta_kts=delta,
            grade=grade,
        )
        segments.append(seg)
        grade_hist[grade] += 1

    return segments, grade_hist


async def grade_session_segments(
    storage: Storage,
    session_id: int,
    polar_source: str = "own",
    segment_seconds: int | None = None,
) -> list[GradedSegment]:
    """Return per-segment polar grading for a completed session.

    Segments are fixed-width windows over the session's effective racing window
    [gun, finish] (#812) — prestart maneuvering and post-finish sailing are
    trimmed just as they are for the polar diagram and baseline. Each segment
    carries averaged conditions, the polar target, and a grade label. Results
    are cached in ``polar_segment_grades`` and invalidated when the polar
    baseline is rebuilt.

    The CPU-bound segmentation runs in a worker thread (#603) so long sessions
    don't block the event loop for other HTTP requests.
    """
    width = segment_seconds or POLAR_SEGMENT_SECONDS
    db = storage._conn()

    # Resolve session bounds
    cur = await db.execute(
        "SELECT start_utc, end_utc, vakaros_session_id FROM races WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    if row is None or row["end_utc"] is None:
        return []  # req 16
    try:
        start = datetime.fromisoformat(str(row["start_utc"])).replace(tzinfo=UTC)
        end = datetime.fromisoformat(str(row["end_utc"])).replace(tzinfo=UTC)
    except ValueError:
        return []
    if end <= start:
        return []

    baseline_version = await get_polar_baseline_version(storage)

    # Cache hit?
    cached = await storage.get_polar_segment_grades(session_id, polar_source, baseline_version)
    if cached is not None:
        return [
            GradedSegment(
                segment_index=int(r["segment_index"]),
                t_start=datetime.fromisoformat(str(r["t_start"])).replace(tzinfo=UTC),
                t_end=datetime.fromisoformat(str(r["t_end"])).replace(tzinfo=UTC),
                lat=r["lat"],
                lon=r["lon"],
                tws_kts=r["tws_kts"],
                twa_deg=r["twa_deg"],
                bsp_kts=r["bsp_kts"],
                target_bsp_kts=r["target_bsp_kts"],
                pct_target=r["pct_target"],
                delta_kts=r["delta_kts"],
                grade=r["grade"],
            )
            for r in cached
        ]

    # Cache miss → narrow to the racing window before segmenting so prestart +
    # post-finish don't get graded (#812). Deferred past the cache check so
    # cheap cache hits don't pay for the finish heuristic's queries. (Cached
    # grades from before this change refresh on the next baseline rebuild, which
    # bumps baseline_version and invalidates the cache.)
    try:
        win = await race_window(
            storage,
            start_utc=str(row["start_utc"]),
            end_utc=str(row["end_utc"]),
            vakaros_session_id=row["vakaros_session_id"],
        )
    except ValueError:
        return []
    start, end = win.gun, win.finish
    if end <= start:
        return []

    # Load session window data
    speeds = await storage.query_range("speeds", start, end)
    winds = await storage.query_range("winds", start, end)
    headings = await storage.query_range("headings", start, end)
    positions = await storage.query_range("positions", start, end)

    # Pre-load the entire polar baseline once so the tight per-segment loop
    # has no I/O and can run in a worker thread. Un-migrated DBs (no
    # polar_baseline table) fall through to an empty map → all segments grade
    # "unknown", matching the original per-lookup fallback behavior.
    try:
        polar_map = await storage.get_all_polar_points()
    except Exception as e:  # noqa: BLE001
        logger.warning("Polar grading: baseline lookup failed for session {}: {}", session_id, e)
        polar_map = {}

    segments, grade_hist = await asyncio.to_thread(
        _grade_segments_sync,
        start,
        end,
        width,
        speeds,
        winds,
        headings,
        positions,
        polar_map,
    )

    # Persist cache
    await storage.upsert_polar_segment_grades(
        session_id,
        polar_source,
        [s.to_row() for s in segments],
        baseline_version,
    )

    logger.info(
        "Polar grading: session={} segments={} grades={}",
        session_id,
        len(segments),
        dict(grade_hist),
    )
    return segments
