"""Heuristic classifier for Gaia GPS tracks.

Determines whether a track is a race, practice, delivery, or unknown based
on duration, speed, location, track name, and time-of-day signals.

This module is pure logic with no I/O — safe to test without hardware or network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from helmlog.gaigps import GaiaTrack

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_NM_TO_M = 1852.0

# Name patterns
_RACE_PATTERNS = re.compile(r"\b(race|regatta|r\d|cyc)\b", re.IGNORECASE)
_DELIVERY_PATTERNS = re.compile(r"\b(delivery|deliver|reposition|move boat)\b", re.IGNORECASE)
_PRACTICE_PATTERNS = re.compile(r"\b(practice|pre.?race|warm.?up)\b", re.IGNORECASE)
_NOT_SAILING_PATTERNS = re.compile(
    r"\b(run|running|bike|biking|cycling|walk|walking|hike|hiking|ski|snowboard"
    r"|trail|drive|driving|flight|train|museum|parade)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClassifierConfig:
    """Tuning knobs for the race classifier."""

    race_area_lat: float = 47.688
    race_area_lon: float = -122.417
    race_area_radius_nm: float = 5.0
    min_duration_min: float = 30
    max_duration_min: float = 300
    min_speed_kts: float = 3.0
    confidence_threshold: float = 0.6


@dataclass
class ClassificationResult:
    """Output of the classifier."""

    is_race: bool
    confidence: float
    session_type: str  # "race", "practice", "delivery", "unknown"
    signals: dict[str, bool]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(track: GaiaTrack, config: ClassifierConfig) -> ClassificationResult:
    """Classify a Gaia GPS track using heuristic signals."""
    from helmlog.gaigps import _haversine_m

    duration_min = (track.ended_at - track.started_at).total_seconds() / 60

    # Compute median SOG from points that have it
    sog_values = [p.sog_kts for p in track.points if p.sog_kts is not None]
    median_sog = sorted(sog_values)[len(sog_values) // 2] if sog_values else 0

    # Distance from race area centre (use first point)
    if track.points:
        dist_from_area_m = _haversine_m(
            track.points[0].lat,
            track.points[0].lon,
            config.race_area_lat,
            config.race_area_lon,
        )
    else:
        dist_from_area_m = float("inf")

    dist_from_area_nm = dist_from_area_m / _NM_TO_M

    # Build signals
    signals: dict[str, bool] = {
        "duration_ok": config.min_duration_min <= duration_min <= config.max_duration_min,
        "speed_ok": median_sog >= config.min_speed_kts,
        "near_race_area": dist_from_area_nm <= config.race_area_radius_nm,
        "name_suggests_race": bool(_RACE_PATTERNS.search(track.name)),
        "name_suggests_delivery": bool(_DELIVERY_PATTERNS.search(track.name)),
        "name_suggests_practice": bool(_PRACTICE_PATTERNS.search(track.name)),
        "name_not_sailing": bool(_NOT_SAILING_PATTERNS.search(track.name)),
    }

    # Non-sailing activity names are an immediate disqualifier
    if signals["name_not_sailing"] and not signals["name_suggests_race"]:
        return ClassificationResult(
            is_race=False,
            confidence=0.1,
            session_type="unknown",
            signals=signals,
        )

    # Determine session type from name first (strongest signal)
    if signals["name_suggests_delivery"]:
        return ClassificationResult(
            is_race=False,
            confidence=0.9,
            session_type="delivery",
            signals=signals,
        )

    if signals["name_suggests_practice"]:
        return ClassificationResult(
            is_race=False,
            confidence=0.8,
            session_type="practice",
            signals=signals,
        )

    # Too long → delivery or unknown
    if duration_min > config.max_duration_min:
        return ClassificationResult(
            is_race=False,
            confidence=0.7,
            session_type="delivery" if median_sog >= config.min_speed_kts else "unknown",
            signals=signals,
        )

    # Score-based classification
    score = 0.0
    weights = {
        "duration_ok": 0.25,
        "speed_ok": 0.25,
        "near_race_area": 0.2,
        "name_suggests_race": 0.3,
    }

    for signal_name, weight in weights.items():
        if signals.get(signal_name):
            score += weight

    is_race = score >= config.confidence_threshold
    session_type = "race" if is_race else "unknown"

    return ClassificationResult(
        is_race=is_race,
        confidence=round(score, 2),
        session_type=session_type,
        signals=signals,
    )
