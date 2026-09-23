"""Probe GoPro/MP4 metadata and match it to a HelmLog session.

This is an experiment for aligning a camera recording to a race using the
file's embedded metadata (creation timestamp and GPS tags) and the HelmLog
session history.
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from pathlib import Path


def _ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # ffprobe may emit ISO 8601 with a trailing Z
        if value.endswith("Z"):
            try:
                dt = datetime.fromisoformat(value[:-1])
                dt = dt.replace(tzinfo=UTC)
            except ValueError:
                return None
        else:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_location_string(value: str) -> tuple[float, float] | None:
    if not value:
        return None
    value = value.strip()

    # ISO6709-style: +47.123456-122.123456/ or -47.123456+122.123456/
    pattern = r"^(?P<lat>[+-]?\d+(?:\.\d+)?)(?P<lon>[+-]?\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?/?)?$"
    m = re.match(pattern, value)
    if m:
        try:
            lat = float(m.group("lat"))
            lon = float(m.group("lon"))
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    # Named latitude/longitude pairs.
    lat_match = re.search(r"lat(?:itude)?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", value, re.I)
    lon_match = re.search(r"lon(?:gitude)?\s*[:=]\s*([+-]?\d+(?:\.\d+)?)", value, re.I)
    if lat_match and lon_match:
        try:
            lat = float(lat_match.group(1))
            lon = float(lon_match.group(1))
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    # Comma/space separated lat lon.
    parts = re.split(r"[\s,;]+", value)
    if len(parts) >= 2:
        try:
            lat = float(parts[0])
            lon = float(parts[1])
        except ValueError:
            return None
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon

    return None


def _collect_tags(data: dict[str, Any]) -> dict[str, str]:
    tags: dict[str, str] = {}
    fmt = data.get("format") or {}
    if isinstance(fmt, dict):
        for key, value in (fmt.get("tags") or {}).items():
            if value is not None:
                tags[str(key)] = str(value)
    for stream in data.get("streams", []):
        if not isinstance(stream, dict):
            continue
        for key, value in (stream.get("tags") or {}).items():
            if value is not None:
                tags.setdefault(str(key), str(value))
    return tags


def _parse_gpsu_string(ts: str) -> datetime | None:
    """Parse a GPMF GPSU timestamp (YYMMDDHHMMSS[.mmm]) to a UTC datetime."""
    ts = ts.rstrip("\x00").strip()
    if len(ts) < 12:
        return None
    try:
        year = 2000 + int(ts[0:2])
        month = int(ts[2:4])
        day = int(ts[4:6])
        hour = int(ts[6:8])
        minute = int(ts[8:10])
        second = int(ts[10:12])
        microsecond = 0
        if len(ts) > 13 and ts[12] == ".":
            frac = ts[13:].ljust(6, "0")[:6]
            microsecond = int(frac)
        return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)
    except (ValueError, IndexError):
        return None


def _parse_gpsu(gpmf_data: bytes) -> datetime | None:
    """Find the first GPSU element in raw GPMF bytes and return its UTC time."""
    idx = gpmf_data.find(b"GPSU")
    if idx < 0 or idx + 8 > len(gpmf_data):
        return None
    size = gpmf_data[idx + 5]
    repeat = struct.unpack(">H", gpmf_data[idx + 6 : idx + 8])[0]
    value_len = size * repeat
    if idx + 8 + value_len > len(gpmf_data):
        return None
    try:
        ts_str = gpmf_data[idx + 8 : idx + 8 + value_len].decode("ascii")
    except UnicodeDecodeError:
        return None
    return _parse_gpsu_string(ts_str)


def _gpmf_stream_index(streams: list[dict[str, Any]]) -> int | None:
    """Return the data-stream index of the GoPro MET (GPMF) stream, or None."""
    data_idx = 0
    for stream in streams:
        if stream.get("codec_type") != "data":
            continue
        handler = (stream.get("tags") or {}).get("handler_name", "")
        if "GoPro MET" in handler:
            return data_idx
        data_idx += 1
    return None


def _extract_gpmf_bytes(
    path: Path, data_stream_idx: int, ffmpeg_cmd: str = "ffmpeg"
) -> bytes | None:
    """Extract the GPMF binary stream from a video file; returns None on failure."""
    import contextlib

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            [
                ffmpeg_cmd,
                "-i",
                str(path),
                "-map",
                f"0:d:{data_stream_idx}",
                "-f",
                "rawvideo",
                tmp_path,
                "-y",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        with open(tmp_path, "rb") as f:
            return f.read()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


class GoProProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoProVideo:
    path: Path
    duration_s: float | None = None
    creation_utc: datetime | None = None
    creation_source: str | None = None
    gps_position: tuple[float, float] | None = None
    gps_source: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def start_utc(self) -> datetime | None:
        return self.creation_utc

    @property
    def end_utc(self) -> datetime | None:
        if self.creation_utc is None or self.duration_s is None:
            return None
        return self.creation_utc + timedelta(seconds=self.duration_s)


def probe_video(path: Path, timezone: str = "UTC") -> GoProVideo:
    if not path.exists():
        raise GoProProbeError(f"File does not exist: {path}")
    try:
        ffprobe_cmd = os.environ.get("HELMLOG_FFPROBE", "ffprobe")
        result = subprocess.run(
            [
                ffprobe_cmd,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration,format_tags:stream=codec_type:stream_tags",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise GoProProbeError("ffprobe is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GoProProbeError(f"ffprobe failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GoProProbeError("ffprobe timed out") from exc

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GoProProbeError("ffprobe returned invalid JSON") from exc

    tags = _collect_tags(data)
    streams = data.get("streams") or []
    duration_s = None
    fmt = data.get("format") or {}
    if isinstance(fmt, dict):
        duration = fmt.get("duration")
        if duration is not None:
            try:
                duration_s = float(duration)
            except (ValueError, TypeError):
                duration_s = None

    # Prefer GPS-disciplined UTC from the GPMF stream over the container clock,
    # which is often uncorrected (wrong year, off by hours, etc.).
    creation_utc: datetime | None = None
    creation_source: str | None = None
    ffmpeg_cmd = os.environ.get("HELMLOG_FFMPEG", "ffmpeg")
    gpmf_idx = _gpmf_stream_index(streams)
    if gpmf_idx is not None:
        gpmf_data = _extract_gpmf_bytes(path, gpmf_idx, ffmpeg_cmd)
        if gpmf_data is not None:
            creation_utc = _parse_gpsu(gpmf_data)
            if creation_utc is not None:
                creation_source = "gpmf:GPSU"

    if creation_utc is None:
        creation_time = tags.get("creation_time")
        if creation_time is not None:
            parsed = _ts(creation_time)
            if parsed is None:
                try:
                    naive = datetime.fromisoformat(creation_time)
                except ValueError:
                    parsed = None
                else:
                    tz = ZoneInfo(timezone)
                    parsed = naive.replace(tzinfo=tz).astimezone(UTC)
            creation_utc = parsed
            if creation_utc is not None:
                creation_source = "ffprobe:creation_time"

    gps_position = None
    gps_source = None
    for key in [
        "com.apple.quicktime.location.ISO6709",
        "location",
        "location-eng",
        "GPSLatitude",
        "GPSLongitude",
    ]:
        if key not in tags:
            continue
        if key == "GPSLatitude" or key == "GPSLongitude":
            continue
        gps_position = _parse_location_string(tags[key])
        if gps_position is not None:
            gps_source = f"ffprobe:{key}"
            break
    if gps_position is None and "GPSLatitude" in tags and "GPSLongitude" in tags:
        try:
            gps_position = (float(tags["GPSLatitude"]), float(tags["GPSLongitude"]))
            gps_source = "ffprobe:GPSLatitude+GPSLongitude"
        except ValueError:
            gps_position = None

    return GoProVideo(
        path=path,
        duration_s=duration_s,
        creation_utc=creation_utc,
        creation_source=creation_source,
        gps_position=gps_position,
        gps_source=gps_source,
        tags=tags,
    )


def match_sessions_to_video(
    video: GoProVideo,
    sessions: list[dict[str, Any]],
    *,
    min_overlap_s: float = 1.0,
) -> list[dict[str, Any]]:
    if video.start_utc is None or video.duration_s is None:
        return []
    video_end = video.end_utc
    if video_end is None:
        return []

    candidates: list[dict[str, Any]] = []
    for session in sessions:
        start = session.get("start_utc")
        if not isinstance(start, str):
            continue
        try:
            session_start = datetime.fromisoformat(start)
        except ValueError:
            continue
        end = session.get("end_utc")
        session_end = None
        if isinstance(end, str):
            try:
                session_end = datetime.fromisoformat(end)
            except ValueError:
                session_end = None
        if session_end is None:
            session_end = video_end

        overlap_start = max(video.start_utc, session_start)
        overlap_end = min(video_end, session_end)
        overlap = (overlap_end - overlap_start).total_seconds()
        if overlap <= min_overlap_s:
            continue

        candidates.append(
            {
                "session": session,
                "overlap_s": overlap,
                "video_fraction": overlap / video.duration_s,
            }
        )

    candidates.sort(key=lambda item: item["overlap_s"], reverse=True)
    return candidates
