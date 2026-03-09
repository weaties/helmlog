"""Tests for helmlog.synthesize — J/105 race simulation engine."""

from __future__ import annotations

from datetime import UTC, datetime

from helmlog.courses import build_wl_course
from helmlog.synthesize import (
    SynthConfig,
    WindModel,
    apparent_wind,
    interpolate_polar,
    simulate,
)

# ---------------------------------------------------------------------------
# Polar interpolation
# ---------------------------------------------------------------------------


def test_interpolate_polar_known_values() -> None:
    """At 10 kts TWS, upwind TWA should be ~42° and BSP ~6.5 kts."""
    twa, bsp = interpolate_polar(10.0, upwind=True)
    assert 41.0 <= twa <= 43.0
    assert 6.3 <= bsp <= 6.7


def test_interpolate_polar_downwind() -> None:
    twa, bsp = interpolate_polar(10.0, upwind=False)
    assert 138.0 <= twa <= 142.0
    assert 6.3 <= bsp <= 6.7


def test_interpolate_polar_clamps_low() -> None:
    """TWS below table minimum should clamp to lowest entry."""
    twa, bsp = interpolate_polar(2.0, upwind=True)
    assert twa == 44.0  # lowest entry TWS=6 has TWA=44
    assert bsp == 5.2


def test_interpolate_polar_clamps_high() -> None:
    twa, bsp = interpolate_polar(30.0, upwind=True)
    assert twa == 39.0  # highest entry TWS=16 has TWA=39
    assert bsp == 7.3


# ---------------------------------------------------------------------------
# Apparent wind
# ---------------------------------------------------------------------------


def test_apparent_wind_head_to_wind() -> None:
    """TWA=0 (head to wind): AWS should equal TWS + BSP."""
    aws, awa = apparent_wind(10.0, 0.0, 6.0)
    assert abs(aws - 16.0) < 0.01
    assert abs(awa) < 0.01


def test_apparent_wind_running() -> None:
    """TWA=180 (dead downwind), BSP < TWS: AWS = TWS - BSP, AWA = 180."""
    aws, awa = apparent_wind(10.0, 180.0, 6.0)
    assert abs(aws - 4.0) < 0.01
    assert abs(awa - 180.0) < 0.01


def test_apparent_wind_upwind() -> None:
    """Upwind at TWA=42: apparent wind should be forward and stronger."""
    aws, awa = apparent_wind(10.0, 42.0, 6.5)
    assert aws > 10.0  # apparent stronger than true
    assert 20.0 < awa < 35.0  # apparent comes from more ahead


# ---------------------------------------------------------------------------
# Wind model
# ---------------------------------------------------------------------------


def test_wind_model_deterministic() -> None:
    """Same seed should produce identical output."""
    w1 = WindModel(base_twd=0.0, tws_low=8.0, tws_high=14.0, seed=42)
    w2 = WindModel(base_twd=0.0, tws_low=8.0, tws_high=14.0, seed=42)
    for t in [0, 100, 500, 1000]:
        assert w1.get(t) == w2.get(t)


def test_wind_model_tws_range() -> None:
    """TWS should stay above the 4.0 floor."""
    w = WindModel(base_twd=0.0, tws_low=8.0, tws_high=14.0, seed=99)
    for t in range(0, 3000, 10):
        _, tws = w.get(t)
        assert tws >= 4.0


# ---------------------------------------------------------------------------
# Full simulation
# ---------------------------------------------------------------------------


def _make_config(seed: int = 42) -> SynthConfig:
    legs = build_wl_course(47.63, -122.40, wind_dir=0.0, leg_nm=1.0, laps=2)
    return SynthConfig(
        start_lat=47.63,
        start_lon=-122.40,
        base_twd=0.0,
        tws_low=8.0,
        tws_high=14.0,
        shift_interval=(600.0, 1200.0),
        shift_magnitude=(5.0, 14.0),
        legs=legs,
        seed=seed,
        start_time=datetime(2026, 3, 8, 19, 0, 0, tzinfo=UTC),
    )


def test_simulate_basic() -> None:
    """Simulation should produce a reasonable number of rows."""
    rows = simulate(_make_config())
    # 2-lap W/L should take ~60-120 min at J/105 speeds
    assert 3000 < len(rows) < 10000


def test_simulate_depth_floor() -> None:
    """All depth values must be >= 2.0m (>6 ft)."""
    rows = simulate(_make_config())
    assert all(r.depth >= 2.0 for r in rows)


def test_simulate_positions_progress() -> None:
    """Positions should change over the course of the simulation."""
    rows = simulate(_make_config())
    first = rows[0]
    last = rows[-1]
    # Should have moved from the start position
    assert abs(first.lat - last.lat) > 0.001 or abs(first.lon - last.lon) > 0.001


def test_simulate_has_wind_data() -> None:
    """All rows should have non-zero wind data."""
    rows = simulate(_make_config())
    assert all(r.tws > 0 for r in rows)
    assert all(r.aws > 0 for r in rows)


def test_simulate_bsp_range() -> None:
    """BSP should stay within J/105 range (2-8 kts for 8-14 TWS)."""
    rows = simulate(_make_config())
    bsps = [r.bsp for r in rows]
    assert min(bsps) >= 2.0
    assert max(bsps) <= 9.0
