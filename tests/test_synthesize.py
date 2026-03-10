"""Tests for synthesize.py — J/105 simulation engine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from helmlog.courses import build_triangle_course, build_wl_course, is_in_water
from helmlog.synthesize import (
    _DEPTH_FLOOR,
    SynthConfig,
    SynthRow,
    WindModel,
    _distance_nm,
    apparent_wind,
    interpolate_polar,
    simulate,
)


class TestInterpolatePolar:
    def test_exact_entry(self) -> None:
        twa, bsp = interpolate_polar(10.0, upwind=True)
        assert twa == 42.0
        assert bsp == 6.5

    def test_interpolation(self) -> None:
        twa, bsp = interpolate_polar(9.0, upwind=True)
        assert 42.0 < twa < 43.0
        assert 6.0 < bsp < 6.5

    def test_downwind(self) -> None:
        twa, bsp = interpolate_polar(10.0, upwind=False)
        assert twa == 140.0
        assert bsp == 6.5

    def test_clamp_below(self) -> None:
        twa, bsp = interpolate_polar(2.0, upwind=True)
        assert twa == 44.0  # clamped to TWS=6

    def test_clamp_above(self) -> None:
        twa, bsp = interpolate_polar(25.0, upwind=True)
        assert twa == 39.0  # clamped to TWS=16


class TestApparentWind:
    def test_headwind(self) -> None:
        aws, awa = apparent_wind(10.0, 0.0, 6.0)
        # Head-to-wind: AWS = TWS + BSP
        assert aws > 15.0
        assert awa < 5.0  # nearly head-on

    def test_nonzero_angle(self) -> None:
        aws, awa = apparent_wind(10.0, 45.0, 6.0)
        assert aws > 0
        assert 0 < awa < 45.0


class TestWindModel:
    def test_deterministic_with_seed(self) -> None:
        wm1 = WindModel(base_twd=180, seed=42)
        wm2 = WindModel(base_twd=180, seed=42)
        assert wm1.get(0) == wm2.get(0)
        assert wm1.get(300) == wm2.get(300)

    def test_tws_above_floor(self) -> None:
        wm = WindModel(base_twd=0, tws_low=5, tws_high=6, seed=1)
        for t in range(0, 3600, 60):
            _, tws = wm.get(t)
            assert tws >= 4.0


class TestSimulate:
    def test_produces_data(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        assert len(rows) > 100
        assert all(isinstance(r, SynthRow) for r in rows)

    def test_depth_floor(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        for r in rows:
            assert r.depth >= _DEPTH_FLOOR

    def test_timestamps_ascending(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        for i in range(1, len(rows)):
            assert rows[i].ts > rows[i - 1].ts

    def test_deterministic(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        config = SynthConfig(
            start_lat=47.63,
            start_lon=-122.40,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows1 = simulate(config)
        rows2 = simulate(config)
        assert len(rows1) == len(rows2)
        assert rows1[0].lat == rows2[0].lat
        assert rows1[-1].lat == rows2[-1].lat


class TestSimulateWindAngles:
    """Verify the simulation produces valid tracks at various wind angles."""

    # Start in open water mid-Sound to avoid land avoidance interference
    _START = (47.70, -122.44)
    _MAX_DRIFT_NM = 0.35  # max acceptable drift from start at finish

    def _run(self, wind_dir: float, laps: int = 1) -> list[SynthRow]:
        legs = build_wl_course(*self._START, wind_dir, 1.0, laps)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=wind_dir,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        return simulate(config)

    @pytest.mark.parametrize("wind_dir", [0, 45, 90, 135, 180, 225, 270, 315])
    def test_finishes_near_start(self, wind_dir: int) -> None:
        """Track should finish within MAX_DRIFT_NM of the start position."""
        rows = self._run(wind_dir)
        drift = _distance_nm(rows[-1].lat, rows[-1].lon, *self._START)
        assert drift < self._MAX_DRIFT_NM, (
            f"Wind {wind_dir}°: finish drifted {drift:.3f} nm from start"
        )

    @pytest.mark.parametrize("wind_dir", [0, 90, 180, 270])
    def test_rounds_marks_sequentially(self, wind_dir: int) -> None:
        """Boat should pass within 0.15 nm of each mark in leg order."""
        legs = build_wl_course(*self._START, wind_dir, 1.0, 1)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=wind_dir,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        search_from = 0
        for i, leg in enumerate(legs):
            m = leg.target
            min_dist = float("inf")
            min_idx = search_from
            for j in range(search_from, len(rows)):
                d = _distance_nm(rows[j].lat, rows[j].lon, m.lat, m.lon)
                if d < min_dist:
                    min_dist = d
                    min_idx = j
                if d > min_dist + 0.1 and min_dist < 0.15:
                    break
            assert min_dist < 0.15, (
                f"Wind {wind_dir}° leg {i}: closest approach to "
                f"{m.name} was {min_dist:.3f} nm (> 0.15)"
            )
            search_from = min_idx + 1

    @pytest.mark.parametrize("wind_dir", [0, 90, 180, 270])
    def test_triangle_finishes_near_start(self, wind_dir: int) -> None:
        """Triangle course should also finish near start."""
        legs = build_triangle_course(*self._START, wind_dir, 1.0)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=wind_dir,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        drift = _distance_nm(rows[-1].lat, rows[-1].lon, *self._START)
        assert drift < self._MAX_DRIFT_NM, (
            f"Triangle wind {wind_dir}°: drifted {drift:.3f} nm from start"
        )


class TestLandAvoidance:
    """Verify the simulation avoids sailing over land."""

    def test_track_stays_in_water(self) -> None:
        """Near-shore start: every position must be in navigable water."""
        # Start near Magnolia where land avoidance will be exercised
        start = (47.64, -122.42)
        legs = build_wl_course(*start, 0.0, 0.8, laps=1)
        config = SynthConfig(
            start_lat=start[0],
            start_lon=start[1],
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows = simulate(config)
        on_land = [(i, r.lat, r.lon) for i, r in enumerate(rows) if not is_in_water(r.lat, r.lon)]
        assert on_land == [], (
            f"{len(on_land)} points on land, first: t={on_land[0][0]}s "
            f"({on_land[0][1]:.5f}, {on_land[0][2]:.5f})"
        )
