"""YouTube video timestamping — correlate recorded video with logged data.

A VideoSession stores a sync point: a (UTC wall-clock time, video offset)
pair that anchors the video timeline to real time.  Given any UTC timestamp,
video_offset_at() returns the corresponding seconds into the video and
url_at() returns the YouTube deep-link URL with ?t=<seconds>.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from loguru import logger

if TYPE_CHECKING:
    from datetime import datetime

# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoSession:
    """A YouTube video linked to logged instrument data via a time sync point.

    The sync point is a (sync_utc, sync_offset_s) pair that says:
        "at UTC time sync_utc, the video playback position was sync_offset_s seconds."

    This lets you link the video even when you don't know the exact start time —
    just pick any identifiable moment (e.g. the starting gun) and note both
    the UTC time from the instrument log and where it appears in the video.
    """

    url: str  # original YouTube URL
    video_id: str  # extracted video ID, e.g. "dQw4w9WgXcQ"
    title: str
    duration_s: float  # total video duration in seconds

    sync_utc: datetime  # UTC wall-clock time at the sync point
    sync_offset_s: float  # seconds into the video at sync_utc

    def video_offset_at(self, utc: datetime) -> float:
        """Return the video playback position (seconds) at the given UTC time."""
        return self.sync_offset_s + (utc - self.sync_utc).total_seconds()

    def covers(self, utc: datetime) -> bool:
        """True if the given UTC time falls within the video's duration."""
        offset = self.video_offset_at(utc)
        return 0.0 <= offset <= self.duration_s

    def url_at(self, utc: datetime) -> str | None:
        """Return a YouTube deep-link URL with ?t= for the given UTC time.

        Returns None if the timestamp falls outside the video.
        """
        offset = self.video_offset_at(utc)
        if offset < 0 or offset > self.duration_s:
            return None
        return f"https://youtu.be/{self.video_id}?t={int(offset)}"


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------


class _VideoInfo(TypedDict):
    video_id: str
    title: str
    duration_s: float


# ---------------------------------------------------------------------------
# VideoLinker
# ---------------------------------------------------------------------------


class VideoLinker:
    """Fetch YouTube metadata and create VideoSessions."""

    async def create_session(
        self,
        url: str,
        sync_utc: datetime,
        sync_offset_s: float,
    ) -> VideoSession:
        """Fetch metadata from YouTube and return a VideoSession.

        Args:
            url:            YouTube video URL.
            sync_utc:       UTC wall-clock time at the sync point.
            sync_offset_s:  Seconds into the video at sync_utc.

        Returns:
            A VideoSession ready to be stored.

        Raises:
            RuntimeError: If yt-dlp cannot retrieve the metadata.
        """
        logger.info("Fetching video metadata for: {}", url)
        info = await asyncio.wait_for(
            asyncio.to_thread(self._fetch_sync, url),
            timeout=30.0,
        )

        session = VideoSession(
            url=url,
            video_id=info["video_id"],
            title=info["title"],
            duration_s=info["duration_s"],
            sync_utc=sync_utc,
            sync_offset_s=sync_offset_s,
        )
        logger.info(
            "Video: {!r} ({:.0f}s) — sync: UTC {} ↔ video t={:.0f}s",
            session.title,
            session.duration_s,
            sync_utc.isoformat(),
            sync_offset_s,
        )
        return session

    def _fetch_sync(self, url: str) -> _VideoInfo:
        """Synchronous yt-dlp metadata fetch (runs in a thread)."""
        try:
            import yt_dlp  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("yt-dlp is not installed") from exc

        ydl_opts: dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 10,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            raise RuntimeError(f"yt-dlp returned no info for {url!r}")

        return {
            "video_id": str(info.get("id", "")),
            "title": str(info.get("title", "")),
            "duration_s": float(info.get("duration", 0.0)),
        }
