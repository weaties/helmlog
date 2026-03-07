"""Tests for race classification heuristics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from logger.gaigps import GaiaTrack, TrackPoint
from logger.race_classifier import ClassificationResult, ClassifierConfig, classify


def _make_track(
    *,
    name: str = "Test Track",
    start: datetime = datetime(2025, 7, 9, 1, 0, 0, tzinfo=UTC),  # Wed 18:00 PDT
    duration_min: float = 60,
    num_points: int = 100,
    base_lat: float = 47.688,
    base_lon: float = -122.417,
    sog_kts: float = 5.0,
    distance_m: float = 5000.0,
    heading_reversals: int = 0,
) -> GaiaTrack:
    """Build a synthetic track for testing."""
    dt = timedelta(minutes=duration_min) / num_points
    points: list[TrackPoint] = []

    for i in range(num_points):
        ts = start + dt * i
        # Create heading reversals by zigzagging lat
        reversal_period = num_points // max(heading_reversals * 2, 1)
        is_forward = heading_reversals == 0 or (i // reversal_period) % 2 == 0
        direction = 1 if is_forward else -1
        lat = base_lat + direction * (i * 0.0001)
        lon = base_lon + i * 0.0001

        points.append(
            TrackPoint(
                lat=lat,
                lon=lon,
                elevation_m=0.0,
                timestamp=ts,
                sog_kts=sog_kts if i > 0 else None,
                cog_deg=45.0 if i > 0 else None,
            )
        )

    return GaiaTrack(
        track_id="test123",
        name=name,
        started_at=start,
        ended_at=start + timedelta(minutes=duration_min),
        points=points,
        distance_m=distance_m,
        max_speed_mps=sog_kts / 1.94384,
    )


@pytest.fixture
def config() -> ClassifierConfig:
    return ClassifierConfig(
        race_area_lat=47.688,
        race_area_lon=-122.417,
        race_area_radius_nm=5.0,
    )


class TestClassify:
    def test_typical_race(self, config: ClassifierConfig) -> None:
        """A 1-hour track at 5+ kts near the race area on a Wednesday evening = race."""
        track = _make_track(
            name="Wednesday Evening Boating",
            duration_min=65,
            sog_kts=5.5,
            distance_m=6000.0,
        )
        result = classify(track, config)
        assert result.is_race
        assert result.session_type == "race"
        assert result.confidence >= 0.6

    def test_too_short(self, config: ClassifierConfig) -> None:
        """A 10-minute track is too short to be a race."""
        track = _make_track(duration_min=10, distance_m=1000.0)
        result = classify(track, config)
        assert not result.is_race
        assert result.confidence < 0.6

    def test_too_long(self, config: ClassifierConfig) -> None:
        """A 10-hour track is probably a delivery, not a race."""
        track = _make_track(duration_min=600, distance_m=50000.0)
        result = classify(track, config)
        assert result.session_type in ("delivery", "unknown")

    def test_too_slow(self, config: ClassifierConfig) -> None:
        """A track at 1 knot is motor-idle or drifting, not a race."""
        track = _make_track(sog_kts=1.0, distance_m=2000.0)
        result = classify(track, config)
        assert not result.is_race

    def test_far_from_race_area(self, config: ClassifierConfig) -> None:
        """A track far from the configured race area scores lower."""
        track = _make_track(
            base_lat=37.0,  # Way down in SF
            base_lon=-122.4,
            duration_min=65,
            sog_kts=5.5,
        )
        result = classify(track, config)
        assert not result.signals["near_race_area"]

    def test_classification_result_fields(self, config: ClassifierConfig) -> None:
        """All expected signal keys are present."""
        track = _make_track()
        result = classify(track, config)
        assert isinstance(result, ClassificationResult)
        assert isinstance(result.signals, dict)
        assert "duration_ok" in result.signals
        assert "speed_ok" in result.signals
        assert "near_race_area" in result.signals

    def test_race_name_hint(self, config: ClassifierConfig) -> None:
        """Track names containing 'race' or 'regatta' should boost confidence."""
        track = _make_track(
            name="North u Friday regatta",
            duration_min=180,
            sog_kts=5.0,
            distance_m=12000.0,
        )
        result = classify(track, config)
        assert result.signals["name_suggests_race"]
        assert result.is_race

    def test_delivery_name_hint(self, config: ClassifierConfig) -> None:
        """Track names containing 'delivery' should classify as delivery."""
        track = _make_track(
            name="25 race week delivery",
            duration_min=280,
            sog_kts=5.0,
            distance_m=25000.0,
        )
        result = classify(track, config)
        assert result.session_type == "delivery"

    def test_non_sailing_activity_filtered(self, config: ClassifierConfig) -> None:
        """Running, biking, hiking tracks should not be classified as races."""
        for name in [
            "Wednesday Evening Running",
            "Monday Morning Biking",
            "Sunday Morning bike ride",
            "Cherry Blossom Run",
        ]:
            track = _make_track(name=name, duration_min=65, sog_kts=5.0)
            result = classify(track, config)
            assert not result.is_race, f"{name} should not be a race"

    def test_non_sailing_with_race_name_still_race(self, config: ClassifierConfig) -> None:
        """A name with both 'run' and 'race' should still be a race."""
        track = _make_track(
            name="Cherry Blossom Run race",
            duration_min=65,
            sog_kts=5.0,
        )
        result = classify(track, config)
        assert result.is_race
