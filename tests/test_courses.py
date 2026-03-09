"""Tests for helmlog.courses — CYC marks and course builder."""

from __future__ import annotations

import pytest

from helmlog.courses import (
    CYC_MARKS,
    CourseLeg,
    build_custom_course,
    build_triangle_course,
    build_wl_course,
    compute_buoy_marks,
)

# ---------------------------------------------------------------------------
# CYC mark data
# ---------------------------------------------------------------------------


def test_cyc_marks_spot_check_u() -> None:
    """Mark U has exact coords given in the PDF: 47°44.4'N, 122°22.95'W."""
    u = CYC_MARKS["U"]
    assert abs(u.lat - 47.740) < 0.002
    assert abs(u.lon - (-122.3825)) < 0.002


def test_cyc_marks_all_in_puget_sound() -> None:
    """All marks should be in the Puget Sound area."""
    for key, m in CYC_MARKS.items():
        assert 47.5 < m.lat < 47.8, f"Mark {key} lat {m.lat} out of range"
        assert -122.55 < m.lon < -122.3, f"Mark {key} lon {m.lon} out of range"


# ---------------------------------------------------------------------------
# Buoy mark computation
# ---------------------------------------------------------------------------


def test_buoy_marks_north_wind() -> None:
    """With wind from N (0°), windward mark A should be north of RC."""
    marks = compute_buoy_marks(47.63, -122.40, wind_dir=0.0, leg_distance_nm=1.0)
    assert marks["A"].lat > 47.63
    assert marks["X"].lat < 47.63


def test_buoy_marks_south_wind() -> None:
    """With wind from S (180°), windward mark A should be south of RC."""
    marks = compute_buoy_marks(47.63, -122.40, wind_dir=180.0, leg_distance_nm=1.0)
    assert marks["A"].lat < 47.63
    assert marks["X"].lat > 47.63


def test_buoy_marks_gybe_offset() -> None:
    """Gybe mark G should be offset laterally from the downwind axis."""
    marks = compute_buoy_marks(47.63, -122.40, wind_dir=0.0, leg_distance_nm=1.0)
    # G should be south of RC (downwind) and offset east or west
    assert marks["G"].lat < 47.63
    assert abs(marks["G"].lon - (-122.40)) > 0.001


def test_buoy_marks_has_all_keys() -> None:
    marks = compute_buoy_marks(47.63, -122.40, wind_dir=0.0)
    for key in ("S", "A", "O", "G", "X", "F"):
        assert key in marks, f"Missing buoy mark {key}"


# ---------------------------------------------------------------------------
# Course builders
# ---------------------------------------------------------------------------


def test_wl_course_4_legs() -> None:
    """2-lap W/L should produce 4 legs."""
    legs = build_wl_course(47.63, -122.40, wind_dir=0.0, leg_nm=1.0, laps=2)
    assert len(legs) == 4
    assert all(isinstance(leg, CourseLeg) for leg in legs)


def test_wl_course_alternates_upwind_downwind() -> None:
    legs = build_wl_course(47.63, -122.40, wind_dir=0.0, leg_nm=1.0, laps=2)
    assert legs[0].upwind is True
    assert legs[1].upwind is False
    assert legs[2].upwind is True
    assert legs[3].upwind is False


def test_triangle_course_3_legs() -> None:
    legs = build_triangle_course(47.63, -122.40, wind_dir=0.0, leg_nm=1.0)
    assert len(legs) == 3
    # First leg upwind, then reach to gybe, then downwind to leeward
    assert legs[0].upwind is True


def test_custom_course_parse() -> None:
    """Parse a mark sequence like 'S-A-G-X-F'."""
    legs = build_custom_course("S-A-G-X-F", 47.63, -122.40, wind_dir=0.0)
    assert len(legs) == 4  # 4 legs between 5 marks
    assert legs[0].target.name == "Windward A"


def test_custom_course_invalid_mark() -> None:
    with pytest.raises(ValueError, match="Unknown mark"):
        build_custom_course("S-A-Z-Q-F", 47.63, -122.40, wind_dir=0.0)


def test_wl_course_single_lap() -> None:
    legs = build_wl_course(47.63, -122.40, wind_dir=0.0, leg_nm=1.0, laps=1)
    assert len(legs) == 2
