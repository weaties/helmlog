"""Tests for the video pipeline orchestration (fetch → match → upload → link)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from helmlog.insta360 import InstaRecording
from helmlog.pipeline import PipelineConfig, process_recording
from helmlog.youtube import UploadResult


def _make_sessions() -> list[dict[str, Any]]:
    """Two sessions: a race and a practice."""
    return [
        {
            "id": 10,
            "name": "Race 1",
            "start_utc": "2026-08-10T14:00:00+00:00",
            "end_utc": "2026-08-10T15:00:00+00:00",
            "event": "Ballard Cup",
            "race_num": 1,
            "session_type": "race",
        },
        {
            "id": 20,
            "name": "Practice",
            "start_utc": "2026-08-10T16:00:00+00:00",
            "end_utc": "2026-08-10T17:30:00+00:00",
            "event": None,
            "race_num": None,
            "session_type": "practice",
        },
    ]


def _config(pi_cookie: str = "test-cookie") -> PipelineConfig:
    return PipelineConfig(
        pi_api_url="http://corvopi:3002",
        pi_session_cookie=pi_cookie,
        privacy="unlisted",
        timezone="America/Los_Angeles",
    )


def _upload_result() -> UploadResult:
    return UploadResult(
        video_id="abc123",
        youtube_url="https://youtu.be/abc123",
        title="Ballard Cup Race 1 — 2026-08-10",
    )


class TestProcessRecording:
    """Tests for the process_recording orchestration function."""

    @pytest.mark.asyncio
    async def test_matched_session_uploads_and_links(self) -> None:
        """When a session matches, uploads with rich metadata and links to Pi."""
        sessions = _make_sessions()
        # Recording at 7:05 AM PDT = 14:05 UTC → matches Race 1 (14:00-15:00)
        rec = InstaRecording(timestamp_str="20260810_070500", segments=[], total_size_bytes=0)
        cfg = _config()

        mock_upload = AsyncMock(return_value=_upload_result())
        mock_link = AsyncMock(return_value=httpx.Response(201))

        with (
            patch("helmlog.pipeline.upload_video", mock_upload),
            patch("helmlog.pipeline._link_video_on_pi", mock_link),
        ):
            result = await process_recording(
                rec=rec,
                video_path="/tmp/test.mp4",
                sessions=sessions,
                config=cfg,
            )

        assert result.uploaded is True
        assert result.video_id == "abc123"
        assert result.session_id == 10
        assert result.linked is True
        # Title should use session metadata
        assert "Ballard Cup" in mock_upload.call_args.kwargs["title"]
        assert "Race 1" in mock_upload.call_args.kwargs["title"]

    @pytest.mark.asyncio
    async def test_no_matching_session_uploads_with_generic_title(self) -> None:
        """When no session matches, upload with generic metadata, no linking."""
        sessions = _make_sessions()
        # Recording at 10 PM PDT = 05:00 UTC next day → no match
        rec = InstaRecording(timestamp_str="20260810_220000", segments=[], total_size_bytes=0)
        cfg = _config()

        mock_upload = AsyncMock(return_value=_upload_result())

        with patch("helmlog.pipeline.upload_video", mock_upload):
            result = await process_recording(
                rec=rec,
                video_path="/tmp/test.mp4",
                sessions=sessions,
                config=cfg,
            )

        assert result.uploaded is True
        assert result.session_id is None
        assert result.linked is False
        # Title should be generic
        assert "Sailing" in mock_upload.call_args.kwargs["title"]

    @pytest.mark.asyncio
    async def test_no_cookie_skips_linking(self) -> None:
        """When PI_SESSION_COOKIE is empty, uploads but skips linking."""
        sessions = _make_sessions()
        rec = InstaRecording(timestamp_str="20260810_070500", segments=[], total_size_bytes=0)
        cfg = _config(pi_cookie="")

        mock_upload = AsyncMock(return_value=_upload_result())
        mock_link = AsyncMock()

        with (
            patch("helmlog.pipeline.upload_video", mock_upload),
            patch("helmlog.pipeline._link_video_on_pi", mock_link),
        ):
            result = await process_recording(
                rec=rec,
                video_path="/tmp/test.mp4",
                sessions=sessions,
                config=cfg,
            )

        assert result.uploaded is True
        assert result.session_id == 10
        assert result.linked is False
        mock_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_link_failure_still_reports_uploaded(self) -> None:
        """If the Pi link call fails, the video is still uploaded."""
        sessions = _make_sessions()
        rec = InstaRecording(timestamp_str="20260810_070500", segments=[], total_size_bytes=0)
        cfg = _config()

        mock_upload = AsyncMock(return_value=_upload_result())
        mock_link = AsyncMock(return_value=httpx.Response(401))

        with (
            patch("helmlog.pipeline.upload_video", mock_upload),
            patch("helmlog.pipeline._link_video_on_pi", mock_link),
        ):
            result = await process_recording(
                rec=rec,
                video_path="/tmp/test.mp4",
                sessions=sessions,
                config=cfg,
            )

        assert result.uploaded is True
        assert result.linked is False

    @pytest.mark.asyncio
    async def test_upload_failure_returns_not_uploaded(self) -> None:
        """If upload_video raises, result reflects the failure."""
        sessions = _make_sessions()
        rec = InstaRecording(timestamp_str="20260810_070500", segments=[], total_size_bytes=0)
        cfg = _config()

        mock_upload = AsyncMock(side_effect=RuntimeError("quota exceeded"))

        with patch("helmlog.pipeline.upload_video", mock_upload):
            result = await process_recording(
                rec=rec,
                video_path="/tmp/test.mp4",
                sessions=sessions,
                config=cfg,
            )

        assert result.uploaded is False
        assert result.error == "quota exceeded"


class TestFetchSessions:
    """Tests for fetch_sessions_from_pi."""

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        from helmlog.pipeline import fetch_sessions_from_pi

        mock_response = httpx.Response(
            200,
            json={"sessions": _make_sessions(), "total": 2},
            request=httpx.Request("GET", "http://test/api/sessions"),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            sessions = await fetch_sessions_from_pi("http://corvopi:3002")

        assert len(sessions) == 2
        assert sessions[0]["id"] == 10

    @pytest.mark.asyncio
    async def test_fetch_returns_list_format(self) -> None:
        """Handle APIs that return a plain list instead of {sessions: [...]}."""
        from helmlog.pipeline import fetch_sessions_from_pi

        mock_response = httpx.Response(
            200,
            json=_make_sessions(),
            request=httpx.Request("GET", "http://test/api/sessions"),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            sessions = await fetch_sessions_from_pi("http://corvopi:3002")

        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_empty(self) -> None:
        from helmlog.pipeline import fetch_sessions_from_pi

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            sessions = await fetch_sessions_from_pi("http://corvopi:3002")

        assert sessions == []

    @pytest.mark.asyncio
    async def test_fetch_non_200_returns_empty(self) -> None:
        from helmlog.pipeline import fetch_sessions_from_pi

        mock_response = httpx.Response(
            500,
            request=httpx.Request("GET", "http://test/api/sessions"),
        )
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            sessions = await fetch_sessions_from_pi("http://corvopi:3002")

        assert sessions == []


class TestLinkVideoOnPi:
    """Tests for _link_video_on_pi."""

    @pytest.mark.asyncio
    async def test_link_success(self) -> None:
        from helmlog.pipeline import _link_video_on_pi

        mock_response = httpx.Response(
            201,
            request=httpx.Request("POST", "http://test/api/sessions/10/videos"),
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await _link_video_on_pi(
                pi_api_url="http://corvopi:3002",
                session_id=10,
                youtube_url="https://youtu.be/abc123",
                sync_utc="2026-08-10T14:05:00+00:00",
                session_cookie="test-cookie",
            )

        assert resp.status_code == 201
        # Verify the POST body
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["json"]["youtube_url"] == "https://youtu.be/abc123"
        assert call_kwargs["cookies"]["session"] == "test-cookie"

    @pytest.mark.asyncio
    async def test_link_network_error_raises(self) -> None:
        from helmlog.pipeline import _link_video_on_pi

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(httpx.ConnectError),
        ):
            await _link_video_on_pi(
                pi_api_url="http://corvopi:3002",
                session_id=10,
                youtube_url="https://youtu.be/abc123",
                sync_utc="2026-08-10T14:05:00+00:00",
                session_cookie="test-cookie",
            )
