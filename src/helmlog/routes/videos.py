"""Route handlers for videos."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import VideoCreate, VideoUpdate, audit, get_storage

router = APIRouter()


def _video_deep_link(row: dict[str, Any], at_utc: datetime | None = None) -> dict[str, Any]:
    """Augment a race_videos row with a computed YouTube deep-link.

    If *at_utc* is supplied the link jumps to that moment in the video.
    Otherwise the link just opens the video from the beginning.
    Local videos (youtube_url starting with /) have no deep-link.
    """
    out = dict(row)
    # Local video files served by nginx have no YouTube deep-link.
    if row.get("youtube_url", "").startswith("/"):
        out["deep_link"] = None
        return out

    from helmlog.video import VideoSession  # local import to avoid circular deps

    sync_utc = datetime.fromisoformat(row["sync_utc"])
    duration_s = row["duration_s"]

    if at_utc is not None and duration_s is not None:
        vs = VideoSession(
            url=row["youtube_url"],
            video_id=row["video_id"],
            title=row["title"],
            duration_s=duration_s,
            sync_utc=sync_utc,
            sync_offset_s=row["sync_offset_s"],
        )
        out["deep_link"] = vs.url_at(at_utc)
    else:
        out["deep_link"] = None
    return out


@router.get("/api/sessions/{session_id}/videos")
async def api_list_videos(
    request: Request,
    session_id: int,
    at: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List videos linked to a session.

    Optional ``?at=<UTC ISO 8601>`` param computes a deep-link to that
    moment in each video.
    """
    storage = get_storage(request)
    # Videos are only supported on races (not audio sessions).
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await storage.list_race_videos(session_id)
    at_utc: datetime | None = None
    if at:
        try:
            at_utc = datetime.fromisoformat(at)
            if at_utc.tzinfo is None:
                at_utc = at_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
    return JSONResponse([_video_deep_link(r, at_utc) for r in rows])


@router.get("/api/sessions/{session_id}/videos/redirect")
async def api_videos_redirect(
    request: Request,
    session_id: int,
    at: str | None = None,
) -> RedirectResponse:
    """Redirect to the YouTube deep-link for a specific moment in the session's first video.

    Returns ``302 Location`` to the computed YouTube URL (with ``?t=<seconds>``).
    Returns ``404`` if the session doesn't exist or has no linked videos.
    Returns ``422`` if ``at`` is missing or cannot be parsed.
    """
    storage = get_storage(request)
    if not at:
        raise HTTPException(status_code=422, detail="'at' query parameter is required")
    try:
        at_utc = datetime.fromisoformat(at)
        if at_utc.tzinfo is None:
            at_utc = at_utc.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await storage.list_race_videos(session_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No videos linked to this session")
    # Use the first video by created_at (list_race_videos returns ASC order).
    row = rows[0]
    enriched = _video_deep_link(row, at_utc)
    url = enriched["deep_link"] or row["youtube_url"]
    return RedirectResponse(url=url, status_code=302)


@router.get("/api/videos/redirect")
async def api_videos_redirect_by_time(
    request: Request,
    at: str | None = None,
) -> RedirectResponse:
    """Resolve the race active at ``at`` and redirect to its first video.

    Designed for Grafana Data Links — no session_id required.  Grafana
    passes ``${__value.time:date:iso}`` as the ``at`` parameter and this
    endpoint resolves the correct race automatically.

    Returns ``302 Location`` to the YouTube deep-link with ``?t=<seconds>``.
    Returns ``404`` if no race covers that timestamp or the race has no video.
    Returns ``422`` if ``at`` is missing or cannot be parsed.
    """
    storage = get_storage(request)
    if not at:
        raise HTTPException(status_code=422, detail="'at' query parameter is required")
    try:
        at_utc = datetime.fromisoformat(at)
        if at_utc.tzinfo is None:
            at_utc = at_utc.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904

    at_iso = at_utc.isoformat()
    cur = await storage._conn().execute(
        """
        SELECT id FROM races
        WHERE start_utc <= ?
          AND (end_utc >= ? OR end_utc IS NULL)
        ORDER BY start_utc DESC
        LIMIT 1
        """,
        (at_iso, at_iso),
    )
    race_row = await cur.fetchone()
    if race_row is None:
        raise HTTPException(status_code=404, detail="No race found at this timestamp")

    session_id = race_row["id"]
    rows = await storage.list_race_videos(session_id)
    if not rows:
        raise HTTPException(status_code=404, detail="No videos linked to this session")

    row = rows[0]
    enriched = _video_deep_link(row, at_utc)
    url = enriched["deep_link"] or row["youtube_url"]
    return RedirectResponse(url=url, status_code=302)


@router.post("/api/sessions/{session_id}/videos", status_code=201)
async def api_add_video(
    request: Request,
    session_id: int,
    body: VideoCreate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Link a YouTube video to a race session.

    The caller supplies a sync point: a UTC wall-clock time and the
    corresponding video player position (seconds).  This pins the video
    timeline to logger time.
    """
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Parse the sync UTC
    try:
        sync_utc = datetime.fromisoformat(body.sync_utc)
        if sync_utc.tzinfo is None:
            sync_utc = sync_utc.replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904

    # Extract YouTube video ID and fetch metadata via yt-dlp if available
    from helmlog.video import VideoLinker

    video_id = ""
    title = ""
    duration_s: float | None = None
    try:
        linker = VideoLinker()
        vs = await linker.create_session(body.youtube_url, sync_utc, body.sync_offset_s)
        video_id = vs.video_id
        title = vs.title
        duration_s = vs.duration_s
    except Exception:  # noqa: BLE001
        # yt-dlp unavailable or network error — store the URL as-is.
        # Extract video ID from URL heuristically.
        import re

        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", body.youtube_url)
        video_id = m.group(1) if m else ""
        title = ""
        duration_s = None

    row_id = await storage.add_race_video(
        race_id=session_id,
        youtube_url=body.youtube_url,
        video_id=video_id,
        title=title,
        label=body.label,
        sync_utc=sync_utc,
        sync_offset_s=body.sync_offset_s,
        duration_s=duration_s,
        user_id=_user.get("id"),
    )
    rows = await storage.list_race_videos(session_id)
    row = next(r for r in rows if r["id"] == row_id)
    await audit(request, "video.add", detail=body.youtube_url, user=_user)
    return JSONResponse(_video_deep_link(row), status_code=201)


@router.patch("/api/videos/{video_id}", status_code=200)
async def api_update_video(
    request: Request,
    video_id: int,
    body: VideoUpdate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Update label or sync calibration on an existing video link."""
    storage = get_storage(request)
    sync_utc: datetime | None = None
    if body.sync_utc is not None:
        try:
            sync_utc = datetime.fromisoformat(body.sync_utc)
            if sync_utc.tzinfo is None:
                sync_utc = sync_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904
    found = await storage.update_race_video(
        video_id,
        label=body.label,
        sync_utc=sync_utc,
        sync_offset_s=body.sync_offset_s,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Video not found")
    await audit(request, "video.update", detail=str(video_id), user=_user)
    return JSONResponse({"id": video_id, "updated": True})


@router.delete("/api/videos/{video_id}", status_code=204)
async def api_delete_video(
    request: Request,
    video_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    """Remove a video link."""
    storage = get_storage(request)
    found = await storage.delete_race_video(video_id)
    if not found:
        raise HTTPException(status_code=404, detail="Video not found")
    await audit(request, "video.delete", detail=str(video_id), user=_user)
