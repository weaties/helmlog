"""Tests for gopro.py GPMF/GPSU extraction."""

from __future__ import annotations

import json
import struct
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

from helmlog.gopro import (
    _gpmf_stream_index,
    _parse_gpsu,
    _parse_gpsu_string,
    probe_video,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gpsu_bytes(ts: str = "260808225720.989") -> bytes:
    """Build a minimal GPMF GPSU element."""
    payload = ts.encode("ascii").ljust(16, b"\x00")[:16]
    return b"GPSU" + b"U" + bytes([16]) + struct.pack(">H", 1) + payload


def _ffprobe_json(
    streams: list[dict],
    format_tags: dict,
    duration: str = "963.963",
) -> str:
    return json.dumps(
        {
            "streams": streams,
            "format": {"duration": duration, "tags": format_tags},
        }
    )


# ---------------------------------------------------------------------------
# _parse_gpsu_string
# ---------------------------------------------------------------------------


class TestParseGpsuString:
    def test_nominal(self) -> None:
        dt = _parse_gpsu_string("260808225720")
        assert dt == datetime(2026, 8, 8, 22, 57, 20, tzinfo=UTC)

    def test_with_fraction(self) -> None:
        dt = _parse_gpsu_string("260808225720.989")
        assert dt == datetime(2026, 8, 8, 22, 57, 20, 989000, tzinfo=UTC)

    def test_null_padded(self) -> None:
        dt = _parse_gpsu_string("260808225720.989\x00\x00\x00")
        assert dt == datetime(2026, 8, 8, 22, 57, 20, 989000, tzinfo=UTC)

    def test_too_short(self) -> None:
        assert _parse_gpsu_string("2608") is None

    def test_invalid_digits(self) -> None:
        assert _parse_gpsu_string("XXXXXXXXXXXX") is None

    def test_empty(self) -> None:
        assert _parse_gpsu_string("") is None


# ---------------------------------------------------------------------------
# _parse_gpsu (binary)
# ---------------------------------------------------------------------------


class TestParseGpsu:
    def test_finds_gpsu_with_leading_garbage(self) -> None:
        data = b"\x00" * 100 + _make_gpsu_bytes("260808225720.989") + b"\x00" * 50
        assert _parse_gpsu(data) == datetime(2026, 8, 8, 22, 57, 20, 989000, tzinfo=UTC)

    def test_gpsu_at_start(self) -> None:
        data = _make_gpsu_bytes("260808130000")
        assert _parse_gpsu(data) == datetime(2026, 8, 8, 13, 0, 0, tzinfo=UTC)

    def test_returns_first_gpsu(self) -> None:
        data = _make_gpsu_bytes("260808100000") + b"\x00" * 4 + _make_gpsu_bytes("260808110000")
        assert _parse_gpsu(data) == datetime(2026, 8, 8, 10, 0, 0, tzinfo=UTC)

    def test_no_gpsu_returns_none(self) -> None:
        assert _parse_gpsu(b"\x00" * 200) is None

    def test_truncated_payload_returns_none(self) -> None:
        # GPSU header present but value bytes missing
        data = b"GPSU" + b"U" + bytes([16]) + struct.pack(">H", 1) + b"\x00" * 4
        assert _parse_gpsu(data) is None


# ---------------------------------------------------------------------------
# _gpmf_stream_index
# ---------------------------------------------------------------------------


class TestGpmfStreamIndex:
    def test_finds_gopro_met(self) -> None:
        streams = [
            {"codec_type": "video", "tags": {}},
            {"codec_type": "audio", "tags": {}},
            {"codec_type": "data", "tags": {"handler_name": "\x0bGoPro TCD  "}},
            {"codec_type": "data", "tags": {"handler_name": "\x0bGoPro MET  "}},
        ]
        assert _gpmf_stream_index(streams) == 1  # second data stream → index 1

    def test_gopro_met_only_stream(self) -> None:
        streams = [
            {"codec_type": "data", "tags": {"handler_name": "GoPro MET"}},
        ]
        assert _gpmf_stream_index(streams) == 0

    def test_no_gopro_met(self) -> None:
        streams = [
            {"codec_type": "video", "tags": {}},
            {"codec_type": "data", "tags": {"handler_name": "some other data"}},
        ]
        assert _gpmf_stream_index(streams) is None

    def test_empty(self) -> None:
        assert _gpmf_stream_index([]) is None

    def test_no_data_streams(self) -> None:
        streams = [{"codec_type": "video", "tags": {}}, {"codec_type": "audio", "tags": {}}]
        assert _gpmf_stream_index(streams) is None


# ---------------------------------------------------------------------------
# probe_video — GPMF integration
# ---------------------------------------------------------------------------


class TestProbeVideoGpmf:
    def test_prefers_gpmf_over_container_time(self, tmp_path: Path) -> None:
        """GPSU from GPMF stream is used instead of wrong container creation_time."""
        video_path = tmp_path / "test.MP4"
        video_path.touch()

        ffprobe_out = _ffprobe_json(
            streams=[
                {"codec_type": "video", "tags": {"creation_time": "2016-05-19T02:46:57.000000Z"}},
                {"codec_type": "data", "tags": {"handler_name": "\x0bGoPro TCD  "}},
                {"codec_type": "data", "tags": {"handler_name": "\x0bGoPro MET  "}},
            ],
            format_tags={"creation_time": "2016-05-19T02:46:57.000000Z"},
        )
        gpmf_data = b"\x00" * 100 + _make_gpsu_bytes("260808225720.989")

        with (
            patch("subprocess.run") as mock_run,
            patch("helmlog.gopro._extract_gpmf_bytes", return_value=gpmf_data),
        ):
            mock_result = MagicMock()
            mock_result.stdout = ffprobe_out
            mock_run.return_value = mock_result

            video = probe_video(video_path)

        assert video.creation_utc == datetime(2026, 8, 8, 22, 57, 20, 989000, tzinfo=UTC)
        assert video.creation_source == "gpmf:GPSU"

    def test_falls_back_to_container_time_when_no_gpmf_stream(self, tmp_path: Path) -> None:
        video_path = tmp_path / "test.MP4"
        video_path.touch()

        ffprobe_out = _ffprobe_json(
            streams=[{"codec_type": "video", "tags": {}}],
            format_tags={"creation_time": "2026-08-08T22:58:00.000000Z"},
        )

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = ffprobe_out
            mock_run.return_value = mock_result

            video = probe_video(video_path)

        assert video.creation_utc == datetime(2026, 8, 8, 22, 58, 0, tzinfo=UTC)
        assert video.creation_source == "ffprobe:creation_time"

    def test_falls_back_to_container_time_when_gpmf_has_no_gpsu(self, tmp_path: Path) -> None:
        video_path = tmp_path / "test.MP4"
        video_path.touch()

        ffprobe_out = _ffprobe_json(
            streams=[
                {"codec_type": "data", "tags": {"handler_name": "GoPro MET"}},
            ],
            format_tags={"creation_time": "2026-08-08T22:58:00.000000Z"},
        )

        with (
            patch("subprocess.run") as mock_run,
            patch("helmlog.gopro._extract_gpmf_bytes", return_value=b"\x00" * 100),
        ):
            mock_result = MagicMock()
            mock_result.stdout = ffprobe_out
            mock_run.return_value = mock_result

            video = probe_video(video_path)

        assert video.creation_utc == datetime(2026, 8, 8, 22, 58, 0, tzinfo=UTC)
        assert video.creation_source == "ffprobe:creation_time"

    def test_no_time_anywhere(self, tmp_path: Path) -> None:
        video_path = tmp_path / "test.MP4"
        video_path.touch()

        ffprobe_out = _ffprobe_json(
            streams=[{"codec_type": "video", "tags": {}}],
            format_tags={},
        )

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = ffprobe_out
            mock_run.return_value = mock_result

            video = probe_video(video_path)

        assert video.creation_utc is None
        assert video.creation_source is None
