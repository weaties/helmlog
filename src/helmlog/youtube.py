"""YouTube upload via the Data API v3.

Handles OAuth2 credential management, resumable uploads, and metadata
templating for 360° sailing video.

Environment variables:
    YOUTUBE_CLIENT_SECRETS  Path to client_secrets.json from Google Cloud Console.
    YOUTUBE_TOKEN_FILE      Path to stored OAuth2 token (default ~/.helmlog-youtube-token.json).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
from googleapiclient.discovery import build as _build_service  # type: ignore[import-untyped]
from googleapiclient.http import MediaFileUpload  # type: ignore[import-untyped]
from loguru import logger

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadResult:
    """Result of a successful YouTube upload."""

    video_id: str
    youtube_url: str
    title: str


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------


def load_credentials(client_secrets: Path, token_file: Path) -> Credentials:
    """Load or create OAuth2 credentials for the YouTube Data API.

    On first run (no token file), opens a browser for interactive consent.
    Subsequent runs use the stored refresh token for unattended access.

    Args:
        client_secrets: Path to ``client_secrets.json`` from Google Cloud Console.
        token_file: Path where the OAuth2 token is cached.

    Returns:
        Valid Credentials object.
    """
    import contextlib

    creds: Credentials | None = None

    with contextlib.suppress(FileNotFoundError, ValueError):
        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)  # type: ignore[no-untyped-call]

    if creds and creds.expired and creds.refresh_token:
        logger.debug("Refreshing expired YouTube credentials")
        creds.refresh(Request())
        token_file.write_text(creds.to_json())  # type: ignore[no-untyped-call]
    elif creds is None or not creds.valid:
        logger.info("No valid YouTube credentials — starting OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), _SCOPES)
        creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())
        logger.info("YouTube credentials saved to {}", token_file)

    assert creds is not None, "Failed to obtain YouTube credentials"
    return creds


def build_service(creds: Credentials) -> object:
    """Build the YouTube API service object."""
    return _build_service("youtube", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Metadata templating
# ---------------------------------------------------------------------------


def build_title(
    *,
    event: str | None,
    session_type: str,
    race_num: int | None,
    date: str,
) -> str:
    """Build a YouTube video title from session metadata.

    Examples:
        >>> build_title(event="Ballard Cup", session_type="race", race_num=2, date="2026-08-10")
        'Ballard Cup Race 2 — 2026-08-10'
        >>> build_title(event=None, session_type="practice", race_num=None, date="2026-08-10")
        'Practice — 2026-08-10'
    """
    label = session_type.capitalize()
    if race_num is not None:
        label = f"{label} {race_num}"
    if event:
        label = f"{event} {label}"
    return f"{label} — {date}"


def build_description(
    *,
    session_url: str,
    start_utc: str,
    end_utc: str,
) -> str:
    """Build a YouTube video description with session metadata.

    Args:
        session_url: Deep link to the HelmLog session page.
        start_utc: Session start time (ISO 8601).
        end_utc: Session end time (ISO 8601).

    Returns:
        Multi-line description string.
    """
    return (
        f"360° sailing video — HelmLog\n"
        f"\n"
        f"Session: {session_url}\n"
        f"Start: {start_utc}\n"
        f"End: {end_utc}\n"
        f"\n"
        f"Recorded with Insta360 X4. Auto-uploaded by helmlog."
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def upload_video(
    *,
    file_path: Path,
    title: str,
    description: str,
    privacy: str = "unlisted",
    tags: list[str] | None = None,
    client_secrets: Path | None = None,
    token_file: Path | None = None,
) -> UploadResult:
    """Upload a video to YouTube via the Data API v3.

    Uses resumable upload for large files. Credentials are loaded from
    ``YOUTUBE_CLIENT_SECRETS`` and ``YOUTUBE_TOKEN_FILE`` env vars, or
    from the explicit parameters.

    Args:
        file_path: Path to the MP4 file to upload.
        title: Video title.
        description: Video description.
        privacy: Privacy status (``"private"``, ``"unlisted"``, ``"public"``).
        tags: Optional list of tags.
        client_secrets: Override for client secrets path.
        token_file: Override for token file path.

    Returns:
        :class:`UploadResult` with the YouTube video ID and URL.
    """
    secrets = client_secrets or Path(
        os.environ.get(
            "YOUTUBE_CLIENT_SECRETS",
            str(Path.home() / ".helmlog-youtube-client-secrets.json"),
        )
    )
    token = token_file or Path(
        os.environ.get(
            "YOUTUBE_TOKEN_FILE",
            str(Path.home() / ".helmlog-youtube-token.json"),
        )
    )

    creds = load_credentials(secrets, token)
    service = build_service(creds)

    body: dict[str, object] = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or ["sailing", "helmlog", "360"],
            "categoryId": "17",  # Sports
        },
        "status": {
            "privacyStatus": privacy,
        },
    }

    media = MediaFileUpload(str(file_path), resumable=True, chunksize=25 * 1024 * 1024)

    logger.info("Uploading {} to YouTube as {!r} ({})", file_path.name, title, privacy)

    # Run the upload in a thread to avoid blocking the event loop
    def _do_upload() -> str:
        request = service.videos().insert(  # type: ignore[attr-defined]
            part="snippet,status",
            body=body,
            media_body=media,
        )
        response: Any = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.debug("Upload progress: {:.0%}", status.progress())
        return str(response["id"])

    video_id: str = await asyncio.to_thread(_do_upload)

    youtube_url = f"https://youtu.be/{video_id}"
    logger.info("Upload complete: {} → {}", title, youtube_url)
    return UploadResult(video_id=video_id, youtube_url=youtube_url, title=title)
