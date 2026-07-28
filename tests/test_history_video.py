"""History-page video surfacing (#827).

The history list already carried ``first_video_url`` but nothing to build a
thumbnail from. These tests pin the added ``first_video_id`` / ``video_count``
fields and the deterministic choice of *which* video is "first".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _race_with_videos(storage: Storage, videos: list[tuple[str, str]]) -> int:
    """Create a race and link the given ``(video_id, url)`` pairs to it."""
    race = await storage.start_race(
        event="TestEvent",
        start_utc=datetime(2026, 7, 21, 17, 24, 52, tzinfo=UTC),
        date_str="2026-07-21",
        race_num=1,
        name="20260721-rw-d2r1",
    )
    await storage.end_race(race.id, datetime(2026, 7, 21, 19, 27, 55, tzinfo=UTC))
    for video_id, url in videos:
        await storage.add_race_video(
            race_id=race.id,
            youtube_url=url,
            video_id=video_id,
            title=f"title-{video_id}",
            label="",
            sync_utc=datetime(2026, 7, 21, 17, 24, 52, tzinfo=UTC),
            sync_offset_s=0.0,
        )
    return race.id


@pytest.mark.asyncio
async def test_session_with_video_exposes_video_id_and_count(storage: Storage) -> None:
    race_id = await _race_with_videos(storage, [("abc123XYZ_-", "https://youtu.be/abc123XYZ_-")])

    _total, sessions = await storage.list_sessions()
    row = next(s for s in sessions if s["id"] == race_id)

    assert row["first_video_id"] == "abc123XYZ_-"
    assert row["first_video_url"] == "https://youtu.be/abc123XYZ_-"
    assert row["video_count"] == 1


@pytest.mark.asyncio
async def test_session_without_video_reports_none_and_zero(storage: Storage) -> None:
    race = await storage.start_race(
        event="TestEvent",
        start_utc=datetime(2026, 7, 18, 21, 24, 38, tzinfo=UTC),
        date_str="2026-07-18",
        race_num=1,
        name="20260718-no-video",
    )
    await storage.end_race(race.id, datetime(2026, 7, 18, 22, 22, 41, tzinfo=UTC))

    _total, sessions = await storage.list_sessions()
    row = next(s for s in sessions if s["id"] == race.id)

    assert row["first_video_id"] is None
    assert row["first_video_url"] is None
    assert row["video_count"] == 0


@pytest.mark.asyncio
async def test_multiple_videos_counted_and_first_is_lowest_row_id(storage: Storage) -> None:
    """``first_video_*`` must be stable, not whatever SQLite happens to return.

    The pre-existing subquery used a bare ``LIMIT 1`` with no ``ORDER BY``,
    so the "first" video was unspecified. Insertion order decides it now.
    """
    race_id = await _race_with_videos(
        storage,
        [
            ("firstVid_01", "https://youtu.be/firstVid_01"),
            ("secondVid02", "https://youtu.be/secondVid02"),
            ("thirdVid_03", "https://youtu.be/thirdVid_03"),
        ],
    )

    _total, sessions = await storage.list_sessions()
    row = next(s for s in sessions if s["id"] == race_id)

    assert row["video_count"] == 3
    assert row["first_video_id"] == "firstVid_01"
    assert row["first_video_url"] == "https://youtu.be/firstVid_01"


@pytest.mark.asyncio
async def test_debrief_rows_carry_null_video_fields(storage: Storage) -> None:
    """The debrief UNION branch must supply matching columns, not blow up."""
    from helmlog.audio import AudioSession

    await storage.write_audio_session(
        AudioSession(
            file_path="/tmp/debrief.wav",
            device_name="test-mic",
            start_utc=datetime(2026, 7, 21, 23, 0, tzinfo=UTC),
            end_utc=datetime(2026, 7, 21, 23, 30, tzinfo=UTC),
            sample_rate=48_000,
            channels=1,
        ),
        session_type="debrief",
        name="Debrief after d2r4",
    )

    _total, sessions = await storage.list_sessions(session_type="debrief")
    assert sessions, "expected the debrief to be listed"
    for row in sessions:
        assert row["first_video_id"] is None
        assert row["video_count"] == 0


@pytest.mark.asyncio
async def test_api_sessions_surfaces_video_fields(storage: Storage) -> None:
    race_id = await _race_with_videos(storage, [("apiVid_0001", "https://youtu.be/apiVid_0001")])

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions?type=race")

    assert resp.status_code == 200
    row = next(s for s in resp.json()["sessions"] if s["id"] == race_id)
    assert row["first_video_id"] == "apiVid_0001"
    assert row["video_count"] == 1
