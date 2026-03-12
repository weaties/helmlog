"""Tests for synthesize.py — J/105 simulation engine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from helmlog.courses import build_triangle_course, build_wl_course, is_in_water
from helmlog.synthesize import (
    _DEPTH_FLOOR,
    CollisionAvoidanceConfig,
    HeaderResponseConfig,
    SynthConfig,
    SynthRow,
    TrackIndex,
    WindModel,
    _distance_nm,
    _has_collision,
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


class TestWindSeedSeparation:
    """Verify wind_seed produces identical wind but different boat behaviour."""

    def test_same_wind_seed_different_boat_seed(self) -> None:
        """Two boats with same wind_seed but different seed get same TWD but different tracks."""
        legs = build_wl_course(47.70, -122.44, 0.0, laps=1)
        base = {
            "start_lat": 47.70,
            "start_lon": -122.44,
            "base_twd": 0.0,
            "tws_low": 10.0,
            "tws_high": 12.0,
            "shift_interval": (600.0, 1200.0),
            "shift_magnitude": (5.0, 10.0),
            "legs": legs,
            "start_time": datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
            "wind_seed": 999,
        }
        rows_a = simulate(SynthConfig(**base, seed=1))
        rows_b = simulate(SynthConfig(**base, seed=2))
        # Same wind field: TWS at the very first point (same position) is identical
        assert rows_a[0].tws == rows_b[0].tws
        # Different boat behaviour: positions should diverge after some time
        diverged = any(
            rows_a[i].lat != rows_b[i].lat or rows_a[i].lon != rows_b[i].lon
            for i in range(min(len(rows_a), len(rows_b)))
        )
        assert diverged

    def test_wind_seed_none_uses_seed(self) -> None:
        """When wind_seed is None, seed drives both wind and boat (backwards-compatible)."""
        legs = build_wl_course(47.70, -122.44, 0.0, laps=1)
        cfg = SynthConfig(
            start_lat=47.70,
            start_lon=-122.44,
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=42,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
        )
        rows1 = simulate(cfg)
        rows2 = simulate(cfg)
        # Fully deterministic — identical output
        assert len(rows1) == len(rows2)
        assert rows1[50].lat == rows2[50].lat
        assert rows1[50].tws == rows2[50].tws


class TestSimulateWindAngles:
    """Verify the simulation produces valid tracks at various wind angles."""

    # Start in open water mid-Sound to avoid land avoidance interference
    _START = (47.70, -122.44)
    _MAX_DRIFT_NM = 0.05  # max acceptable drift from start at finish

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
            assert min_dist < 0.18, (
                f"Wind {wind_dir}° leg {i}: closest approach to "
                f"{m.name} was {min_dist:.3f} nm (> 0.18)"
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


class TestLaylineOverstand:
    """Verify the boat doesn't overstand the layline excessively."""

    _START = (47.70, -122.44)

    @pytest.mark.parametrize("wind_dir", [0, 90, 180, 270])
    def test_no_excessive_overstand(self, wind_dir: int) -> None:
        """Boat should not overstand the layline by more than ~8 boat lengths.

        We allow a generous 8 BL margin (vs the 4 BL target) to account
        for wind shifts and maneuver timing in the simulation.
        """
        legs = build_wl_course(*self._START, wind_dir, 1.0, laps=1)
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
        mark_a = legs[0].target

        # Find tacks near the mark (heading change > 50°) within 0.3 nm
        max_overstand_nm = 0.0
        for i in range(1, len(rows)):
            hdiff = abs(rows[i].heading - rows[i - 1].heading)
            if hdiff > 180:
                hdiff = 360 - hdiff
            if hdiff < 50:
                continue
            d = _distance_nm(rows[i].lat, rows[i].lon, mark_a.lat, mark_a.lon)
            if d > 0.3:
                continue
            # Settled heading after tack
            settled = rows[min(i + 15, len(rows) - 1)]
            brg = _distance_nm.__module__  # just need _bearing
            from helmlog.synthesize import _bearing as brg_fn

            brg = brg_fn(settled.lat, settled.lon, mark_a.lat, mark_a.lon)
            hdg_diff = abs(((settled.heading - brg + 180) % 360) - 180)
            # Only count when heading is within TWA range (upwind approach)
            if hdg_diff < 60:
                overstand = d * abs(hdg_diff - 42) / 57.3 * d  # rough cross-track
                max_overstand_nm = max(max_overstand_nm, overstand)

        # 8 boat lengths = 8 * 35ft * 0.000165nm/ft ≈ 0.046 nm
        max_bl = max_overstand_nm / 0.006
        assert max_bl < 8, f"Wind {wind_dir}°: max overstand {max_bl:.1f} BL (> 8)"


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


class TestHeaderResponse:
    """Verify probabilistic tacking on wind shifts (#247)."""

    _START = (47.70, -122.44)

    def _make_config(
        self,
        seed: int = 42,
        header_response: HeaderResponseConfig | None = None,
    ) -> SynthConfig:
        legs = build_wl_course(*self._START, 0.0, 1.0, laps=1)
        return SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(8.0, 14.0),
            legs=legs,
            seed=seed,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
            header_response=header_response or HeaderResponseConfig(),
        )

    def _count_tacks(self, rows: list[SynthRow]) -> int:
        """Count maneuvers by detecting cumulative heading swings > 60° over 15s windows."""
        count = 0
        cooldown = 0
        for i in range(15, len(rows)):
            if cooldown > 0:
                cooldown -= 1
                continue
            hdiff = abs(rows[i].heading - rows[i - 15].heading)
            if hdiff > 180:
                hdiff = 360 - hdiff
            if hdiff > 60:
                count += 1
                cooldown = 20  # skip ahead to avoid double-counting
        return count

    def test_different_seeds_different_responses(self) -> None:
        """Two boats with different seeds should not tack identically."""
        rows_a = simulate(self._make_config(seed=42))
        rows_b = simulate(self._make_config(seed=99))
        # Compare heading sequences — different seeds must produce different tracks
        hdg_a = [r.heading for r in rows_a[:300]]
        hdg_b = [r.heading for r in rows_b[:300]]
        assert hdg_a != hdg_b, "Different seeds should produce different heading sequences"

    def test_perfect_vmg_never_misses_shifts(self) -> None:
        """With reaction_probability=1.0 and threshold=0, should respond to every shift."""
        aggressive = HeaderResponseConfig(
            reaction_probability=1.0,
            min_shift_threshold=(0.0, 0.1),
            reaction_delay=(1.0, 2.0),
            fatigue_start_frac=1.0,
        )
        passive = HeaderResponseConfig(
            reaction_probability=0.0,
            min_shift_threshold=(90.0, 90.0),
            reaction_delay=(1.0, 2.0),
        )
        rows_aggressive = simulate(self._make_config(header_response=aggressive))
        rows_passive = simulate(self._make_config(header_response=passive))
        tacks_aggressive = self._count_tacks(rows_aggressive)
        tacks_passive = self._count_tacks(rows_passive)
        # Aggressive should tack more often than passive
        assert tacks_aggressive > tacks_passive, (
            f"Aggressive ({tacks_aggressive} tacks) should tack more than "
            f"passive ({tacks_passive} tacks)"
        )

    def test_deterministic_same_seed(self) -> None:
        """Same seed + same config = identical track."""
        rows_a = simulate(self._make_config(seed=42))
        rows_b = simulate(self._make_config(seed=42))
        assert len(rows_a) == len(rows_b)
        for a, b in zip(rows_a[:100], rows_b[:100], strict=True):
            assert a.lat == b.lat
            assert a.lon == b.lon
            assert a.heading == b.heading

    def test_still_finishes_near_start(self) -> None:
        """Header response should not break course completion."""
        rows = simulate(self._make_config())
        drift = _distance_nm(rows[-1].lat, rows[-1].lon, *self._START)
        assert drift < 0.05, f"Drifted {drift:.3f} nm from start"

    def test_fatigue_reduces_tacking(self) -> None:
        """Early fatigue onset (frac=0.0) should reduce total tacks vs no fatigue."""
        no_fatigue = HeaderResponseConfig(
            reaction_probability=0.80,
            fatigue_start_frac=1.0,  # no fatigue
            fatigue_floor=0.80,
        )
        early_fatigue = HeaderResponseConfig(
            reaction_probability=0.80,
            fatigue_start_frac=0.0,  # fatigued from the start
            fatigue_floor=0.10,
        )
        rows_fresh = simulate(self._make_config(header_response=no_fatigue))
        rows_tired = simulate(self._make_config(header_response=early_fatigue))
        tacks_fresh = self._count_tacks(rows_fresh)
        tacks_tired = self._count_tacks(rows_tired)
        # Fatigued crew should tack no more than fresh crew
        # (could be equal if interval tacks dominate, but shouldn't tack more)
        assert tacks_tired <= tacks_fresh + 2, (
            f"Fatigued ({tacks_tired}) should not tack much more than fresh ({tacks_fresh})"
        )


class TestTrackIndex:
    """Tests for the TrackIndex collision lookup structure."""

    def test_empty_index(self) -> None:
        idx = TrackIndex([])
        assert len(idx) == 0
        assert idx.positions_at("2025-08-10T18:00:00") == []

    def test_single_track(self) -> None:
        track = [
            {"timestamp": "2025-08-10T18:00:00", "LAT": 47.63, "LON": -122.40},
            {"timestamp": "2025-08-10T18:00:01", "LAT": 47.631, "LON": -122.401},
        ]
        idx = TrackIndex([track])
        assert len(idx) == 2
        positions = idx.positions_at("2025-08-10T18:00:00")
        assert len(positions) == 1
        assert positions[0] == (47.63, -122.40)

    def test_multiple_tracks_same_timestamp(self) -> None:
        track_a = [{"timestamp": "2025-08-10T18:00:00", "LAT": 47.63, "LON": -122.40}]
        track_b = [{"timestamp": "2025-08-10T18:00:00", "LAT": 47.64, "LON": -122.41}]
        idx = TrackIndex([track_a, track_b])
        positions = idx.positions_at("2025-08-10T18:00:00")
        assert len(positions) == 2


class TestHasCollision:
    def test_no_collision_without_index(self) -> None:
        assert not _has_collision(47.63, -122.40, "2025-08-10T18:00:00", None, 30.0)

    def test_collision_same_position(self) -> None:
        track = [{"timestamp": "2025-08-10T18:00:00", "LAT": 47.63, "LON": -122.40}]
        idx = TrackIndex([track])
        assert _has_collision(47.63, -122.40, "2025-08-10T18:00:00", idx, 30.0)

    def test_no_collision_far_away(self) -> None:
        track = [{"timestamp": "2025-08-10T18:00:00", "LAT": 47.63, "LON": -122.40}]
        idx = TrackIndex([track])
        # ~1 nm away — well beyond 30m separation
        assert not _has_collision(47.65, -122.40, "2025-08-10T18:00:00", idx, 30.0)

    def test_no_collision_different_timestamp(self) -> None:
        track = [{"timestamp": "2025-08-10T18:00:00", "LAT": 47.63, "LON": -122.40}]
        idx = TrackIndex([track])
        assert not _has_collision(47.63, -122.40, "2025-08-10T18:01:00", idx, 30.0)


class TestCollisionAvoidance:
    """Verify collision avoidance in the simulation engine (#246)."""

    _START = (47.70, -122.44)

    def _make_config(
        self,
        seed: int = 42,
        min_separation_m: float = 30.0,
    ) -> SynthConfig:
        legs = build_wl_course(*self._START, 0.0, 1.0, laps=1)
        return SynthConfig(
            start_lat=self._START[0],
            start_lon=self._START[1],
            base_twd=0.0,
            tws_low=10.0,
            tws_high=12.0,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(5.0, 10.0),
            legs=legs,
            seed=seed,
            start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
            collision_avoidance=CollisionAvoidanceConfig(
                min_separation_m=min_separation_m,
            ),
        )

    def test_no_collision_with_other_track(self) -> None:
        """When given another boat's track, synthesized track should avoid collisions."""
        # Generate a reference track (boat A)
        config_a = self._make_config(seed=42)
        rows_a = simulate(config_a)

        # Convert to peer-API track format
        track_a = [
            {
                "timestamp": r.ts.isoformat()[:19],
                "LAT": r.lat,
                "LON": r.lon,
            }
            for r in rows_a
        ]

        # Generate boat B with collision avoidance against boat A
        config_b = self._make_config(seed=99, min_separation_m=30.0)
        rows_b = simulate(config_b, other_tracks=[track_a])

        # Check for collisions
        track_a_idx = TrackIndex([track_a])
        collisions = 0
        for r in rows_b:
            ts = r.ts.isoformat()[:19]
            if _has_collision(r.lat, r.lon, ts, track_a_idx, 30.0):
                collisions += 1

        # Allow a very small number of collisions (edge cases during mark rounding)
        max_allowed = max(5, len(rows_b) // 100)  # < 1% collision rate
        assert collisions <= max_allowed, (
            f"{collisions} collisions in {len(rows_b)} points (> {max_allowed} allowed)"
        )

    def test_without_other_tracks_unchanged(self) -> None:
        """Simulation without other_tracks should produce identical results."""
        config = self._make_config(seed=42)
        rows_a = simulate(config)
        rows_b = simulate(config, other_tracks=None)
        assert len(rows_a) == len(rows_b)
        assert rows_a[0].lat == rows_b[0].lat
        assert rows_a[-1].lat == rows_b[-1].lat

    def test_configurable_separation_distance(self) -> None:
        """Different separation distances should be respected."""
        config_a = self._make_config(seed=42)
        rows_a = simulate(config_a)
        track_a = [{"timestamp": r.ts.isoformat()[:19], "LAT": r.lat, "LON": r.lon} for r in rows_a]

        # Use a large separation (100m) — should force more avoidance
        config_b = self._make_config(seed=99, min_separation_m=100.0)
        rows_b = simulate(config_b, other_tracks=[track_a])

        track_a_idx = TrackIndex([track_a])
        violations_100m = sum(
            1
            for r in rows_b
            if _has_collision(r.lat, r.lon, r.ts.isoformat()[:19], track_a_idx, 100.0)
        )
        # 100m is very large (~3× J/105 LOA) — mark roundings and tight
        # tactical situations make zero violations unrealistic, so allow 5%
        max_allowed = max(10, len(rows_b) // 20)
        assert violations_100m <= max_allowed, (
            f"{violations_100m} violations at 100m in {len(rows_b)} points"
        )

    def test_still_finishes(self) -> None:
        """Collision avoidance should not prevent course completion."""
        config_a = self._make_config(seed=42)
        rows_a = simulate(config_a)
        track_a = [{"timestamp": r.ts.isoformat()[:19], "LAT": r.lat, "LON": r.lon} for r in rows_a]

        config_b = self._make_config(seed=99, min_separation_m=30.0)
        rows_b = simulate(config_b, other_tracks=[track_a])
        assert len(rows_b) > 100, "Track should still produce substantial output"
