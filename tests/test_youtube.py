"""Tests for YouTube upload module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from helmlog.youtube import (
    UploadResult,
    build_description,
    build_title,
    load_credentials,
    upload_video,
)

# ---------------------------------------------------------------------------
# Title templating
# ---------------------------------------------------------------------------


class TestBuildTitle:
    def test_race_with_event(self) -> None:
        title = build_title(event="Ballard Cup", session_type="race", race_num=2, date="2026-08-10")
        assert title == "Ballard Cup Race 2 — 2026-08-10"

    def test_race_without_event(self) -> None:
        title = build_title(event=None, session_type="race", race_num=1, date="2026-08-10")
        assert title == "Race 1 — 2026-08-10"

    def test_practice(self) -> None:
        title = build_title(event=None, session_type="practice", race_num=None, date="2026-08-10")
        assert title == "Practice — 2026-08-10"

    def test_practice_with_event(self) -> None:
        title = build_title(
            event="Ballard Cup", session_type="practice", race_num=None, date="2026-08-10"
        )
        assert title == "Ballard Cup Practice — 2026-08-10"

    def test_unknown_session_type(self) -> None:
        title = build_title(event=None, session_type="other", race_num=None, date="2026-08-10")
        assert title == "Other — 2026-08-10"


# ---------------------------------------------------------------------------
# Description templating
# ---------------------------------------------------------------------------


class TestBuildDescription:
    def test_basic_description(self) -> None:
        desc = build_description(
            session_url="https://corvopi:3002/history/42",
            start_utc="2026-08-10T14:05:30Z",
            end_utc="2026-08-10T15:30:00Z",
        )
        assert "https://corvopi:3002/history/42" in desc
        assert "14:05:30" in desc
        assert "360" in desc.lower() or "HelmLog" in desc


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------


class TestLoadCredentials:
    def test_loads_existing_token(self, tmp_path: Path) -> None:
        """When a valid token file exists, load and return credentials."""
        token_data = {
            "token": "ya29.test-access-token",
            "refresh_token": "1//test-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
        }
        token_file = tmp_path / "token.json"
        token_file.write_text(json.dumps(token_data))

        secrets_file = tmp_path / "client_secrets.json"
        secrets_file.write_text("{}")  # not used when token exists

        with patch("helmlog.youtube.Credentials") as mock_creds_cls:
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds.expired = False
            mock_creds_cls.from_authorized_user_file.return_value = mock_creds

            creds = load_credentials(secrets_file, token_file)

        assert creds is mock_creds
        mock_creds_cls.from_authorized_user_file.assert_called_once()

    def test_missing_token_triggers_flow(self, tmp_path: Path) -> None:
        """When no token file exists, run the interactive OAuth flow."""
        secrets_file = tmp_path / "client_secrets.json"
        secrets_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"

        with (
            patch("helmlog.youtube.InstalledAppFlow") as mock_flow_cls,
            patch("helmlog.youtube.Credentials") as mock_creds_cls,
        ):
            mock_creds_cls.from_authorized_user_file.side_effect = FileNotFoundError
            mock_flow = MagicMock()
            mock_creds = MagicMock()
            mock_creds.to_json.return_value = '{"token": "new"}'
            mock_flow.run_local_server.return_value = mock_creds
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow

            creds = load_credentials(secrets_file, token_file)

        assert creds is mock_creds
        mock_flow.run_local_server.assert_called_once_with(port=0)
        # Token should have been saved
        assert token_file.exists()


# ---------------------------------------------------------------------------
# Upload (mocked API)
# ---------------------------------------------------------------------------


class TestUploadVideo:
    @pytest.mark.asyncio
    async def test_upload_returns_result(self, tmp_path: Path) -> None:
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"\x00" * 100)

        mock_response = MagicMock()
        mock_response.__getitem__ = lambda _, k: {"id": "dQw4w9WgXcQ"}[k]

        mock_insert = MagicMock()
        mock_insert.next_chunk.return_value = (None, mock_response)

        mock_videos = MagicMock()
        mock_videos.insert.return_value = mock_insert

        mock_service = MagicMock()
        mock_service.videos.return_value = mock_videos

        with (
            patch("helmlog.youtube.load_credentials") as mock_load,
            patch("helmlog.youtube.build_service") as mock_build,
        ):
            mock_load.return_value = MagicMock()
            mock_build.return_value = mock_service

            result = await upload_video(
                file_path=video_file,
                title="Test Race",
                description="A test upload",
                privacy="unlisted",
            )

        assert isinstance(result, UploadResult)
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.youtube_url == "https://youtu.be/dQw4w9WgXcQ"
        assert result.title == "Test Race"

    @pytest.mark.asyncio
    async def test_upload_sets_privacy(self, tmp_path: Path) -> None:
        """Verify privacy setting is passed to the API."""
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"\x00" * 100)

        mock_response = MagicMock()
        mock_response.__getitem__ = lambda _, k: {"id": "abc123"}[k]

        mock_insert = MagicMock()
        mock_insert.next_chunk.return_value = (None, mock_response)

        mock_videos = MagicMock()
        mock_videos.insert.return_value = mock_insert

        mock_service = MagicMock()
        mock_service.videos.return_value = mock_videos

        with (
            patch("helmlog.youtube.load_credentials") as mock_load,
            patch("helmlog.youtube.build_service") as mock_build,
        ):
            mock_load.return_value = MagicMock()
            mock_build.return_value = mock_service

            await upload_video(
                file_path=video_file,
                title="Test",
                description="Desc",
                privacy="private",
            )

        # Check the body passed to videos().insert()
        call_kwargs = mock_videos.insert.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body")
        assert body["status"]["privacyStatus"] == "private"
