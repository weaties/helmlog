"""Tests for Gaia GPS track download and parsing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from logger.gaigps import GaiaTrack, TrackPoint, parse_track_detail, parse_track_list

# ---------------------------------------------------------------------------
# Sample API responses (trimmed from real HAR capture)
# ---------------------------------------------------------------------------

TRACK_LIST_ITEM = {
    "id": "7d1bd70eeb99ade0641acba897d32f8f",
    "updated_date": "2025-09-04T02:01:48Z",
    "time_created": "2025-09-04T01:17:32Z",
    "title": "Wednesday Evening Boating",
    "distance": 6540.8,
    "total_ascent": 0.0,
    "total_time": 2639.0,
    "activities": [],
    "source": "iPhone17,2",
    "folder": "a0eb2ea697e5fbe397515554cda6999d",
    "folder_name": "Sailing",
    "public": True,
    "deleted": False,
}

TRACK_DETAIL = {
    "id": "7d1bd70eeb99ade0641acba897d32f8f",
    "name": "Wednesday Evening Boating",
    "create_date": "2025-09-04T01:17:32",
    "source": "iPhone17,2",
    "geometry": {
        "type": "MultiLineString",
        "coordinates": [
            [
                [-122.417148, 47.688097, 0.5, 1756948653.0],
                [-122.417361, 47.688074, 0.2, 1756948659.0],
                [-122.416543, 47.687354, -2.0, 1756948738.0],
                [-122.413227, 47.688313, -1.2, 1756951292.0],
            ]
        ],
    },
    "stats": {
        "ascent": 0.0,
        "average_speed": 2.478,
        "descent": 0.0,
        "distance": 6540.8,
        "max_speed": 3.263,
        "moving_speed": 2.478,
        "moving_time": 2639,
        "stopped_time": 0,
        "total_time": 2639,
    },
}


# ---------------------------------------------------------------------------
# parse_track_list
# ---------------------------------------------------------------------------


class TestParseTrackList:
    def test_parses_single_item(self) -> None:
        summaries = parse_track_list([TRACK_LIST_ITEM])
        assert len(summaries) == 1
        s = summaries[0]
        assert s.track_id == "7d1bd70eeb99ade0641acba897d32f8f"
        assert s.title == "Wednesday Evening Boating"
        assert s.distance_m == pytest.approx(6540.8)
        assert s.total_time_s == pytest.approx(2639.0)

    def test_parses_empty_list(self) -> None:
        assert parse_track_list([]) == []

    def test_skips_deleted(self) -> None:
        deleted = {**TRACK_LIST_ITEM, "deleted": True}
        assert parse_track_list([deleted]) == []

    def test_time_created_parsed(self) -> None:
        summaries = parse_track_list([TRACK_LIST_ITEM])
        s = summaries[0]
        assert s.time_created == datetime(2025, 9, 4, 1, 17, 32, tzinfo=UTC)


# ---------------------------------------------------------------------------
# parse_track_detail
# ---------------------------------------------------------------------------


class TestParseTrackDetail:
    def test_parses_geometry(self) -> None:
        track = parse_track_detail(TRACK_DETAIL)
        assert isinstance(track, GaiaTrack)
        assert track.track_id == "7d1bd70eeb99ade0641acba897d32f8f"
        assert track.name == "Wednesday Evening Boating"
        assert len(track.points) == 4

    def test_point_fields(self) -> None:
        track = parse_track_detail(TRACK_DETAIL)
        p = track.points[0]
        assert isinstance(p, TrackPoint)
        assert p.lon == pytest.approx(-122.417148)
        assert p.lat == pytest.approx(47.688097)
        assert p.elevation_m == pytest.approx(0.5)
        assert p.timestamp == datetime(2025, 9, 4, 1, 17, 33, tzinfo=UTC)

    def test_time_range(self) -> None:
        track = parse_track_detail(TRACK_DETAIL)
        assert track.started_at == datetime(2025, 9, 4, 1, 17, 33, tzinfo=UTC)
        assert track.ended_at == datetime(2025, 9, 4, 2, 1, 32, tzinfo=UTC)

    def test_stats(self) -> None:
        track = parse_track_detail(TRACK_DETAIL)
        assert track.distance_m == pytest.approx(6540.8)
        assert track.max_speed_mps == pytest.approx(3.263)

    def test_invalid_elevation_filtered(self) -> None:
        """Elevation sentinel -19999.0 should be replaced with None."""
        detail = {
            **TRACK_DETAIL,
            "geometry": {
                "type": "MultiLineString",
                "coordinates": [
                    [
                        [-122.41, 47.68, -19999.0, 1756948653.0],
                        [-122.42, 47.69, 5.0, 1756948660.0],
                    ]
                ],
            },
        }
        track = parse_track_detail(detail)
        assert track.points[0].elevation_m is None
        assert track.points[1].elevation_m == pytest.approx(5.0)

    def test_computed_sog_cog(self) -> None:
        """SOG and COG should be computed from consecutive points."""
        track = parse_track_detail(TRACK_DETAIL)
        # First point has no previous, so sog/cog should be None
        assert track.points[0].sog_kts is None
        assert track.points[0].cog_deg is None
        # Subsequent points should have computed values
        assert track.points[1].sog_kts is not None
        assert track.points[1].sog_kts > 0
        assert track.points[1].cog_deg is not None
