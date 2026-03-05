"""Unit tests for logger.cameras — OSC camera control and storage integration."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from logger.cameras import (
    Camera,
    get_status,
    parse_cameras_config,
    start_all,
    start_camera,
    stop_all,
    stop_camera,
)

# ---------------------------------------------------------------------------
# parse_cameras_config
# ---------------------------------------------------------------------------


def test_parse_cameras_config_single() -> None:
    result = parse_cameras_config("main:192.168.8.50")
    assert len(result) == 1
    assert result[0].name == "main"
    assert result[0].ip == "192.168.8.50"
    assert result[0].model == "insta360-x4"


def test_parse_cameras_config_multiple() -> None:
    result = parse_cameras_config("port:192.168.8.50,starboard:192.168.8.51")
    assert len(result) == 2
    assert result[0].name == "port"
    assert result[1].name == "starboard"


def test_parse_cameras_config_empty() -> None:
    assert parse_cameras_config("") == []
    assert parse_cameras_config("  ") == []


def test_parse_cameras_config_whitespace() -> None:
    result = parse_cameras_config(" port : 192.168.8.50 , stern : 192.168.8.51 ")
    assert len(result) == 2
    assert result[0].name == "port"
    assert result[0].ip == "192.168.8.50"


def test_parse_cameras_config_invalid_entry() -> None:
    """Invalid entries (missing colon) are skipped."""
    result = parse_cameras_config("good:192.168.8.50,bad_no_colon")
    assert len(result) == 1
    assert result[0].name == "good"


# ---------------------------------------------------------------------------
# start_camera / stop_camera / get_status (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_camera_success() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=5.0)

    assert status.recording is True
    assert status.error is None
    assert status.latency_ms is not None
    assert status.latency_ms >= 0


@pytest.mark.asyncio
async def test_start_camera_timeout() -> None:
    cam = Camera(name="slow", ip="192.168.8.50")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.TimeoutException("timed out")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=1.0)

    assert status.recording is False
    assert status.error is not None
    assert "timed out" in status.error


@pytest.mark.asyncio
async def test_start_camera_connection_error() -> None:
    cam = Camera(name="offline", ip="192.168.8.99")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await start_camera(cam, timeout=5.0)

    assert status.recording is False
    assert "Connection refused" in (status.error or "")


@pytest.mark.asyncio
async def test_stop_camera_success() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await stop_camera(cam, timeout=5.0)

    assert status.recording is False
    assert status.error is None


@pytest.mark.asyncio
async def test_get_status_recording() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": {"options": {"captureStatus": "shooting"}}
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await get_status(cam)

    assert status.recording is True
    assert status.error is None


@pytest.mark.asyncio
async def test_get_status_idle() -> None:
    cam = Camera(name="test", ip="192.168.8.50")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": {"options": {"captureStatus": "idle"}}
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await get_status(cam)

    assert status.recording is False
    assert status.error is None


@pytest.mark.asyncio
async def test_get_status_connection_error() -> None:
    cam = Camera(name="offline", ip="192.168.8.99")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("unreachable")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        status = await get_status(cam)

    assert status.recording is False
    assert status.error is not None


# ---------------------------------------------------------------------------
# start_all / stop_all (with storage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_all_parallel(storage: object) -> None:
    """Two cameras, both succeed → two camera_sessions rows."""
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    # Create a race first
    from datetime import datetime as _dt

    race = await storage.start_race("TestEvent", _dt.now(UTC), "2026-03-04", 1, "test-race")
    race_id = race.id

    cams = [Camera(name="port", ip="1.1.1.1"), Camera(name="stern", ip="2.2.2.2")]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        statuses = await start_all(cams, race_id, storage)

    assert len(statuses) == 2
    assert all(s.recording for s in statuses)

    # Verify storage rows
    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 2
    assert {r["camera_name"] for r in rows} == {"port", "stern"}
    assert all(r["recording_started_utc"] is not None for r in rows)


@pytest.mark.asyncio
async def test_start_all_one_fails(storage: object) -> None:
    """First camera times out, second succeeds → partial success."""
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("TestEvent", _dt.now(UTC), "2026-03-04", 2, "test-race-2")
    race_id = race.id

    cams = [Camera(name="broken", ip="1.1.1.1"), Camera(name="good", ip="2.2.2.2")]

    # broken camera raises, good camera works
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    call_count = 0

    async def _mock_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Connection refused")
        return mock_resp

    mock_client = AsyncMock()
    mock_client.post = _mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        statuses = await start_all(cams, race_id, storage)

    assert len(statuses) == 2
    # One failed, one succeeded
    errors = [s for s in statuses if s.error]
    ok = [s for s in statuses if not s.error]
    assert len(errors) == 1
    assert len(ok) == 1

    # Both should have storage rows
    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_stop_all_updates_rows(storage: object) -> None:
    """stop_all updates camera_sessions with stopped_utc."""
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("TestEvent", _dt.now(UTC), "2026-03-04", 3, "test-race-3")
    race_id = race.id

    cams = [Camera(name="main", ip="1.1.1.1")]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = '{"state": "done"}'

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await start_all(cams, race_id, storage)
        await stop_all(cams, race_id, storage)

    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 1
    assert rows[0]["recording_stopped_utc"] is not None


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_camera_session_roundtrip(storage: object) -> None:
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("Evt", _dt.now(UTC), "2026-03-04", 4, "test-race-4")
    race_id = race.id
    now = _dt.now(UTC)

    row_id = await storage.add_camera_session(
        session_id=race_id,
        camera_name="bow",
        camera_ip="192.168.8.50",
        started_utc=now,
        sync_offset_ms=42,
        error=None,
    )
    assert row_id > 0

    rows = await storage.list_camera_sessions(race_id)
    assert len(rows) == 1
    assert rows[0]["camera_name"] == "bow"
    assert rows[0]["sync_offset_ms"] == 42
    assert rows[0]["error"] is None


@pytest.mark.asyncio
async def test_update_camera_session_stop(storage: object) -> None:
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("Evt", _dt.now(UTC), "2026-03-04", 5, "test-race-5")
    race_id = race.id
    now = _dt.now(UTC)

    await storage.add_camera_session(
        session_id=race_id,
        camera_name="bow",
        camera_ip="192.168.8.50",
        started_utc=now,
        sync_offset_ms=10,
        error=None,
    )

    updated = await storage.update_camera_session_stop(
        session_id=race_id,
        camera_name="bow",
        stopped_utc=now,
        error=None,
    )
    assert updated is True

    rows = await storage.list_camera_sessions(race_id)
    assert rows[0]["recording_stopped_utc"] is not None


@pytest.mark.asyncio
async def test_list_unlinked_camera_sessions(storage: object) -> None:
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    from datetime import datetime as _dt

    race = await storage.start_race("Evt", _dt.now(UTC), "2026-03-04", 6, "test-race-6")
    race_id = race.id
    now = _dt.now(UTC)

    await storage.add_camera_session(
        session_id=race_id,
        camera_name="bow",
        camera_ip="192.168.8.50",
        started_utc=now,
        sync_offset_ms=10,
        error=None,
    )

    unlinked = await storage.list_unlinked_camera_sessions()
    assert len(unlinked) >= 1
    assert any(r["camera_name"] == "bow" for r in unlinked)
