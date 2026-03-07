"""Tests for VideoSession sync math and storage round-trips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from helmlog.video import VideoSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SYNC_UTC = datetime(2025, 8, 10, 14, 5, 30, tzinfo=UTC)  # starting gun UTC
_SYNC_OFFSET = 330.0  # starting gun at 5:30 into video


def _make_session(
    duration_s: float = 7200.0,  # 2-hour race video
    sync_utc: datetime = _SYNC_UTC,
    sync_offset_s: float = _SYNC_OFFSET,
) -> VideoSession:
    return VideoSession(
        url="https://youtu.be/dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        title="J105 Race — August 2025",
        duration_s=duration_s,
        sync_utc=sync_utc,
        sync_offset_s=sync_offset_s,
    )


# ---------------------------------------------------------------------------
# video_offset_at
# ---------------------------------------------------------------------------


class TestVideoOffsetAt:
    def test_at_sync_point(self) -> None:
        s = _make_session()
        assert s.video_offset_at(_SYNC_UTC) == pytest.approx(_SYNC_OFFSET)

    def test_before_sync_point(self) -> None:
        s = _make_session()
        # 60 seconds before the sync point → offset decreases by 60
        utc = _SYNC_UTC - timedelta(seconds=60)
        assert s.video_offset_at(utc) == pytest.approx(_SYNC_OFFSET - 60)

    def test_after_sync_point(self) -> None:
        s = _make_session()
        utc = _SYNC_UTC + timedelta(seconds=90)
        assert s.video_offset_at(utc) == pytest.approx(_SYNC_OFFSET + 90)

    def test_sync_at_start(self) -> None:
        """When sync_offset_s=0 (--start shorthand), offset equals elapsed time."""
        s = _make_session(sync_offset_s=0.0)
        utc = _SYNC_UTC + timedelta(seconds=600)
        assert s.video_offset_at(utc) == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# covers
# ---------------------------------------------------------------------------


class TestCovers:
    def test_covers_sync_point(self) -> None:
        assert _make_session().covers(_SYNC_UTC)

    def test_covers_video_start(self) -> None:
        s = _make_session()
        video_start_utc = _SYNC_UTC - timedelta(seconds=_SYNC_OFFSET)
        assert s.covers(video_start_utc)

    def test_covers_video_end(self) -> None:
        s = _make_session(duration_s=7200.0)
        video_end_utc = _SYNC_UTC + timedelta(seconds=7200.0 - _SYNC_OFFSET)
        assert s.covers(video_end_utc)

    def test_not_covers_before_start(self) -> None:
        s = _make_session()
        too_early = _SYNC_UTC - timedelta(seconds=_SYNC_OFFSET + 1)
        assert not s.covers(too_early)

    def test_not_covers_after_end(self) -> None:
        s = _make_session(duration_s=7200.0)
        too_late = _SYNC_UTC + timedelta(seconds=7200.0 - _SYNC_OFFSET + 1)
        assert not s.covers(too_late)


# ---------------------------------------------------------------------------
# url_at
# ---------------------------------------------------------------------------


class TestUrlAt:
    def test_url_at_sync_point(self) -> None:
        s = _make_session()
        url = s.url_at(_SYNC_UTC)
        assert url == f"https://youtu.be/dQw4w9WgXcQ?t={int(_SYNC_OFFSET)}"

    def test_url_at_offset_60s_later(self) -> None:
        s = _make_session()
        utc = _SYNC_UTC + timedelta(seconds=60)
        url = s.url_at(utc)
        assert url == f"https://youtu.be/dQw4w9WgXcQ?t={int(_SYNC_OFFSET + 60)}"

    def test_url_truncates_to_int(self) -> None:
        """Fractional seconds should be truncated, not rounded."""
        s = _make_session(sync_offset_s=0.0)
        utc = _SYNC_UTC + timedelta(seconds=10.9)
        url = s.url_at(utc)
        assert url is not None
        assert url.endswith("?t=10")

    def test_url_none_before_video(self) -> None:
        s = _make_session()
        too_early = _SYNC_UTC - timedelta(seconds=_SYNC_OFFSET + 1)
        assert s.url_at(too_early) is None

    def test_url_none_after_video(self) -> None:
        s = _make_session(duration_s=7200.0)
        too_late = _SYNC_UTC + timedelta(seconds=7200.0 - _SYNC_OFFSET + 1)
        assert s.url_at(too_late) is None

    def test_url_at_video_second_zero(self) -> None:
        """The very first second of the video should give ?t=0."""
        s = _make_session(sync_offset_s=0.0)
        url = s.url_at(_SYNC_UTC)
        assert url == "https://youtu.be/dQw4w9WgXcQ?t=0"


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


class TestVideoSessionStorage:
    async def test_write_and_list(self, storage: object) -> None:
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)
        session = _make_session()
        await storage.write_video_session(session)

        sessions = await storage.list_video_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s.url == session.url
        assert s.video_id == session.video_id
        assert s.title == session.title
        assert s.duration_s == pytest.approx(session.duration_s)
        assert s.sync_utc == session.sync_utc
        assert s.sync_offset_s == pytest.approx(session.sync_offset_s)

    async def test_list_ordered_by_sync_utc(self, storage: object) -> None:
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)
        s1 = _make_session(sync_utc=datetime(2025, 8, 10, 14, 0, 0, tzinfo=UTC))
        s2 = _make_session(sync_utc=datetime(2025, 8, 10, 10, 0, 0, tzinfo=UTC))
        await storage.write_video_session(s1)
        await storage.write_video_session(s2)

        sessions = await storage.list_video_sessions()
        assert len(sessions) == 2
        assert sessions[0].sync_utc < sessions[1].sync_utc

    async def test_list_empty(self, storage: object) -> None:
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)
        sessions = await storage.list_video_sessions()
        assert sessions == []
