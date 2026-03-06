"""Tests for Insta360 X4 file discovery and grouping."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from logger.insta360 import (
    InstaRecording,
    discover_recordings,
    match_sessions,
    parse_insv_filename,
    recording_start_utc,
)

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------


class TestParseInsvFilename:
    def test_standard_back_lens(self) -> None:
        info = parse_insv_filename("VID_20260810_140530_00_000.insv")
        assert info is not None
        assert info.timestamp_str == "20260810_140530"
        assert info.lens == "00"
        assert info.segment == 0
        assert info.extension == "insv"

    def test_front_lens(self) -> None:
        info = parse_insv_filename("VID_20260810_140530_10_000.insv")
        assert info is not None
        assert info.lens == "10"

    def test_multi_segment(self) -> None:
        info = parse_insv_filename("VID_20260810_140530_00_002.insv")
        assert info is not None
        assert info.segment == 2

    def test_mp4_single_lens(self) -> None:
        """Single-lens .mp4 recordings should be parsed."""
        info = parse_insv_filename("VID_20260810_140530_00_001.mp4")
        assert info is not None
        assert info.timestamp_str == "20260810_140530"
        assert info.lens == "00"
        assert info.segment == 1
        assert info.extension == "mp4"

    def test_lrv_file_returns_none(self) -> None:
        """Low-res preview files should be skipped."""
        assert parse_insv_filename("LRV_20260810_140530_01_000.mp4") is None

    def test_photo_file_returns_none(self) -> None:
        assert parse_insv_filename("IMG_20260810_140530_00_000.insp") is None

    def test_garbage_returns_none(self) -> None:
        assert parse_insv_filename("random_file.txt") is None

    def test_pro_video_prefix(self) -> None:
        info = parse_insv_filename("PRO_VID_20260810_140530_00_000.insv")
        assert info is not None
        assert info.timestamp_str == "20260810_140530"


# ---------------------------------------------------------------------------
# Recording discovery
# ---------------------------------------------------------------------------


class TestDiscoverRecordings:
    def test_empty_directory(self, tmp_path: Path) -> None:
        """No DCIM dir → empty list."""
        result = discover_recordings(tmp_path)
        assert result == []

    def test_empty_camera_dir(self, tmp_path: Path) -> None:
        """DCIM/Camera01 exists but empty."""
        (tmp_path / "DCIM" / "Camera01").mkdir(parents=True)
        result = discover_recordings(tmp_path)
        assert result == []

    def test_single_recording_single_segment(self, tmp_path: Path) -> None:
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        # Back lens only (front lens paired by stitcher)
        (cam / "VID_20260810_140530_00_000.insv").write_bytes(b"\x00" * 1024)
        # Front lens — should be excluded from segments list
        (cam / "VID_20260810_140530_10_000.insv").write_bytes(b"\x00" * 1024)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 1
        assert recs[0].timestamp_str == "20260810_140530"
        assert len(recs[0].segments) == 1
        assert recs[0].segments[0].name == "VID_20260810_140530_00_000.insv"

    def test_single_recording_multi_segment(self, tmp_path: Path) -> None:
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        (cam / "VID_20260810_140530_00_000.insv").write_bytes(b"\x00" * 2048)
        (cam / "VID_20260810_140530_00_001.insv").write_bytes(b"\x00" * 1024)
        (cam / "VID_20260810_140530_00_002.insv").write_bytes(b"\x00" * 512)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 1
        assert len(recs[0].segments) == 3
        # Segments ordered by number
        assert [s.name for s in recs[0].segments] == [
            "VID_20260810_140530_00_000.insv",
            "VID_20260810_140530_00_001.insv",
            "VID_20260810_140530_00_002.insv",
        ]
        assert recs[0].total_size_bytes == 2048 + 1024 + 512

    def test_multiple_recordings(self, tmp_path: Path) -> None:
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        (cam / "VID_20260810_140530_00_000.insv").write_bytes(b"\x00" * 100)
        (cam / "VID_20260810_153000_00_000.insv").write_bytes(b"\x00" * 200)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 2
        # Ordered by timestamp
        assert recs[0].timestamp_str == "20260810_140530"
        assert recs[1].timestamp_str == "20260810_153000"

    def test_skips_lrv_files(self, tmp_path: Path) -> None:
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        (cam / "VID_20260810_140530_00_000.insv").write_bytes(b"\x00" * 100)
        (cam / "LRV_20260810_140530_01_000.mp4").write_bytes(b"\x00" * 50)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 1
        assert all(s.suffix == ".insv" for s in recs[0].segments)

    def test_skips_front_lens(self, tmp_path: Path) -> None:
        """Front lens (_10_) files are NOT included in segments."""
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        (cam / "VID_20260810_140530_00_000.insv").write_bytes(b"\x00" * 100)
        (cam / "VID_20260810_140530_10_000.insv").write_bytes(b"\x00" * 100)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 1
        assert len(recs[0].segments) == 1
        assert "_00_" in recs[0].segments[0].name

    def test_insv_needs_stitching(self, tmp_path: Path) -> None:
        """.insv recordings should have needs_stitching=True."""
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        (cam / "VID_20260810_140530_00_000.insv").write_bytes(b"\x00" * 100)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 1
        assert recs[0].needs_stitching is True

    def test_mp4_no_stitching(self, tmp_path: Path) -> None:
        """.mp4 recordings should have needs_stitching=False."""
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        (cam / "VID_20260810_140530_00_001.mp4").write_bytes(b"\x00" * 200)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 1
        assert recs[0].needs_stitching is False
        assert recs[0].segments[0].name == "VID_20260810_140530_00_001.mp4"

    def test_mixed_insv_and_mp4(self, tmp_path: Path) -> None:
        """SD card with both 360° and single-lens recordings."""
        cam = tmp_path / "DCIM" / "Camera01"
        cam.mkdir(parents=True)
        (cam / "VID_20260810_140530_00_000.insv").write_bytes(b"\x00" * 100)
        (cam / "VID_20260810_153000_00_001.mp4").write_bytes(b"\x00" * 200)

        recs = discover_recordings(tmp_path)
        assert len(recs) == 2
        assert recs[0].timestamp_str == "20260810_140530"
        assert recs[0].needs_stitching is True
        assert recs[1].timestamp_str == "20260810_153000"
        assert recs[1].needs_stitching is False


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------


class TestRecordingStartUtc:
    def test_utc_timezone(self) -> None:
        rec = InstaRecording(
            timestamp_str="20260810_140530",
            segments=[],
            total_size_bytes=0,
        )
        utc = recording_start_utc(rec, "UTC")
        assert utc == datetime(2026, 8, 10, 14, 5, 30, tzinfo=UTC)

    def test_pacific_timezone(self) -> None:
        """Camera timestamp is local time — convert to UTC."""
        rec = InstaRecording(
            timestamp_str="20260810_070530",
            segments=[],
            total_size_bytes=0,
        )
        utc = recording_start_utc(rec, "America/Los_Angeles")
        # Aug 10 07:05:30 PDT = Aug 10 14:05:30 UTC
        assert utc == datetime(2026, 8, 10, 14, 5, 30, tzinfo=UTC)

    def test_invalid_timezone_raises(self) -> None:
        rec = InstaRecording(
            timestamp_str="20260810_140530",
            segments=[],
            total_size_bytes=0,
        )
        with pytest.raises(KeyError):
            recording_start_utc(rec, "Not/A/Timezone")


# ---------------------------------------------------------------------------
# Session matching
# ---------------------------------------------------------------------------


class TestMatchSessions:
    def test_exact_overlap(self) -> None:
        """Recording that falls entirely within a session matches it."""
        sessions = [
            {
                "id": 1,
                "name": "Race 1",
                "start_utc": "2026-08-10T14:00:00+00:00",
                "end_utc": "2026-08-10T15:00:00+00:00",
                "event": "Ballard Cup",
                "race_num": 1,
                "date": "2026-08-10",
                "session_type": "race",
            },
        ]
        rec_start = datetime(2026, 8, 10, 13, 55, 0, tzinfo=UTC)  # started 5 min early
        rec_end = datetime(2026, 8, 10, 15, 5, 0, tzinfo=UTC)

        result = match_sessions(rec_start, rec_end, sessions)
        assert result is not None
        assert result["id"] == 1

    def test_best_overlap_wins(self) -> None:
        """When recording spans two sessions, pick the one with more overlap."""
        sessions = [
            {
                "id": 1,
                "start_utc": "2026-08-10T14:00:00+00:00",
                "end_utc": "2026-08-10T14:30:00+00:00",
            },
            {
                "id": 2,
                "start_utc": "2026-08-10T14:30:00+00:00",
                "end_utc": "2026-08-10T16:00:00+00:00",
            },
        ]
        # Recording covers 14:20 → 15:30 — 10 min overlap with session 1, 60 min with session 2
        rec_start = datetime(2026, 8, 10, 14, 20, 0, tzinfo=UTC)
        rec_end = datetime(2026, 8, 10, 15, 30, 0, tzinfo=UTC)

        result = match_sessions(rec_start, rec_end, sessions)
        assert result is not None
        assert result["id"] == 2

    def test_no_overlap(self) -> None:
        """Recording outside all sessions returns None."""
        sessions = [
            {
                "id": 1,
                "start_utc": "2026-08-10T14:00:00+00:00",
                "end_utc": "2026-08-10T15:00:00+00:00",
            },
        ]
        rec_start = datetime(2026, 8, 10, 16, 0, 0, tzinfo=UTC)
        rec_end = datetime(2026, 8, 10, 17, 0, 0, tzinfo=UTC)

        assert match_sessions(rec_start, rec_end, sessions) is None

    def test_empty_sessions(self) -> None:
        rec_start = datetime(2026, 8, 10, 14, 0, 0, tzinfo=UTC)
        rec_end = datetime(2026, 8, 10, 15, 0, 0, tzinfo=UTC)
        assert match_sessions(rec_start, rec_end, []) is None

    def test_session_without_end_utc(self) -> None:
        """Active session (no end_utc) should still match."""
        sessions = [
            {
                "id": 1,
                "start_utc": "2026-08-10T14:00:00+00:00",
                "end_utc": None,
            },
        ]
        rec_start = datetime(2026, 8, 10, 14, 30, 0, tzinfo=UTC)
        rec_end = datetime(2026, 8, 10, 15, 30, 0, tzinfo=UTC)

        result = match_sessions(rec_start, rec_end, sessions)
        assert result is not None
        assert result["id"] == 1
