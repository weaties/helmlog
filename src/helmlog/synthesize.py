"""J/105 race simulation engine — synthesize realistic sailing sessions.

Generates 1 Hz data rows with positions, headings, speeds, COG/SOG,
true/apparent wind, and depth based on J/105 polars and a configurable
wind model with periodic shifts.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helmlog.courses import CourseLeg

from helmlog.courses import _BBOX_E, _BBOX_N, _BBOX_S, _BBOX_W, is_in_water
from helmlog.wind_field import WindField

# ---------------------------------------------------------------------------
# J/105 polar performance table
# TWS (kts) -> (upwind_twa deg, upwind_bsp, downwind_twa deg, downwind_bsp)
# ---------------------------------------------------------------------------

J105_POLARS: dict[int, tuple[float, float, float, float]] = {
    6: (44.0, 5.2, 150.0, 4.8),
    8: (43.0, 6.0, 145.0, 5.8),
    10: (42.0, 6.5, 140.0, 6.5),
    12: (41.0, 6.9, 138.0, 7.0),
    14: (40.0, 7.2, 135.0, 7.4),
    16: (39.0, 7.3, 130.0, 7.6),
}


# ---------------------------------------------------------------------------
# Polar interpolation
# ---------------------------------------------------------------------------


def interpolate_polar(tws: float, upwind: bool) -> tuple[float, float]:
    """Look up J/105 polar for given TWS. Returns (optimal_twa deg, bsp_kts).

    TWS values below or above the table range are clamped to the nearest entry.
    """
    keys = sorted(J105_POLARS)
    tws_c = max(keys[0], min(keys[-1], tws))

    lo = keys[0]
    hi = keys[-1]
    for _i, k in enumerate(keys):
        if k <= tws_c:
            lo = k
        if k >= tws_c:
            hi = k
            break

    if lo == hi:
        row = J105_POLARS[lo]
        return (row[0], row[1]) if upwind else (row[2], row[3])

    frac = (tws_c - lo) / (hi - lo)
    r_lo = J105_POLARS[lo]
    r_hi = J105_POLARS[hi]
    i0, i1 = (0, 1) if upwind else (2, 3)
    twa = r_lo[i0] + frac * (r_hi[i0] - r_lo[i0])
    bsp = r_lo[i1] + frac * (r_hi[i1] - r_lo[i1])
    return twa, bsp


# ---------------------------------------------------------------------------
# Apparent wind from true wind + boat speed
# ---------------------------------------------------------------------------


def apparent_wind(tws: float, twa_deg: float, bsp: float) -> tuple[float, float]:
    """Compute apparent wind speed and angle.

    Args:
        tws: true wind speed (kts)
        twa_deg: true wind angle 0-360 deg clockwise from bow (0 = head-to-wind)
        bsp: boat speed through water (kts)

    Returns:
        (aws_kts, awa_deg) with awa 0-360 deg clockwise from bow
    """
    twa_r = math.radians(twa_deg)
    ax = tws * math.sin(twa_r)
    ay = tws * math.cos(twa_r) + bsp
    aws = math.sqrt(ax * ax + ay * ay)
    awa = math.degrees(math.atan2(ax, ay)) % 360
    return aws, awa


# ---------------------------------------------------------------------------
# Wind model — periodic shifts + speed variation
# ---------------------------------------------------------------------------


@dataclass
class _WindShift:
    time: float  # seconds from race start
    twd_offset: float  # degrees offset from base TWD
    tws: float  # TWS at this shift point


class WindModel:
    """Generate a realistic wind timeline with shifts and gusts."""

    def __init__(
        self,
        base_twd: float = 0.0,
        tws_low: float = 8.0,
        tws_high: float = 14.0,
        duration_s: float = 7200.0,
        shift_interval: tuple[float, float] = (600.0, 1200.0),
        shift_magnitude: tuple[float, float] = (5.0, 14.0),
        seed: int | None = None,
    ) -> None:
        self.base_twd = base_twd
        self._rng = random.Random(seed)
        self._shifts: list[_WindShift] = []
        self._build(tws_low, tws_high, duration_s, shift_interval, shift_magnitude)

    def _build(
        self,
        tws_lo: float,
        tws_hi: float,
        dur: float,
        interval: tuple[float, float],
        magnitude: tuple[float, float],
    ) -> None:
        t = 0.0
        offset = 0.0
        tws = self._rng.uniform(tws_lo, tws_hi)
        self._shifts.append(_WindShift(t, offset, tws))

        while t < dur:
            t += self._rng.uniform(*interval)
            mag = self._rng.uniform(*magnitude)
            direction = 1 if self._rng.random() > 0.5 else -1
            offset += direction * mag
            offset = max(-25.0, min(25.0, offset))
            tws = self._rng.uniform(tws_lo, tws_hi)
            self._shifts.append(_WindShift(t, offset, tws))

    def get(self, elapsed_s: float) -> tuple[float, float]:
        """Return (twd, tws) at the given elapsed seconds."""
        prev = self._shifts[0]
        nxt = self._shifts[-1]
        for i, s in enumerate(self._shifts):
            if s.time <= elapsed_s:
                prev = s
                nxt = self._shifts[min(i + 1, len(self._shifts) - 1)]
            else:
                break

        if prev.time == nxt.time:
            frac = 0.0
        else:
            frac = min(1.0, max(0.0, (elapsed_s - prev.time) / (nxt.time - prev.time)))

        # Smooth interpolation (ease in/out)
        smooth = 0.5 - 0.5 * math.cos(frac * math.pi)
        twd_off = prev.twd_offset + smooth * (nxt.twd_offset - prev.twd_offset)
        tws = prev.tws + smooth * (nxt.tws - prev.tws)

        # Small-scale noise
        twd_off += self._rng.gauss(0, 1.5)
        tws += self._rng.gauss(0, 0.3)

        twd = (self.base_twd + twd_off) % 360
        tws = max(4.0, tws)
        return twd, tws


# ---------------------------------------------------------------------------
# Data row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthRow:
    """One second of synthesized sailing data."""

    ts: datetime
    lat: float
    lon: float
    heading: float
    bsp: float
    cog: float
    sog: float
    tws: float
    twa: float
    aws: float
    awa: float
    depth: float


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderResponseConfig:
    """Configuration for probabilistic header/lift response.

    Controls how realistically the synthesized boat responds to wind shifts.
    On a beat the boat should tack on lifts; on a run it should gybe on
    headers — but not on every shift.
    """

    reaction_probability: float = 0.70
    """Base probability of responding to any detected shift (0-1)."""

    min_shift_threshold: tuple[float, float] = (3.0, 8.0)
    """Range (lo, hi) in degrees for per-shift notice threshold.
    Each shift draws its own threshold from U(lo, hi); shifts smaller
    than the drawn value are ignored."""

    reaction_delay: tuple[float, float] = (10.0, 45.0)
    """Range (lo, hi) in seconds of delay before executing a maneuver
    after the crew 'notices' a shift."""

    fatigue_start_frac: float = 0.70
    """Fraction of total race elapsed before fatigue begins degrading
    reaction probability (0-1, 1.0 = no fatigue)."""

    fatigue_floor: float = 0.40
    """Minimum reaction probability at the very end of the race."""


# Sensible J/105 club-racer defaults
_DEFAULT_HEADER_RESPONSE = HeaderResponseConfig()


@dataclass(frozen=True)
class CollisionAvoidanceConfig:
    """Configuration for collision avoidance with other boats' tracks.

    During synthesis, the engine checks proposed positions against all other
    boats' positions at the same timestamp.  If a collision would occur,
    it adjusts speed to maintain separation.
    """

    min_separation_m: float = 30.0
    """Minimum distance in metres between any two boats at the same timestamp.
    Default is ~3 J/105 boat lengths (~30 m)."""


_DEFAULT_COLLISION_AVOIDANCE = CollisionAvoidanceConfig()


@dataclass(frozen=True)
class SynthConfig:
    """Configuration for a synthesized race simulation."""

    start_lat: float
    start_lon: float
    base_twd: float
    tws_low: float
    tws_high: float
    shift_interval: tuple[float, float]
    shift_magnitude: tuple[float, float]
    legs: list[CourseLeg]
    seed: int
    start_time: datetime
    wind_seed: int | None = None
    """Seed for the WindField.  When None, ``seed`` is used for both the
    wind model and the boat behaviour RNG (backwards-compatible default).
    Set this to a shared value across boats to give them identical wind
    while each boat's ``seed`` drives its own sailing RNG."""
    header_response: HeaderResponseConfig = _DEFAULT_HEADER_RESPONSE
    collision_avoidance: CollisionAvoidanceConfig = _DEFAULT_COLLISION_AVOIDANCE


# ---------------------------------------------------------------------------
# Collision avoidance — check against other boats' tracks (#246)
# ---------------------------------------------------------------------------

# One metre in nautical miles (for separation distance conversion)
_METRES_PER_NM = 1852.0


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in metres (flat earth, fine for < 5 nm)."""
    dlat = (lat2 - lat1) * 60.0
    dlon = (lon2 - lon1) * 60.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon) * _METRES_PER_NM


class TrackIndex:
    """Time-indexed position lookup for other boats' tracks.

    Accepts a list of tracks, each being a list of ``{timestamp, LAT, LON}``
    dicts (the format returned by the peer API track endpoint).
    Provides fast lookup of all boat positions at a given ISO-second timestamp.
    """

    def __init__(self, tracks: list[list[dict[str, Any]]]) -> None:
        # Index: timestamp_str -> list[(lat, lon)]
        self._positions: dict[str, list[tuple[float, float]]] = {}
        for track in tracks:
            for pt in track:
                ts = str(pt.get("timestamp", ""))[:19]
                lat = pt.get("LAT")
                lon = pt.get("LON")
                if ts and lat is not None and lon is not None:
                    self._positions.setdefault(ts, []).append((float(lat), float(lon)))

    def positions_at(self, ts_iso: str) -> list[tuple[float, float]]:
        """Return all other-boat positions at the given ISO-second timestamp."""
        return self._positions.get(ts_iso[:19], [])

    def __len__(self) -> int:
        return len(self._positions)


def _has_collision(
    lat: float,
    lon: float,
    ts_iso: str,
    track_index: TrackIndex | None,
    min_separation_m: float,
) -> bool:
    """Return True if (lat, lon) at ts_iso is within min_separation_m of any other boat."""
    if track_index is None:
        return False
    for other_lat, other_lon in track_index.positions_at(ts_iso):
        if _distance_m(lat, lon, other_lat, other_lon) < min_separation_m:
            return True
    return False


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------


def _distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in nautical miles (flat earth, fine for < 5 nm)."""
    dlat = (lat2 - lat1) * 60.0
    dlon = (lon2 - lon1) * 60.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat * dlat + dlon * dlon)


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing from point 1 to point 2 (degrees true)."""
    dlat = (lat2 - lat1) * 60.0
    dlon = (lon2 - lon1) * 60.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2(dlon, dlat)) % 360


def _tack_speed(progress: float, base_bsp: float) -> float:
    """BSP profile during a tack: dips to ~55% at midpoint."""
    dip: float = 0.55 + 0.45 * float(abs(2.0 * progress - 1.0)) ** 1.5
    return base_bsp * dip


def _gybe_speed(progress: float, base_bsp: float) -> float:
    """BSP profile during a gybe: dips to ~75% at midpoint."""
    dip: float = 0.75 + 0.25 * float(abs(2.0 * progress - 1.0)) ** 1.2
    return base_bsp * dip


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

_DEPTH_FLOOR = 2.0  # metres — Puget Sound racing areas are 10-200m+ deep
_POS_DECIMALS = 7  # decimal places for stored lat/lon


def _water_at_stored_precision(lat: float, lon: float) -> bool:
    """Check is_in_water at the same precision stored in SynthRow.

    The simulation operates at full float precision, but SynthRow rounds
    lat/lon to ``_POS_DECIMALS`` places.  Right at the coastline boundary
    the rounding can push a point from water to land.  This helper ensures
    the *stored* position will pass the land check.
    """
    return is_in_water(round(lat, _POS_DECIMALS), round(lon, _POS_DECIMALS))


def _segment_crosses_land(lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
    """Return True if the straight-line segment between two points crosses land.

    Both endpoints may be in water, but the line between them could clip a
    peninsula or island.  Checks intermediate points along the segment.
    """
    # Quick check: if the step is tiny (<~50m), endpoint check is sufficient.
    # At this scale, both points in water means the segment can't meaningfully
    # cross land (OSM coastline data is simplified to ~30m tolerance).
    dlat = abs(lat2 - lat1)
    dlon = abs(lon2 - lon1)
    if dlat < 0.0005 and dlon < 0.0005:
        return False
    # Sample intermediate points — one every ~30m
    steps = max(3, int(max(dlat, dlon) / 0.0003))
    for i in range(1, steps):
        frac = i / steps
        mid_lat = lat1 + frac * (lat2 - lat1)
        mid_lon = lon1 + frac * (lon2 - lon1)
        if not is_in_water(mid_lat, mid_lon):
            return True
    return False


def _clamp_to_bbox(lat: float, lon: float) -> tuple[float, float]:
    """Clamp a position to stay inside the coastline data bounding box.

    Leaves a small margin so the point isn't right on the edge where
    is_in_water returns False.
    """
    margin = 0.002  # ~200m margin inside the bbox
    lat = max(_BBOX_S + margin, min(_BBOX_N - margin, lat))
    lon = max(_BBOX_W + margin, min(_BBOX_E - margin, lon))
    return lat, lon


def simulate(
    config: SynthConfig,
    other_tracks: list[list[dict[str, Any]]] | None = None,
) -> list[SynthRow]:
    """Simulate a full race, returning 1 Hz data rows.

    Uses J/105 polars for boat speed, a WindModel for realistic wind shifts,
    and tacking/gybing maneuvers with speed dip profiles.

    Args:
        config: Simulation configuration.
        other_tracks: Optional list of other boats' tracks (each a list of
            ``{timestamp, LAT, LON, ...}`` dicts from the peer track endpoint).
            When provided, the engine ensures the generated track maintains
            at least ``config.collision_avoidance.min_separation_m`` from all
            other boats at each timestamp.
    """
    rng = random.Random(config.seed)
    w_seed = config.wind_seed if config.wind_seed is not None else config.seed
    wind = WindField(
        base_twd=config.base_twd,
        tws_low=config.tws_low,
        tws_high=config.tws_high,
        shift_interval=config.shift_interval,
        shift_magnitude=config.shift_magnitude,
        ref_lat=config.start_lat,
        ref_lon=config.start_lon,
        seed=w_seed,
    )

    # Collision avoidance index (#246)
    track_idx: TrackIndex | None = None
    if other_tracks:
        track_idx = TrackIndex(other_tracks)
    ca_min_sep = config.collision_avoidance.min_separation_m

    lat, lon = config.start_lat, config.start_lon
    heading = 0.0
    bsp = 0.0
    on_stbd = True
    base_depth = 8.0
    rows: list[SynthRow] = []
    elapsed = 0.0
    dt = 1.0

    # Maneuver state
    in_maneuver = False
    man_elapsed = 0.0
    man_duration = 0.0
    man_start_hdg = 0.0
    man_target_hdg = 0.0
    man_start_bsp = 0.0
    man_is_tack = True

    # Header response state — tracks wind direction to detect shifts
    # Use a separate RNG so header response draws don't perturb the base
    # simulation's interval-tack / noise randomisation.
    hrc = config.header_response
    hr_rng = random.Random(config.seed ^ 0x48454144)  # "HEAD" in ASCII
    prev_twd: float | None = None
    # Pending header-triggered maneuver: (delay_remaining_s,)
    header_delay_remaining = 0.0
    header_tack_pending = False
    # Estimate total race duration for fatigue curve (rough: ~10 min per leg)
    est_total_s = len(config.legs) * 600.0

    for leg_idx, leg in enumerate(config.legs):
        tack_timer = 0.0
        # Downwind legs need fewer gybes than upwind tacks — longer intervals
        next_tack = rng.uniform(200, 400) if leg.upwind else rng.uniform(300, 500)
        in_maneuver = False
        on_stbd = leg_idx % 2 == 0
        # Record initial bearing to mark for overshoot detection
        leg_initial_bearing = _bearing(lat, lon, leg.target.lat, leg.target.lon)
        reached_mark = False
        # Per-leg: draw a shift-notice threshold for next shift event
        shift_notice_threshold = hr_rng.uniform(*hrc.min_shift_threshold)
        header_tack_pending = False
        header_delay_remaining = 0.0

        while True:
            t = config.start_time + timedelta(seconds=elapsed)
            twd, tws = wind.at(elapsed, lat, lon)
            opt_twa, polar_bsp = interpolate_polar(tws, leg.upwind)

            dist = _distance_nm(lat, lon, leg.target.lat, leg.target.lon)

            if in_maneuver:
                p = man_elapsed / man_duration
                smooth_p = 0.5 - 0.5 * math.cos(p * math.pi)
                dh = (man_target_hdg - man_start_hdg + 540) % 360 - 180
                heading = (man_start_hdg + smooth_p * dh) % 360
                bsp = (
                    _tack_speed(p, man_start_bsp) if man_is_tack else _gybe_speed(p, man_start_bsp)
                )
                man_elapsed += dt
                if man_elapsed >= man_duration:
                    in_maneuver = False
                    heading = man_target_hdg
                    tack_timer = 0.0
                    next_tack = rng.uniform(200, 400) if leg.upwind else rng.uniform(300, 500)
            else:
                # Pick the tack/gybe with better VMG toward the mark
                brg = _bearing(lat, lon, leg.target.lat, leg.target.lon)
                stbd_hdg = (twd - opt_twa + 360) % 360
                port_hdg = (twd - (360.0 - opt_twa) + 360) % 360
                stbd_off = abs(((brg - stbd_hdg + 180) % 360) - 180)
                port_off = abs(((brg - port_hdg + 180) % 360) - 180)
                best_stbd = stbd_off <= port_off

                # Layline overstand check — force tack/gybe when the boat
                # is near the layline and past it by more than ~4 boat
                # lengths (J/105 LOA ≈ 35 ft ≈ 0.006 nm).
                # Applies to both upwind and downwind legs.
                # Only activates within 0.4 nm of the mark (layline zone)
                # and requires a 30 s cooldown after any maneuver.
                _MAX_OVERSTAND_NM = 0.023  # 4 × 35 ft
                _LAYLINE_ZONE_NM = 0.4  # only check near the mark
                _LAYLINE_COOLDOWN_S = 30.0  # min seconds between forced maneuvers
                force_tack = False
                if (
                    0.08 < dist < _LAYLINE_ZONE_NM
                    and best_stbd != on_stbd
                    and tack_timer >= _LAYLINE_COOLDOWN_S
                ):
                    # other_off = angle from mark bearing to the tack/gybe
                    # heading we'd switch to.  When small, the mark is
                    # nearly fetchable on the other tack (near layline).
                    # Perpendicular overstand = dist * sin(other_off).
                    other_off = port_off if on_stbd else stbd_off
                    overstand_nm = dist * math.sin(math.radians(min(other_off, 90)))
                    if overstand_nm > _MAX_OVERSTAND_NM and other_off < 15.0:
                        force_tack = True

                # When very close to mark, snap to the optimal tack
                if dist < 0.10:
                    on_stbd = best_stbd

                twa_target = opt_twa if on_stbd else (360.0 - opt_twa) % 360
                heading = (twd - twa_target + 360) % 360
                bsp = polar_bsp * rng.gauss(1.0, 0.02)
                bsp = max(2.0, bsp)

                tack_timer += dt

                # --- Header response: detect wind shifts and react ---
                if header_tack_pending:
                    header_delay_remaining -= dt
                    if header_delay_remaining <= 0:
                        # Crew has decided to tack/gybe — execute it now
                        header_tack_pending = False
                        want_stbd = best_stbd
                        if want_stbd != on_stbd and dist >= 0.10:
                            on_stbd = want_stbd
                            new_twa = opt_twa if on_stbd else (360.0 - opt_twa) % 360
                            new_heading = (twd - new_twa + 360) % 360

                            in_maneuver = True
                            man_elapsed = 0.0
                            man_start_hdg = heading
                            man_target_hdg = new_heading
                            man_start_bsp = bsp
                            man_is_tack = leg.upwind
                            man_duration = rng.uniform(8, 12) if leg.upwind else rng.uniform(5, 8)
                            tack_timer = 0.0
                            next_tack = (
                                rng.uniform(200, 400) if leg.upwind else rng.uniform(300, 500)
                            )

                if prev_twd is not None and not in_maneuver and not header_tack_pending:
                    twd_delta = ((twd - prev_twd + 180) % 360) - 180
                    abs_delta = abs(twd_delta)

                    if abs_delta >= shift_notice_threshold:
                        # Classify: upwind lift = should tack, downwind header = should gybe
                        # Lift (upwind): shift toward the boat's current tack
                        #   stbd tack → positive delta is a lift
                        #   port tack → negative delta is a lift
                        # Header (downwind): shift away from current gybe
                        is_favorable_shift = (on_stbd and twd_delta > 0) or (
                            not on_stbd and twd_delta < 0
                        )
                        should_respond = (leg.upwind and is_favorable_shift) or (
                            not leg.upwind and not is_favorable_shift
                        )

                        if should_respond and best_stbd != on_stbd and dist >= 0.10:
                            # Apply fatigue degradation
                            race_frac = min(1.0, elapsed / max(est_total_s, 1.0))
                            if race_frac > hrc.fatigue_start_frac:
                                fatigue_frac = (race_frac - hrc.fatigue_start_frac) / (
                                    1.0 - hrc.fatigue_start_frac
                                )
                                prob = hrc.reaction_probability + fatigue_frac * (
                                    hrc.fatigue_floor - hrc.reaction_probability
                                )
                            else:
                                prob = hrc.reaction_probability

                            if hr_rng.random() < prob:
                                header_tack_pending = True
                                header_delay_remaining = hr_rng.uniform(*hrc.reaction_delay)

                        # Draw a new threshold for the next shift event
                        shift_notice_threshold = hr_rng.uniform(*hrc.min_shift_threshold)

                prev_twd = twd

                if force_tack and not in_maneuver:
                    # At the layline — save current heading, then initiate
                    # tack/gybe maneuver to the favoured tack
                    old_heading = heading  # current tack heading
                    on_stbd = best_stbd
                    new_twa = opt_twa if on_stbd else (360.0 - opt_twa) % 360
                    new_heading = (twd - new_twa + 360) % 360

                    in_maneuver = True
                    man_elapsed = 0.0
                    man_start_hdg = old_heading
                    man_target_hdg = new_heading
                    man_start_bsp = bsp
                    man_is_tack = leg.upwind
                    man_duration = rng.uniform(8, 12) if leg.upwind else rng.uniform(5, 8)
                    tack_timer = 0.0
                    next_tack = rng.uniform(200, 400) if leg.upwind else rng.uniform(300, 500)
                elif tack_timer >= next_tack and not in_maneuver and dist >= 0.10:
                    # Tack to the side with better VMG toward the mark
                    want_stbd = best_stbd
                    if want_stbd == on_stbd:
                        # Already on the favoured tack; reset timer and wait
                        tack_timer = 0.0
                        next_tack = rng.uniform(200, 400) if leg.upwind else rng.uniform(300, 500)
                    else:
                        on_stbd = want_stbd
                        new_twa = opt_twa if on_stbd else (360.0 - opt_twa) % 360
                        new_heading = (twd - new_twa + 360) % 360

                        in_maneuver = True
                        man_elapsed = 0.0
                        man_start_hdg = heading
                        man_target_hdg = new_heading
                        man_start_bsp = bsp
                        man_is_tack = leg.upwind
                        man_duration = rng.uniform(8, 12) if leg.upwind else rng.uniform(5, 8)

            # Update position — with land avoidance
            hdg_r = math.radians(heading)
            spd_deg_s = bsp / 3600.0 / 60.0  # kts -> deg_lat/sec
            new_lat = lat + spd_deg_s * math.cos(hdg_r) * dt
            new_lon = lon + spd_deg_s * math.sin(hdg_r) * dt / math.cos(math.radians(lat))

            # Clamp to bounding box to prevent drifting outside coastline data
            new_lat, new_lon = _clamp_to_bbox(new_lat, new_lon)

            def _position_ok(
                nlat: float, nlon: float, cur_lat: float = lat, cur_lon: float = lon
            ) -> bool:
                """Check endpoint is in water AND segment doesn't cross land."""
                if not _water_at_stored_precision(nlat, nlon):
                    return False
                return not _segment_crosses_land(cur_lat, cur_lon, nlat, nlon)

            if not _position_ok(new_lat, new_lon):
                # About to sail onto land — force immediate tack away
                on_stbd = not on_stbd
                twa_target = opt_twa if on_stbd else (360.0 - opt_twa) % 360
                heading = (twd - twa_target + 360) % 360
                in_maneuver = False
                tack_timer = 0.0
                next_tack = rng.uniform(60, 120)
                # Recalculate position with new heading
                hdg_r = math.radians(heading)
                new_lat = lat + spd_deg_s * math.cos(hdg_r) * dt
                new_lon = lon + spd_deg_s * math.sin(hdg_r) * dt / math.cos(math.radians(lat))
                new_lat, new_lon = _clamp_to_bbox(new_lat, new_lon)
                if not _position_ok(new_lat, new_lon):
                    # Both tacks hit land — scan headings to find the best
                    # escape route toward the mark
                    brg_mark = _bearing(lat, lon, leg.target.lat, leg.target.lon)
                    best_hdg = None
                    best_diff = 360.0
                    for probe in range(0, 360, 10):
                        pr = math.radians(probe)
                        tl = lat + spd_deg_s * math.cos(pr) * dt
                        tn = lon + spd_deg_s * math.sin(pr) * dt / math.cos(math.radians(lat))
                        tl, tn = _clamp_to_bbox(tl, tn)
                        if _position_ok(tl, tn):
                            diff = abs(((probe - brg_mark + 180) % 360) - 180)
                            if diff < best_diff:
                                best_diff = diff
                                best_hdg = probe
                    if best_hdg is not None:
                        heading = float(best_hdg)
                        hdg_r = math.radians(heading)
                        new_lat = lat + spd_deg_s * math.cos(hdg_r) * dt
                        new_lon = lon + spd_deg_s * math.sin(hdg_r) * dt / math.cos(
                            math.radians(lat)
                        )
                        new_lat, new_lon = _clamp_to_bbox(new_lat, new_lon)
                    else:
                        new_lat, new_lon = lat, lon

            lat, lon = new_lat, new_lon

            # Collision avoidance (#246): if the new position would collide
            # with another boat at this timestamp, recompute from the
            # previous position at progressively reduced speeds.
            ts_iso = t.isoformat()[:19]
            if _has_collision(lat, lon, ts_iso, track_idx, ca_min_sep):
                prev_lat = rows[-1].lat if rows else config.start_lat
                prev_lon = rows[-1].lon if rows else config.start_lon
                hdg_r = math.radians(heading)
                for speed_frac in (0.6, 0.3, 0.0):
                    ca_bsp = bsp * speed_frac
                    ca_deg_s = ca_bsp / 3600.0 / 60.0
                    ca_lat = prev_lat + ca_deg_s * math.cos(hdg_r) * dt
                    ca_lon = prev_lon + ca_deg_s * math.sin(hdg_r) * dt / math.cos(
                        math.radians(prev_lat)
                    )
                    ca_lat, ca_lon = _clamp_to_bbox(ca_lat, ca_lon)
                    if not _has_collision(ca_lat, ca_lon, ts_iso, track_idx, ca_min_sep):
                        lat, lon = ca_lat, ca_lon
                        bsp = ca_bsp
                        break
                else:
                    # All speeds still collide — hold position
                    lat, lon = prev_lat, prev_lon
                    bsp = 0.0

            # Compute TWA and apparent wind
            twa_actual = (twd - heading + 360) % 360
            aws, awa = apparent_wind(tws, twa_actual, bsp)

            # COG/SOG (GPS: heading/bsp + tiny noise)
            cog = (heading + rng.gauss(0, 0.5)) % 360
            sog = max(0, bsp + rng.gauss(0, 0.1))

            depth = max(_DEPTH_FLOOR, base_depth + rng.gauss(0, 0.3))

            rows.append(
                SynthRow(
                    ts=t,
                    lat=round(lat, _POS_DECIMALS),
                    lon=round(lon, _POS_DECIMALS),
                    heading=round(heading, 1),
                    bsp=round(bsp, 2),
                    cog=round(cog, 1),
                    sog=round(sog, 2),
                    tws=round(tws, 2),
                    twa=round(twa_actual, 1),
                    aws=round(aws, 2),
                    awa=round(awa, 1),
                    depth=round(max(_DEPTH_FLOOR, depth), 1),
                )
            )
            elapsed += dt

            if dist < 0.08:
                reached_mark = True
                break
            # Overshoot detection: if the bearing to the mark has swung more than
            # 90 deg from the initial approach bearing, we've sailed past it.
            brg_to_mark = _bearing(lat, lon, leg.target.lat, leg.target.lon)
            brg_diff = abs(((brg_to_mark - leg_initial_bearing + 180) % 360) - 180)
            if brg_diff > 90:
                reached_mark = True
                break
            if elapsed > 7200:
                reached_mark = False
                break

        # Only snap to mark if we actually reached it — don't teleport on timeout
        if (
            reached_mark
            and _water_at_stored_precision(leg.target.lat, leg.target.lon)
            and not _segment_crosses_land(lat, lon, leg.target.lat, leg.target.lon)
        ):
            lat, lon = leg.target.lat, leg.target.lon

        # If the leg timed out, abort the race — remaining legs would produce
        # teleports and unrealistic tracks
        if not reached_mark:
            break

        # For the last leg, append a final row at the finish mark position
        # so the track ends exactly at the finish line (near the start).
        if leg_idx == len(config.legs) - 1:
            t = config.start_time + timedelta(seconds=elapsed)
            twd, tws = wind.at(elapsed, lat, lon)
            twa_actual = (twd - heading + 360) % 360
            aws, awa = apparent_wind(tws, twa_actual, bsp)
            cog = (heading + rng.gauss(0, 0.5)) % 360
            sog = max(0, bsp + rng.gauss(0, 0.1))
            depth = max(_DEPTH_FLOOR, base_depth + rng.gauss(0, 0.3))
            rows.append(
                SynthRow(
                    ts=t,
                    lat=round(lat, _POS_DECIMALS),
                    lon=round(lon, _POS_DECIMALS),
                    heading=round(heading, 1),
                    bsp=round(bsp, 2),
                    cog=round(cog, 1),
                    sog=round(sog, 2),
                    tws=round(tws, 2),
                    twa=round(twa_actual, 1),
                    aws=round(aws, 2),
                    awa=round(awa, 1),
                    depth=round(max(_DEPTH_FLOOR, depth), 1),
                )
            )
            elapsed += dt

        # Mark rounding transition
        if leg_idx < len(config.legs) - 1:
            next_leg = config.legs[leg_idx + 1]
            twd, tws = wind.at(elapsed, lat, lon)
            next_opt_twa, _ = interpolate_polar(tws, next_leg.upwind)
            next_stbd = (leg_idx + 1) % 2 == 0
            next_twa = next_opt_twa if next_stbd else (360.0 - next_opt_twa) % 360
            target_hdg = (twd - next_twa + 360) % 360

            rounding_dur = rng.uniform(15, 25)
            start_hdg = heading
            start_bsp = bsp

            for step in range(int(rounding_dur)):
                t = config.start_time + timedelta(seconds=elapsed)
                twd, tws = wind.at(elapsed, lat, lon)

                p = step / rounding_dur
                smooth_p = 0.5 - 0.5 * math.cos(p * math.pi)
                dh = (target_hdg - start_hdg + 540) % 360 - 180
                heading = (start_hdg + smooth_p * dh) % 360

                bsp = start_bsp * (0.6 + 0.4 * abs(2 * p - 1))

                hdg_r = math.radians(heading)
                spd_deg_s = bsp / 3600.0 / 60.0
                new_lat = lat + spd_deg_s * math.cos(hdg_r) * dt
                new_lon = lon + spd_deg_s * math.sin(hdg_r) * dt / math.cos(math.radians(lat))
                new_lat, new_lon = _clamp_to_bbox(new_lat, new_lon)
                if _water_at_stored_precision(new_lat, new_lon) and not _segment_crosses_land(
                    lat, lon, new_lat, new_lon
                ):
                    lat, lon = new_lat, new_lon

                twa_actual = (twd - heading + 360) % 360
                aws, awa = apparent_wind(tws, twa_actual, bsp)
                cog = (heading + rng.gauss(0, 0.5)) % 360
                sog = max(0, bsp + rng.gauss(0, 0.1))
                depth = max(_DEPTH_FLOOR, base_depth + rng.gauss(0, 0.3))

                rows.append(
                    SynthRow(
                        ts=t,
                        lat=round(lat, _POS_DECIMALS),
                        lon=round(lon, _POS_DECIMALS),
                        heading=round(heading, 1),
                        bsp=round(bsp, 2),
                        cog=round(cog, 1),
                        sog=round(sog, 2),
                        tws=round(tws, 2),
                        twa=round(twa_actual, 1),
                        aws=round(aws, 2),
                        awa=round(awa, 1),
                        depth=round(max(_DEPTH_FLOOR, depth), 1),
                    )
                )
                elapsed += dt

    return rows
