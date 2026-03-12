"""Spatially varying wind model for race simulation.

Produces wind direction and speed as a function of time AND position.
Different locations on the racecourse experience different shifts, puffs,
and pressure — creating realistic divergence between boats on opposite
sides of the course.

All spatial parameters are pre-computed at construction from a seeded RNG.
The ``at()`` query method is pure math with no RNG calls, so results are
deterministic regardless of query order.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class _ShiftEvent:
    """A wind shift that propagates across the course."""

    time: float  # arrival time at reference point (seconds)
    twd_offset: float  # direction offset from base TWD (degrees)
    tws: float  # wind speed at this shift point (knots)


@dataclass(frozen=True)
class _Puff:
    """A traveling blob of increased or decreased wind pressure."""

    birth_time: float  # seconds from start
    birth_lat: float  # starting position
    birth_lon: float
    travel_hdg: float  # direction of travel (degrees true)
    travel_speed_nm_s: float  # speed in nm/sec
    radius_nm: float  # Gaussian sigma (nm)
    intensity_kts: float  # TWS change at center (positive = puff, negative = lull)
    lifetime_s: float  # how long the puff lasts


@dataclass(frozen=True)
class _GradientPhase:
    """A phase of the direction gradient oscillation."""

    time: float  # start time (seconds)
    magnitude_deg_per_nm: float  # degrees of TWD shift per nm cross-course
    axis_deg: float  # gradient axis direction (degrees true)


class WindField:
    """Spatially varying wind model.

    Core interface::

        field = WindField(base_twd=180, ref_lat=47.63, ref_lon=-122.40, seed=42)
        twd, tws = field.at(elapsed_s=300.0, lat=47.635, lon=-122.405)

    The model layers three spatial effects on top of a temporal shift timeline:

    1. **Shift propagation** — shifts arrive at the reference point first and
       propagate crosswind at 8–15 kts, so one side of the course sees a shift
       before the other.

    2. **Traveling puffs** — Gaussian blobs of increased/decreased pressure
       (200–500 m radius) that travel roughly downwind across the course.

    3. **Direction gradient** — a slowly oscillating left-to-right TWD gradient
       of 3–10° per nm, creating persistent side-of-course advantage.
    """

    def __init__(
        self,
        base_twd: float = 0.0,
        tws_low: float = 8.0,
        tws_high: float = 14.0,
        duration_s: float = 7200.0,
        shift_interval: tuple[float, float] = (600.0, 1200.0),
        shift_magnitude: tuple[float, float] = (5.0, 14.0),
        ref_lat: float = 47.63,
        ref_lon: float = -122.40,
        seed: int | None = None,
    ) -> None:
        self.base_twd = base_twd
        self._ref_lat = ref_lat
        self._ref_lon = ref_lon
        self._cos_ref = math.cos(math.radians(ref_lat))

        rng = random.Random(seed)
        self._shifts = _build_shifts(
            rng,
            tws_low,
            tws_high,
            duration_s,
            shift_interval,
            shift_magnitude,
        )
        self._propagation_speed_nm_s = rng.uniform(8.0, 15.0) / 3600.0  # kts -> nm/s
        self._propagation_axis = (base_twd + 90.0) % 360.0  # perpendicular to wind
        self._puffs = _build_puffs(rng, base_twd, ref_lat, ref_lon, duration_s, tws_low, tws_high)
        self._gradients = _build_gradients(rng, base_twd, duration_s)
        # Small-scale noise seeds — deterministic per-query via position hash
        self._noise_seed = rng.randint(0, 2**31)

    def at(self, elapsed_s: float, lat: float, lon: float) -> tuple[float, float]:
        """Return (twd, tws) at the given time and position.

        Deterministic: same (elapsed_s, lat, lon) always returns the same result.
        """
        # 1. Spatial offset from reference point
        dlat_nm = (lat - self._ref_lat) * 60.0
        dlon_nm = (lon - self._ref_lon) * 60.0 * self._cos_ref
        # Project onto propagation axis (cross-course distance)
        # Compass convention: unit vector = (sin(θ), cos(θ)) in (east, north)
        axis_r = math.radians(self._propagation_axis)
        cross_nm = dlon_nm * math.sin(axis_r) + dlat_nm * math.cos(axis_r)

        # 2. Shift propagation delay — shifts arrive later at positions
        #    further along the propagation axis
        if self._propagation_speed_nm_s > 0:
            delay_s = cross_nm / self._propagation_speed_nm_s
        else:
            delay_s = 0.0
        local_t = elapsed_s - delay_s

        # 3. Temporal shift interpolation (same as old WindModel but at local_t)
        twd_off, tws = _interp_shifts(self._shifts, local_t)

        # 4. Direction gradient
        grad_off = _eval_gradient(self._gradients, elapsed_s, cross_nm)
        twd_off += grad_off

        # 5. Traveling puffs
        puff_delta = _eval_puffs(self._puffs, elapsed_s, lat, lon, self._cos_ref)
        tws += puff_delta

        # 6. Small-scale noise (deterministic from position + time)
        noise_twd, noise_tws = _spatial_noise(
            self._noise_seed,
            elapsed_s,
            lat,
            lon,
        )
        twd_off += noise_twd
        tws += noise_tws

        twd = (self.base_twd + twd_off) % 360
        tws = max(4.0, tws)
        return twd, tws


# ---------------------------------------------------------------------------
# Builder helpers (called once at construction with seeded RNG)
# ---------------------------------------------------------------------------


def _build_shifts(
    rng: random.Random,
    tws_lo: float,
    tws_hi: float,
    dur: float,
    interval: tuple[float, float],
    magnitude: tuple[float, float],
) -> list[_ShiftEvent]:
    shifts: list[_ShiftEvent] = []
    t = 0.0
    offset = 0.0
    tws = rng.uniform(tws_lo, tws_hi)
    shifts.append(_ShiftEvent(t, offset, tws))
    while t < dur:
        t += rng.uniform(*interval)
        mag = rng.uniform(*magnitude)
        direction = 1 if rng.random() > 0.5 else -1
        offset += direction * mag
        offset = max(-25.0, min(25.0, offset))
        tws = rng.uniform(tws_lo, tws_hi)
        shifts.append(_ShiftEvent(t, offset, tws))
    return shifts


def _build_puffs(
    rng: random.Random,
    base_twd: float,
    ref_lat: float,
    ref_lon: float,
    duration_s: float,
    tws_lo: float,
    tws_hi: float,
) -> list[_Puff]:
    """Generate traveling puffs/lulls for the session."""
    puffs: list[_Puff] = []
    cos_ref = math.cos(math.radians(ref_lat))
    # One puff every 60-180 seconds
    t = 0.0
    while t < duration_s:
        t += rng.uniform(60.0, 180.0)
        # Puff spawns upwind, 0.5-1.5 nm from reference
        spawn_dist_nm = rng.uniform(0.5, 1.5)
        spawn_angle = base_twd + rng.uniform(-30, 30)  # roughly upwind
        spawn_r = math.radians(spawn_angle)
        spawn_lat = ref_lat + spawn_dist_nm / 60.0 * math.cos(spawn_r)
        spawn_lon = ref_lon + spawn_dist_nm / 60.0 * math.sin(spawn_r) / cos_ref

        travel_hdg = (base_twd + 180.0 + rng.uniform(-15, 15)) % 360.0  # roughly downwind
        travel_speed_kts = rng.uniform(5.0, 15.0)
        radius_nm = rng.uniform(0.10, 0.27)  # 200-500m
        # Puffs are 2-5 kts intensity; lulls are less common
        intensity = rng.uniform(1.5, 4.0) if rng.random() < 0.7 else -rng.uniform(1.0, 3.0)
        lifetime = rng.uniform(120.0, 360.0)

        puffs.append(
            _Puff(
                birth_time=t,
                birth_lat=spawn_lat,
                birth_lon=spawn_lon,
                travel_hdg=travel_hdg,
                travel_speed_nm_s=travel_speed_kts / 3600.0,
                radius_nm=radius_nm,
                intensity_kts=intensity,
                lifetime_s=lifetime,
            )
        )
    return puffs


def _build_gradients(
    rng: random.Random,
    base_twd: float,
    duration_s: float,
) -> list[_GradientPhase]:
    """Generate gradient phases that oscillate over the session."""
    phases: list[_GradientPhase] = []
    t = 0.0
    while t < duration_s:
        mag = rng.uniform(3.0, 10.0)  # deg per nm cross-course
        axis = (base_twd + 90.0 + rng.uniform(-20, 20)) % 360.0
        phases.append(_GradientPhase(t, mag, axis))
        t += rng.uniform(300.0, 900.0)  # new gradient phase every 5-15 min
    return phases


# ---------------------------------------------------------------------------
# Query-time evaluation (pure math, no RNG)
# ---------------------------------------------------------------------------


def _interp_shifts(shifts: list[_ShiftEvent], t: float) -> tuple[float, float]:
    """Interpolate the shift timeline at time *t*. Returns (twd_offset, tws)."""
    prev = shifts[0]
    nxt = shifts[-1]
    for i, s in enumerate(shifts):
        if s.time <= t:
            prev = s
            nxt = shifts[min(i + 1, len(shifts) - 1)]
        else:
            break

    if prev.time == nxt.time:
        return prev.twd_offset, prev.tws

    frac = min(1.0, max(0.0, (t - prev.time) / (nxt.time - prev.time)))
    smooth = 0.5 - 0.5 * math.cos(frac * math.pi)
    twd_off = prev.twd_offset + smooth * (nxt.twd_offset - prev.twd_offset)
    tws = prev.tws + smooth * (nxt.tws - prev.tws)
    return twd_off, tws


def _eval_gradient(
    phases: list[_GradientPhase],
    elapsed_s: float,
    cross_nm: float,
) -> float:
    """Evaluate the direction gradient contribution at a cross-course distance."""
    if not phases:
        return 0.0
    # Find the active phase
    active = phases[0]
    next_phase = phases[0]
    for i, p in enumerate(phases):
        if p.time <= elapsed_s:
            active = p
            next_phase = phases[min(i + 1, len(phases) - 1)]
        else:
            break

    # Smooth blend between phases
    if active.time == next_phase.time:
        mag = active.magnitude_deg_per_nm
    else:
        frac = min(1.0, max(0.0, (elapsed_s - active.time) / (next_phase.time - active.time)))
        smooth = 0.5 - 0.5 * math.cos(frac * math.pi)
        mag = active.magnitude_deg_per_nm + smooth * (
            next_phase.magnitude_deg_per_nm - active.magnitude_deg_per_nm
        )

    return cross_nm * mag


def _eval_puffs(
    puffs: list[_Puff],
    elapsed_s: float,
    lat: float,
    lon: float,
    cos_ref: float,
) -> float:
    """Sum puff contributions at (elapsed_s, lat, lon)."""
    total = 0.0
    for p in puffs:
        age = elapsed_s - p.birth_time
        if age < 0 or age > p.lifetime_s:
            continue
        # Current puff center position
        travel_r = math.radians(p.travel_hdg)
        dist_nm = p.travel_speed_nm_s * age
        center_lat = p.birth_lat + dist_nm / 60.0 * math.cos(travel_r)
        center_lon = p.birth_lon + dist_nm / 60.0 * math.sin(travel_r) / cos_ref

        # Distance from query point to puff center
        dlat = (lat - center_lat) * 60.0
        dlon = (lon - center_lon) * 60.0 * cos_ref
        dist_sq = dlat * dlat + dlon * dlon
        sigma_sq = p.radius_nm * p.radius_nm

        # Gaussian envelope
        envelope = math.exp(-0.5 * dist_sq / sigma_sq) if sigma_sq > 0 else 0.0
        # Fade in/out at birth and death
        fade = 1.0
        if age < 30.0:
            fade = age / 30.0
        elif age > p.lifetime_s - 30.0:
            fade = (p.lifetime_s - age) / 30.0
        total += p.intensity_kts * envelope * fade
    return total


def _spatial_noise(
    seed: int,
    elapsed_s: float,
    lat: float,
    lon: float,
) -> tuple[float, float]:
    """Deterministic small-scale noise at a given position and time.

    Uses a hash-based approach so the result depends only on inputs,
    not on call order.
    """
    # Quantize inputs to create a stable grid with smooth transitions.
    # Use 10-second time cells and ~100m spatial cells.
    t_cell = elapsed_s / 10.0
    lat_cell = lat * 600.0  # ~100m cells
    lon_cell = lon * 600.0

    # Integer cell indices
    ti = int(math.floor(t_cell))
    li = int(math.floor(lat_cell))
    oi = int(math.floor(lon_cell))

    # Fractional positions within cells (for smoothing)
    tf = t_cell - ti
    lf = lat_cell - li
    of_ = lon_cell - oi

    # Smooth step
    tf = tf * tf * (3 - 2 * tf)
    lf = lf * lf * (3 - 2 * lf)
    of_ = of_ * of_ * (3 - 2 * of_)

    # Trilinear interpolation of noise at 8 corners
    twd_noise = 0.0
    tws_noise = 0.0
    for dt in (0, 1):
        for dl in (0, 1):
            for do in (0, 1):
                h = hash((seed, ti + dt, li + dl, oi + do))
                # Extract two pseudo-uniform values from hash
                v_twd = ((h >> 0) & 0xFFFF) / 65535.0 * 2.0 - 1.0  # [-1, 1]
                v_tws = ((h >> 16) & 0xFFFF) / 65535.0 * 2.0 - 1.0
                wt = tf if dt else (1 - tf)
                wl = lf if dl else (1 - lf)
                wo = of_ if do else (1 - of_)
                w = wt * wl * wo
                twd_noise += v_twd * w
                tws_noise += v_tws * w

    return twd_noise * 1.5, tws_noise * 0.3  # σ≈1.5° direction, σ≈0.3kts speed
