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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helmlog.courses import CourseLeg

# ---------------------------------------------------------------------------
# J/105 polar performance table
# TWS (kts) -> (upwind_twa°, upwind_bsp, downwind_twa°, downwind_bsp)
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
    """Look up J/105 polar for given TWS. Returns (optimal_twa°, bsp_kts).

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
        twa_deg: true wind angle 0-360° clockwise from bow (0 = head-to-wind)
        bsp: boat speed through water (kts)

    Returns:
        (aws_kts, awa_deg) with awa 0-360° clockwise from bow
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


def simulate(config: SynthConfig) -> list[SynthRow]:
    """Simulate a full race, returning 1 Hz data rows.

    Uses J/105 polars for boat speed, a WindModel for realistic wind shifts,
    and tacking/gybing maneuvers with speed dip profiles.
    """
    rng = random.Random(config.seed)
    wind = WindModel(
        base_twd=config.base_twd,
        tws_low=config.tws_low,
        tws_high=config.tws_high,
        shift_interval=config.shift_interval,
        shift_magnitude=config.shift_magnitude,
        seed=config.seed,
    )

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

    for leg_idx, leg in enumerate(config.legs):
        tack_timer = 0.0
        next_tack = rng.uniform(150, 300)
        in_maneuver = False
        on_stbd = leg_idx % 2 == 0

        while True:
            t = config.start_time + timedelta(seconds=elapsed)
            twd, tws = wind.get(elapsed)
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
                    next_tack = rng.uniform(120, 300)
            else:
                # Near mark: pick the tack/gybe that VMGs best toward it
                if dist < 0.15:
                    brg = _bearing(lat, lon, leg.target.lat, leg.target.lon)
                    stbd_hdg = (twd - opt_twa + 360) % 360
                    port_hdg = (twd - (360.0 - opt_twa) + 360) % 360
                    stbd_off = abs(((brg - stbd_hdg + 180) % 360) - 180)
                    port_off = abs(((brg - port_hdg + 180) % 360) - 180)
                    on_stbd = stbd_off <= port_off

                twa_target = opt_twa if on_stbd else (360.0 - opt_twa) % 360
                heading = (twd - twa_target + 360) % 360
                bsp = polar_bsp * rng.gauss(1.0, 0.02)
                bsp = max(2.0, bsp)

                tack_timer += dt
                if tack_timer >= next_tack and not in_maneuver and dist >= 0.15:
                    on_stbd = not on_stbd
                    new_twa = opt_twa if on_stbd else (360.0 - opt_twa) % 360
                    new_heading = (twd - new_twa + 360) % 360

                    in_maneuver = True
                    man_elapsed = 0.0
                    man_start_hdg = heading
                    man_target_hdg = new_heading
                    man_start_bsp = bsp
                    man_is_tack = leg.upwind
                    man_duration = rng.uniform(8, 12) if leg.upwind else rng.uniform(5, 8)

            # Update position
            hdg_r = math.radians(heading)
            spd_deg_s = bsp / 3600.0 / 60.0  # kts -> deg_lat/sec
            lat += spd_deg_s * math.cos(hdg_r) * dt
            lon += spd_deg_s * math.sin(hdg_r) * dt / math.cos(math.radians(lat))

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
                    lat=round(lat, 7),
                    lon=round(lon, 7),
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
                break
            # Overshoot detection
            if leg.upwind and lat > leg.target.lat + 0.0005:
                break
            if not leg.upwind and lat < leg.target.lat - 0.0005:
                break
            if elapsed > 7200:
                break

        # Mark rounding transition
        if leg_idx < len(config.legs) - 1:
            next_leg = config.legs[leg_idx + 1]
            twd, tws = wind.get(elapsed)
            next_opt_twa, _ = interpolate_polar(tws, next_leg.upwind)
            next_stbd = (leg_idx + 1) % 2 == 0
            next_twa = next_opt_twa if next_stbd else (360.0 - next_opt_twa) % 360
            target_hdg = (twd - next_twa + 360) % 360

            rounding_dur = rng.uniform(15, 25)
            start_hdg = heading
            start_bsp = bsp

            for step in range(int(rounding_dur)):
                t = config.start_time + timedelta(seconds=elapsed)
                twd, tws = wind.get(elapsed)

                p = step / rounding_dur
                smooth_p = 0.5 - 0.5 * math.cos(p * math.pi)
                dh = (target_hdg - start_hdg + 540) % 360 - 180
                heading = (start_hdg + smooth_p * dh) % 360

                bsp = start_bsp * (0.6 + 0.4 * abs(2 * p - 1))

                hdg_r = math.radians(heading)
                spd_deg_s = bsp / 3600.0 / 60.0
                lat += spd_deg_s * math.cos(hdg_r) * dt
                lon += spd_deg_s * math.sin(hdg_r) * dt / math.cos(math.radians(lat))

                twa_actual = (twd - heading + 360) % 360
                aws, awa = apparent_wind(tws, twa_actual, bsp)
                cog = (heading + rng.gauss(0, 0.5)) % 360
                sog = max(0, bsp + rng.gauss(0, 0.1))
                depth = max(_DEPTH_FLOOR, base_depth + rng.gauss(0, 0.3))

                rows.append(
                    SynthRow(
                        ts=t,
                        lat=round(lat, 7),
                        lon=round(lon, 7),
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
