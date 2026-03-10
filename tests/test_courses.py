"""Tests for courses.py — CYC marks and course building."""

from __future__ import annotations

import pytest

from helmlog.courses import (
    CYC_MARKS,
    CourseLeg,
    CourseMark,
    build_custom_course,
    build_triangle_course,
    build_wl_course,
    compute_buoy_marks,
    validate_course_marks,
)


class TestCYCMarks:
    def test_all_marks_present(self) -> None:
        expected = {"D", "E", "H", "I", "J", "K", "L", "M", "N", "P", "Q", "R", "T", "U", "V"}
        assert set(CYC_MARKS.keys()) == expected

    def test_mark_coordinates_in_puget_sound(self) -> None:
        for key, m in CYC_MARKS.items():
            assert 47.5 < m.lat < 47.8, f"Mark {key} lat out of range: {m.lat}"
            assert -122.6 < m.lon < -122.3, f"Mark {key} lon out of range: {m.lon}"


class TestComputeBuoyMarks:
    def test_returns_all_six_marks(self) -> None:
        marks = compute_buoy_marks(47.63, -122.40, 0.0)
        assert set(marks.keys()) == {"S", "A", "O", "G", "X", "F"}

    def test_finish_at_rc(self) -> None:
        marks = compute_buoy_marks(47.63, -122.40, 180.0)
        assert marks["F"].lat == 47.63
        assert marks["F"].lon == -122.40

    def test_windward_mark_upwind(self) -> None:
        marks = compute_buoy_marks(47.63, -122.40, 0.0, 1.0)
        # Wind from north: A should be north of RC
        assert marks["A"].lat > 47.63


class TestBuildWLCourse:
    def test_two_laps_plus_finish(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=2)
        # 2 laps × 2 legs + finish leg to F = 5
        assert len(legs) == 5
        assert all(isinstance(leg, CourseLeg) for leg in legs)

    def test_one_lap_visits_weather_mark_once(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        # 1 lap: A -> X -> F = 3 legs
        assert len(legs) == 3
        weather_visits = sum(1 for leg in legs if leg.target.name == "Windward A")
        assert weather_visits == 1

    def test_alternating_upwind_downwind(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=2)
        assert legs[0].upwind is True
        assert legs[1].upwind is False
        assert legs[2].upwind is True
        assert legs[3].upwind is False
        # Final beat from leeward to finish
        assert legs[4].upwind is True

    def test_finishes_at_rc(self) -> None:
        legs = build_wl_course(47.63, -122.40, 0.0, laps=2)
        assert legs[-1].target.name == "Finish"
        assert legs[-1].target.lat == 47.63
        assert legs[-1].target.lon == -122.40

    def test_mark_overrides_respected(self) -> None:
        """Dragged mark positions should override computed positions."""
        default_legs = build_wl_course(47.63, -122.40, 0.0, laps=1)
        default_a = default_legs[0].target
        # Move windward mark A to a custom position
        custom_lat, custom_lon = 47.66, -122.41
        legs = build_wl_course(
            47.63,
            -122.40,
            0.0,
            laps=1,
            mark_overrides={"A": (custom_lat, custom_lon)},
        )
        assert legs[0].target.lat == custom_lat
        assert legs[0].target.lon == custom_lon
        assert legs[0].target.lat != default_a.lat
        # X mark should be unchanged
        assert legs[1].target.lat == default_legs[1].target.lat


class TestBuildTriangleCourse:
    def test_four_legs_with_finish(self) -> None:
        legs = build_triangle_course(47.63, -122.40, 0.0)
        # A -> G -> X -> F
        assert len(legs) == 4
        assert legs[-1].target.name == "Finish"


class TestBuildCustomCourse:
    def test_valid_cyc_sequence(self) -> None:
        legs = build_custom_course("S-K-D-I-F", 47.63, -122.40, 0.0)
        assert len(legs) == 4

    def test_unknown_mark_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown mark"):
            build_custom_course("S-Z-F", 47.63, -122.40, 0.0)

    def test_too_few_marks_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 2 marks"):
            build_custom_course("S", 47.63, -122.40, 0.0)

    def test_mixed_buoy_and_cyc(self) -> None:
        legs = build_custom_course("S-A-K-F", 47.63, -122.40, 0.0)
        assert len(legs) == 3


class TestValidateCourseMarks:
    def test_marks_in_water_pass(self) -> None:
        """Buoy marks in the middle of Puget Sound should produce no warnings."""
        marks = compute_buoy_marks(47.63, -122.42, 0.0)
        warnings = validate_course_marks(marks)
        assert warnings == []

    def test_cyc_marks_all_in_water(self) -> None:
        """All predefined CYC marks should be in navigable water."""
        warnings = validate_course_marks(CYC_MARKS)
        assert warnings == [], f"CYC marks failed validation: {warnings}"

    def test_mark_on_land_warns(self) -> None:
        """A mark placed on land (downtown Seattle) should produce a warning."""
        land_marks = {"X": CourseMark("On Land", 47.61, -122.33)}
        warnings = validate_course_marks(land_marks)
        assert len(warnings) == 1
        assert "X" in warnings[0]
        assert "shallow water" in warnings[0] or "land" in warnings[0]

    def test_mark_on_bainbridge_warns(self) -> None:
        """A mark on Bainbridge Island should produce a warning."""
        land_marks = {"B": CourseMark("Bainbridge", 47.63, -122.54)}
        warnings = validate_course_marks(land_marks)
        assert len(warnings) == 1
        assert "B" in warnings[0]

    def test_mixed_valid_and_invalid(self) -> None:
        """Only invalid marks produce warnings."""
        marks = {
            "A": CourseMark("In Water", 47.65, -122.42),
            "Z": CourseMark("On Land", 47.61, -122.33),
        }
        warnings = validate_course_marks(marks)
        assert len(warnings) == 1
        assert "Z" in warnings[0]
