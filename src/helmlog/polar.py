"""Polar performance baseline: build and query BSP vs (TWS, TWA) buckets.

Historical (TWS, TWA, BSP) triplets from completed race sessions are bucketed,
then mean and p90 BSP are stored per bin. Live BSP can then be compared against
this baseline to show whether the boat is over or under-performing.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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

    # 1. Fetch all completed races
    cur = await db.execute("SELECT id, start_utc, end_utc FROM races WHERE end_utc IS NOT NULL")
    races = list(await cur.fetchall())
    if not races:
        logger.info("Polar: no completed races found; baseline not built")
        await storage.upsert_polar_baseline([], datetime.now(UTC).isoformat())
        return 0

    # bin_samples[(tws_bin, twa_bin)] = list of (race_id, bsp_kts)
    bin_samples: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)

    for race_row in races:
        race_id = int(race_row["id"])
        try:
            start = datetime.fromisoformat(str(race_row["start_utc"])).replace(tzinfo=UTC)
            end = datetime.fromisoformat(str(race_row["end_utc"])).replace(tzinfo=UTC)
        except ValueError:
            logger.warning("Polar: skipping race {} — bad timestamps", race_id)
            continue

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
    logger.info("Polar baseline built: {} bins from {} races", len(rows_to_write), len(races))
    return len(rows_to_write)


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
    """One (TWS, TWA) cell with baseline and session data."""

    tws_bin: int
    twa_bin: int
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


async def session_polar_comparison(
    storage: Storage,
    session_id: int,
) -> SessionPolarData | None:
    """Compare a session's BSP performance against the polar baseline.

    Returns None if the session doesn't exist or hasn't ended.
    Returns a SessionPolarData with empty cells if no instrument data is available.
    """
    db = storage._conn()

    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None or row["end_utc"] is None:
        return None

    try:
        start = datetime.fromisoformat(str(row["start_utc"])).replace(tzinfo=UTC)
        end = datetime.fromisoformat(str(row["end_utc"])).replace(tzinfo=UTC)
    except ValueError:
        return None

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

    # Bin session samples
    bin_samples: dict[tuple[int, int], list[float]] = defaultdict(list)
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
        bin_samples[(tb, ab)].append(bsp_kts)

    # Load full baseline
    baseline: dict[tuple[int, int], dict[str, Any]] = {}
    try:
        bcur = await db.execute(
            "SELECT tws_bin, twa_bin, mean_bsp, p90_bsp"
            " FROM polar_baseline WHERE session_count >= 3"
        )
        for br in await bcur.fetchall():
            baseline[(int(br["tws_bin"]), int(br["twa_bin"]))] = dict(br)
    except Exception:
        pass  # no baseline table on un-migrated DB

    # Merge session + baseline into cells
    all_keys = set(bin_samples.keys()) | set(baseline.keys())
    cells: list[PolarCell] = []
    total_samples = 0

    for key in all_keys:
        tws_b, twa_b = key
        samples = bin_samples.get(key, [])
        bl = baseline.get(key)

        session_mean = round(sum(samples) / len(samples), 4) if samples else None
        bl_mean = float(bl["mean_bsp"]) if bl else None
        bl_p90 = float(bl["p90_bsp"]) if bl else None
        delta = (
            round(session_mean - bl_mean, 4)
            if session_mean is not None and bl_mean is not None
            else None
        )

        n = len(samples)
        total_samples += n

        # Only include cells where the session has data
        if n > 0:
            cells.append(
                PolarCell(
                    tws_bin=tws_b,
                    twa_bin=twa_b,
                    baseline_mean_bsp=bl_mean,
                    baseline_p90_bsp=bl_p90,
                    session_mean_bsp=session_mean,
                    session_sample_count=n,
                    delta=delta,
                )
            )

    cells.sort(key=lambda c: (c.tws_bin, c.twa_bin))
    tws_bins = sorted({c.tws_bin for c in cells})
    twa_bins = sorted({c.twa_bin for c in cells})

    return SessionPolarData(
        cells=cells,
        tws_bins=tws_bins,
        twa_bins=twa_bins,
        session_sample_count=total_samples,
    )
