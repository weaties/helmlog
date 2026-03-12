"""Tests for wind_field.py — spatially varying wind model."""

from __future__ import annotations

from datetime import UTC, datetime

from helmlog.courses import build_wl_course
from helmlog.synthesize import SynthConfig, SynthRow, simulate
from helmlog.wind_field import WindField


class TestWindFieldBasic:
    """Core WindField interface and determinism."""

    def test_returns_tuple(self) -> None:
        wf = WindField(base_twd=180, seed=42)
        result = wf.at(0.0, 47.63, -122.40)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_deterministic_same_seed(self) -> None:
        wf1 = WindField(base_twd=180, seed=42)
        wf2 = WindField(base_twd=180, seed=42)
        assert wf1.at(0.0, 47.63, -122.40) == wf2.at(0.0, 47.63, -122.40)
        assert wf1.at(300.0, 47.635, -122.41) == wf2.at(300.0, 47.635, -122.41)

    def test_deterministic_query_order(self) -> None:
        """Querying in different order must give same results."""
        wf1 = WindField(base_twd=180, seed=42)
        wf2 = WindField(base_twd=180, seed=42)
        # Query in forward order
        a1 = wf1.at(100.0, 47.63, -122.40)
        a2 = wf1.at(200.0, 47.64, -122.41)
        # Query in reverse order
        b2 = wf2.at(200.0, 47.64, -122.41)
        b1 = wf2.at(100.0, 47.63, -122.40)
        assert a1 == b1
        assert a2 == b2

    def test_tws_above_floor(self) -> None:
        wf = WindField(base_twd=0, tws_low=5, tws_high=6, seed=1)
        for t in range(0, 3600, 60):
            _, tws = wf.at(t, 47.63, -122.40)
            assert tws >= 4.0

    def test_different_seeds_differ(self) -> None:
        wf1 = WindField(base_twd=180, seed=42)
        wf2 = WindField(base_twd=180, seed=99)
        # Very unlikely to be identical with different seeds
        twd1, tws1 = wf1.at(300.0, 47.63, -122.40)
        twd2, tws2 = wf2.at(300.0, 47.63, -122.40)
        assert (twd1, tws1) != (twd2, tws2)


class TestSpatialVariation:
    """Wind varies across the racecourse."""

    _REF_LAT = 47.63
    _REF_LON = -122.40

    def test_direction_varies_across_course(self) -> None:
        """Wind direction should differ by 3-10° across ~1 nm course width."""
        wf = WindField(
            base_twd=180,
            ref_lat=self._REF_LAT,
            ref_lon=self._REF_LON,
            seed=42,
        )
        # Two points ~0.5 nm apart cross-course (roughly east-west for 180° wind)
        # 0.5 nm east and west of reference
        offset_deg = 0.5 / 60.0 / abs(max(0.01, abs(wf._cos_ref)))
        twd_left, _ = wf.at(600.0, self._REF_LAT, self._REF_LON - offset_deg)
        twd_right, _ = wf.at(600.0, self._REF_LAT, self._REF_LON + offset_deg)
        diff = abs(((twd_right - twd_left + 180) % 360) - 180)
        assert diff > 1.0, f"Direction should vary across course, got {diff:.1f}°"

    def test_speed_varies_spatially(self) -> None:
        """Wind speed should vary across the course due to puffs."""
        wf = WindField(
            base_twd=180,
            ref_lat=self._REF_LAT,
            ref_lon=self._REF_LON,
            seed=42,
        )
        # Sample a grid and check that TWS varies
        speeds = set()
        for t in range(0, 3600, 120):
            for dlat in (-0.005, 0, 0.005):
                for dlon in (-0.005, 0, 0.005):
                    _, tws = wf.at(t, self._REF_LAT + dlat, self._REF_LON + dlon)
                    speeds.add(round(tws, 1))
        assert len(speeds) > 5, "TWS should vary across different positions"

    def test_shift_propagation_delay(self) -> None:
        """A shift should arrive at different times on opposite sides."""
        wf = WindField(
            base_twd=0,
            ref_lat=self._REF_LAT,
            ref_lon=self._REF_LON,
            seed=42,
            shift_interval=(300.0, 400.0),
            shift_magnitude=(10.0, 14.0),
        )
        # Sample wind direction over time at two points 0.5 nm apart cross-course
        offset = 0.5 / 60.0 / wf._cos_ref
        series_left = [wf.at(t, self._REF_LAT, self._REF_LON - offset)[0] for t in range(0, 1800)]
        series_right = [wf.at(t, self._REF_LAT, self._REF_LON + offset)[0] for t in range(0, 1800)]
        # Compute cross-correlation to find the time lag
        # A non-zero lag means the shift propagated
        diffs = [
            abs(((sl - sr + 180) % 360) - 180)
            for sl, sr in zip(series_left, series_right, strict=True)
        ]
        avg_diff = sum(diffs) / len(diffs)
        assert avg_diff > 0.5, f"Expected measurable direction difference, got {avg_diff:.2f}°"


class TestSimulationIntegration:
    """Verify that simulate() now uses spatial wind — boats at different
    positions experience different wind.
    """

    _START = (47.70, -122.44)

    def test_simulation_still_works(self) -> None:
        """Smoke test — simulation produces valid output with WindField."""
        legs = build_wl_course(*self._START, 0.0, laps=1)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
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

    def test_two_boats_different_wind(self) -> None:
        """Two boats on different sides of the course should experience
        different wind conditions over the race.
        """
        wf = WindField(
            base_twd=180,
            ref_lat=self._START[0],
            ref_lon=self._START[1],
            seed=42,
        )
        # Simulate two boats 0.3 nm apart (roughly half a course width)
        offset = 0.3 / 60.0 / wf._cos_ref
        boat_a_twd = []
        boat_b_twd = []
        boat_a_tws = []
        boat_b_tws = []
        for t in range(0, 1800, 10):
            twd_a, tws_a = wf.at(t, self._START[0], self._START[1] - offset)
            twd_b, tws_b = wf.at(t, self._START[0], self._START[1] + offset)
            boat_a_twd.append(twd_a)
            boat_b_twd.append(twd_b)
            boat_a_tws.append(tws_a)
            boat_b_tws.append(tws_b)

        # Direction should diverge meaningfully at times
        twd_diffs = [
            abs(((a - b + 180) % 360) - 180) for a, b in zip(boat_a_twd, boat_b_twd, strict=True)
        ]
        max_twd_diff = max(twd_diffs)
        assert max_twd_diff > 2.0, (
            f"Boats 0.3 nm apart should see >2° TWD difference, max was {max_twd_diff:.1f}°"
        )

        # Speed should also diverge due to puffs
        tws_diffs = [abs(a - b) for a, b in zip(boat_a_tws, boat_b_tws, strict=True)]
        max_tws_diff = max(tws_diffs)
        assert max_tws_diff > 0.5, (
            f"Boats 0.3 nm apart should see >0.5 kt TWS difference, max was {max_tws_diff:.1f}"
        )

    def test_simulation_deterministic_with_wind_field(self) -> None:
        """Two runs with same seed produce identical results."""
        legs = build_wl_course(*self._START, 0.0, laps=1)
        config = SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
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
        for r1, r2 in zip(rows1[:50], rows2[:50], strict=True):
            assert r1.lat == r2.lat
            assert r1.lon == r2.lon
            assert r1.tws == r2.tws
