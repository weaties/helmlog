"""Tests for src/logger/web.py — race API and audio integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helmlog.audio import AudioConfig, AudioDeviceNotFoundError, AudioSession
from helmlog.nmea2000 import (
    PGN_COG_SOG_RAPID,
    PGN_SPEED_THROUGH_WATER,
    PGN_VESSEL_HEADING,
    PGN_WIND_DATA,
    COGSOGRecord,
    HeadingRecord,
    SpeedRecord,
    WindRecord,
)
from helmlog.web import _get_git_info, create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_git_info_includes_hostname() -> None:
    """_get_git_info() should include the system hostname."""
    import socket

    info = _get_git_info()
    assert info  # non-empty (we're in a git repo)
    assert socket.gethostname() in info


_DEVICE = "Gordik 2T1R USB Audio"
_START_UTC = datetime(2026, 2, 26, 14, 0, 0, tzinfo=UTC)
_END_UTC = datetime(2026, 2, 26, 14, 30, 0, tzinfo=UTC)


def _make_session(*, end_utc: datetime | None = None) -> AudioSession:
    return AudioSession(
        file_path="/tmp/audio/20260226-TestRegatta-1.wav",
        device_name=_DEVICE,
        start_utc=_START_UTC,
        end_utc=end_utc,
        sample_rate=48000,
        channels=1,
    )


def _make_recorder(*, raises_on_start: bool = False) -> MagicMock:
    """Return a mock AudioRecorder with async start/stop."""
    recorder = MagicMock()
    if raises_on_start:
        recorder.start = AsyncMock(side_effect=AudioDeviceNotFoundError("no device"))
    else:
        recorder.start = AsyncMock(return_value=_make_session())
        recorder.stop = AsyncMock(return_value=_make_session(end_utc=_END_UTC))
    return recorder


async def _set_event(client: httpx.AsyncClient, name: str = "TestRegatta") -> None:
    resp = await client.post("/api/event", json={"event_name": name})
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Tests — basic API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_endpoint_returns_json(storage: Storage) -> None:
    """GET /api/state returns a valid JSON response."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/state")

    assert resp.status_code == 200
    data = resp.json()
    assert "date" in data
    assert "current_race" in data
    assert "today_races" in data


@pytest.mark.asyncio
async def test_start_race_no_event_returns_422(storage: Storage) -> None:
    """POST /api/races/start fails with 422 when no event is configured."""
    from unittest.mock import patch

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with patch("helmlog.races.default_event_for_date", return_value=None):
            resp = await client.post("/api/races/start")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_event_and_start_race(storage: Storage) -> None:
    """POST /api/event + POST /api/races/start creates a race."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "TestRegatta" in data["name"]
    assert data["race_num"] == 1


# ---------------------------------------------------------------------------
# Tests — audio integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_race_triggers_audio_start(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/start calls recorder.start() with the race name."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201
    recorder.start.assert_awaited_once()
    _args, kwargs = recorder.start.await_args
    assert "name" in kwargs
    assert "TestRegatta" in kwargs["name"]


@pytest.mark.asyncio
async def test_end_race_triggers_audio_stop(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/end calls recorder.stop() and updates the DB."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        end_resp = await client.post(f"/api/races/{race_id}/end")

    assert end_resp.status_code == 204
    recorder.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_race_audio_device_not_found(storage: Storage, tmp_path: Path) -> None:
    """AudioDeviceNotFoundError is caught; race is still created successfully."""
    recorder = _make_recorder(raises_on_start=True)
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201
    assert "name" in resp.json()


@pytest.mark.asyncio
async def test_start_race_without_recorder(storage: Storage) -> None:
    """create_app without a recorder still creates races normally."""
    app = create_app(storage)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_instruments_returns_nulls_empty_db(storage: Storage) -> None:
    """GET /api/instruments returns all None values when no data is in the DB."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/instruments")

    assert resp.status_code == 200
    data = resp.json()
    assert data["heading_deg"] is None
    assert data["bsp_kts"] is None
    assert data["cog_deg"] is None
    assert data["sog_kts"] is None
    assert data["tws_kts"] is None
    assert data["twa_deg"] is None
    assert data["twd_deg"] is None
    assert data["aws_kts"] is None
    assert data["awa_deg"] is None


@pytest.mark.asyncio
async def test_instruments_returns_latest_values(storage: Storage) -> None:
    """GET /api/instruments returns correctly rounded values from each table."""
    ts = datetime(2026, 2, 26, 15, 0, 0, tzinfo=UTC)
    await storage.write(
        HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=ts,
            heading_deg=270.0,
            deviation_deg=None,
            variation_deg=None,
        )
    )
    await storage.write(
        SpeedRecord(pgn=PGN_SPEED_THROUGH_WATER, source_addr=5, timestamp=ts, speed_kts=6.5)
    )
    await storage.write(
        COGSOGRecord(pgn=PGN_COG_SOG_RAPID, source_addr=5, timestamp=ts, cog_deg=265.0, sog_kts=5.8)
    )
    await storage.write(
        WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=ts,
            wind_speed_kts=12.0,
            wind_angle_deg=45.0,
            reference=0,
        )
    )
    await storage.write(
        WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=ts,
            wind_speed_kts=14.5,
            wind_angle_deg=35.0,
            reference=2,
        )
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/instruments")

    assert resp.status_code == 200
    data = resp.json()
    assert data["heading_deg"] == 270.0
    assert data["bsp_kts"] == 6.5
    assert data["cog_deg"] == 265.0
    assert data["sog_kts"] == 5.8
    assert data["tws_kts"] == 12.0
    assert data["twa_deg"] == 45.0
    assert data["twd_deg"] == (270.0 + 45.0) % 360  # 315.0
    assert data["aws_kts"] == 14.5
    assert data["awa_deg"] == 35.0


@pytest.mark.asyncio
async def test_start_practice_creates_practice_session(storage: Storage) -> None:
    """POST /api/races/start?session_type=practice creates a practice session with P prefix."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start?session_type=practice")

    assert resp.status_code == 201
    data = resp.json()
    assert "P1" in data["name"]
    assert data["session_type"] == "practice"


@pytest.mark.asyncio
async def test_state_includes_next_practice_num(storage: Storage) -> None:
    """GET /api/state includes next_practice_num."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/state")

    assert resp.status_code == 200
    data = resp.json()
    assert "next_practice_num" in data
    assert data["next_practice_num"] == 1


@pytest.mark.asyncio
async def test_invalid_session_type_returns_422(storage: Storage) -> None:
    """POST /api/races/start with invalid session_type returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/start?session_type=invalid")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_index_substitutes_grafana_port(storage: Storage) -> None:
    """GET / returns HTML with port/UID placeholders replaced by configured values."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    html = resp.text
    assert "__GRAFANA_PORT__" not in html
    assert "__GRAFANA_UID__" not in html
    assert "__SK_PORT__" not in html
    # Default ports and UID are injected as data- attributes
    assert 'data-grafana-port="3001"' in html
    assert "helmlog-sailing" in html
    assert 'data-sk-port="3000"' in html


@pytest.mark.asyncio
async def test_index_uses_env_grafana_port(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET / uses GRAFANA_PORT / GRAFANA_DASHBOARD_UID env vars when set."""
    monkeypatch.setenv("GRAFANA_PORT", "4001")
    monkeypatch.setenv("GRAFANA_DASHBOARD_UID", "custom-uid")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")

    html = resp.text
    assert 'data-grafana-port="4001"' in html
    assert "custom-uid" in html
    assert "__GRAFANA_PORT__" not in html


@pytest.mark.asyncio
async def test_index_has_dynamic_signalk_link(storage: Storage) -> None:
    """Index page always includes the Signal K nav link (JS shows/hides it)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")

    html = resp.text
    assert 'id="signalk-nav"' in html
    assert "Signal K" in html


@pytest.mark.asyncio
async def test_nav_has_hamburger_menu(storage: Storage) -> None:
    """Base layout includes a hamburger toggle button for mobile nav."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")

    html = resp.text
    # Hamburger button must be present with accessibility attributes
    assert 'id="nav-hamburger"' in html
    assert "aria-label=" in html
    assert "aria-expanded=" in html
    # Nav links must still all be present
    assert 'href="/history"' in html
    assert 'href="/admin/boats"' in html


# ---------------------------------------------------------------------------
# Home routing: / redirects between control, live session, and latest completed
# session depending on race state. See #635.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_renders_control_panel(storage: Storage) -> None:
    """GET /control always renders the control-panel home template (#635)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/control")
    assert resp.status_code == 200
    assert 'id="setup-card"' in resp.text
    assert "home.js" in resp.text


@pytest.mark.asyncio
async def test_root_empty_db_falls_back_to_control_panel(storage: Storage) -> None:
    """GET / with no races still serves the control panel (empty-state fallback, #635)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="setup-card"' in resp.text
    assert "home.js" in resp.text


@pytest.mark.asyncio
async def test_root_with_completed_race_renders_session(storage: Storage) -> None:
    """GET / with a completed race renders that race's session view (#635)."""
    start = datetime(2026, 4, 20, 19, 0, tzinfo=UTC)
    end = datetime(2026, 4, 20, 19, 45, tzinfo=UTC)
    race = await storage.start_race("Spring", start, "2026-04-20", 1, "spring-1")
    await storage.end_race(race.id, end)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    # Session template markers
    assert "session.js" in resp.text
    assert f'data-session-id="{race.id}"' in resp.text
    # Not live — completed
    assert 'data-live="1"' not in resp.text


@pytest.mark.asyncio
async def test_root_with_in_progress_race_renders_live_session(storage: Storage) -> None:
    """GET / with an open race renders the live session view (#635)."""
    start = datetime(2026, 4, 21, 19, 0, tzinfo=UTC)
    race = await storage.start_race("Spring", start, "2026-04-21", 2, "spring-2")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "session.js" in resp.text
    assert f'data-session-id="{race.id}"' in resp.text
    assert 'data-live="1"' in resp.text


@pytest.mark.asyncio
async def test_root_in_progress_takes_precedence_over_completed(storage: Storage) -> None:
    """An in-progress race beats the most-recent-completed for / (#635)."""
    done_start = datetime(2026, 4, 20, 19, 0, tzinfo=UTC)
    done_end = datetime(2026, 4, 20, 19, 45, tzinfo=UTC)
    done = await storage.start_race("Spring", done_start, "2026-04-20", 1, "spring-1")
    await storage.end_race(done.id, done_end)

    open_start = datetime(2026, 4, 21, 19, 0, tzinfo=UTC)
    open_race = await storage.start_race("Spring", open_start, "2026-04-21", 2, "spring-2")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert f'data-session-id="{open_race.id}"' in resp.text
    assert 'data-live="1"' in resp.text


@pytest.mark.asyncio
async def test_grafana_annotations_cors_header(storage: Storage) -> None:
    """GET /api/grafana/annotations returns Access-Control-Allow-Origin: *."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/grafana/annotations", params={"from": 0, "to": 1000})

    assert resp.headers.get("access-control-allow-origin") == "*"


# ---------------------------------------------------------------------------
# Tests — debrief mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debrief_start_creates_audio(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/debrief/start calls recorder.start() with name containing '-debrief'."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        await client.post(f"/api/races/{race_id}/end")
        debrief_resp = await client.post(f"/api/races/{race_id}/debrief/start")

    assert debrief_resp.status_code == 201
    # recorder.start called twice: once for race, once for debrief
    assert recorder.start.await_count == 2
    _args, kwargs = recorder.start.await_args
    assert "name" in kwargs
    assert kwargs["name"].endswith("-debrief")


@pytest.mark.asyncio
async def test_debrief_stop_ends_audio(storage: Storage, tmp_path: Path) -> None:
    """POST /api/debrief/stop calls recorder.stop() and returns 204."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        await client.post(f"/api/races/{race_id}/end")
        await client.post(f"/api/races/{race_id}/debrief/start")
        stop_resp = await client.post("/api/debrief/stop")

    assert stop_resp.status_code == 204
    # stop called twice: once for race end, once for debrief stop
    assert recorder.stop.await_count == 2


@pytest.mark.asyncio
async def test_debrief_on_open_race_auto_ends_it(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/debrief/start on an in-progress race auto-ends the race first."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        start_resp = await client.post("/api/races/start")
        race_id = start_resp.json()["id"]
        # Race is still in progress — debrief auto-ends it
        debrief_resp = await client.post(f"/api/races/{race_id}/debrief/start")

    assert debrief_resp.status_code == 201


@pytest.mark.asyncio
async def test_debrief_no_recorder_returns_409(storage: Storage) -> None:
    """POST /api/races/{id}/debrief/start with no recorder configured returns 409."""
    app = create_app(storage)  # no recorder

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Use a dummy race_id — the recorder check happens first
        debrief_resp = await client.post("/api/races/1/debrief/start")

    assert debrief_resp.status_code == 409


@pytest.mark.asyncio
async def test_state_includes_debrief_fields(storage: Storage, tmp_path: Path) -> None:
    """GET /api/state returns has_recorder and current_debrief fields."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app_with = create_app(storage, recorder=recorder, audio_config=config)
    app_without = create_app(storage)

    # Without recorder: has_recorder false, current_debrief null
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_without), base_url="http://test"
    ) as client:
        data = (await client.get("/api/state")).json()
    assert data["has_recorder"] is False
    assert data["current_debrief"] is None

    # With recorder, before debrief: has_recorder true, current_debrief null
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with), base_url="http://test"
    ) as client:
        data = (await client.get("/api/state")).json()
        assert data["has_recorder"] is True
        assert data["current_debrief"] is None

        # Start a race, end it, then start debrief
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/races/{race_id}/end")
        await client.post(f"/api/races/{race_id}/debrief/start")

        data = (await client.get("/api/state")).json()
    assert data["has_recorder"] is True
    assert data["current_debrief"] is not None
    assert data["current_debrief"]["race_id"] == race_id
    assert "race_name" in data["current_debrief"]
    assert "start_utc" in data["current_debrief"]


@pytest.mark.asyncio
async def test_end_race_no_active_recording_is_noop(storage: Storage, tmp_path: Path) -> None:
    """POST /api/races/{id}/end does not call recorder.stop() if no recording started."""
    # Start a race without a recorder (so _audio_session_id stays None), then
    # end it with a recorder attached — stop() should NOT be called.
    recorder = _make_recorder()
    no_recorder_app = create_app(storage)
    recorder_app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )

    # Use the no-recorder app to start a race
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=no_recorder_app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]

    # Use the recorder app to end it — _audio_session_id is None so stop() is skipped
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recorder_app), base_url="http://test"
    ) as client:
        end_resp = await client.post(f"/api/races/{race_id}/end")

    assert end_resp.status_code == 204
    recorder.stop.assert_not_called()


# ---------------------------------------------------------------------------
# /history page and /api/sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_page_served(storage: Storage) -> None:
    """GET /history returns the history HTML page."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/history")
    assert resp.status_code == 200
    assert "Session History" in resp.text
    assert "history.js" in resp.text


@pytest.mark.asyncio
async def test_main_page_has_history_link(storage: Storage) -> None:
    """GET / contains a link to /history."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "/history" in resp.text


@pytest.mark.asyncio
async def test_api_sessions_empty(storage: Storage) -> None:
    """GET /api/sessions returns empty list when no sessions exist."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["sessions"] == []


@pytest.mark.asyncio
async def test_api_sessions_returns_races_and_practices(storage: Storage) -> None:
    """GET /api/sessions returns all races and practices sorted newest-first."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        # Create a race and a practice
        r1 = (await client.post("/api/races/start?session_type=race")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        r2 = (await client.post("/api/races/start?session_type=practice")).json()
        await client.post(f"/api/races/{r2['id']}/end")

        resp = await client.get("/api/sessions")
    data = resp.json()
    assert data["total"] == 2
    types = [s["type"] for s in data["sessions"]]
    assert "race" in types
    assert "practice" in types


@pytest.mark.asyncio
async def test_api_sessions_type_filter(storage: Storage) -> None:
    """GET /api/sessions?type=race only returns races."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r1 = (await client.post("/api/races/start?session_type=race")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        r2 = (await client.post("/api/races/start?session_type=practice")).json()
        await client.post(f"/api/races/{r2['id']}/end")

        resp = await client.get("/api/sessions?type=race")
    data = resp.json()
    assert data["total"] == 1
    assert data["sessions"][0]["type"] == "race"


@pytest.mark.asyncio
async def test_api_sessions_invalid_type(storage: Storage) -> None:
    """GET /api/sessions?type=bogus returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions?type=bogus")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_sessions_search(storage: Storage) -> None:
    """GET /api/sessions?q=BallardCup only returns sessions matching the query."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client, "BallardCup")
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")

        # Set a different event and create another race
        await _set_event(client, "CYC")
        r2 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r2['id']}/end")

        resp_all = await client.get("/api/sessions")
        resp_filtered = await client.get("/api/sessions?q=BallardCup")

    assert resp_all.json()["total"] == 2
    data = resp_filtered.json()
    assert data["total"] == 1
    assert "BallardCup" in data["sessions"][0]["event"]


@pytest.mark.asyncio
async def test_api_sessions_date_filter(storage: Storage) -> None:
    """GET /api/sessions?from_date=...&to_date=... filters by date."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")

        today = datetime.now(UTC).date().isoformat()
        resp_in = await client.get(f"/api/sessions?from_date={today}&to_date={today}")
        resp_out = await client.get("/api/sessions?from_date=2000-01-01&to_date=2000-01-02")

    assert resp_in.json()["total"] == 1
    assert resp_out.json()["total"] == 0


@pytest.mark.asyncio
async def test_api_sessions_pagination(storage: Storage) -> None:
    """GET /api/sessions limit/offset pagination works correctly."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        # Create 3 races
        for _ in range(3):
            r = (await client.post("/api/races/start")).json()
            await client.post(f"/api/races/{r['id']}/end")

        resp_all = await client.get("/api/sessions?limit=10")
        resp_page1 = await client.get("/api/sessions?limit=2&offset=0")
        resp_page2 = await client.get("/api/sessions?limit=2&offset=2")

    assert resp_all.json()["total"] == 3
    assert len(resp_page1.json()["sessions"]) == 2
    assert len(resp_page2.json()["sessions"]) == 1


@pytest.mark.asyncio
async def test_api_sessions_has_audio_flag(storage: Storage, tmp_path: Path) -> None:
    """has_audio is True for a race that has an associated audio session."""
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")

        resp = await client.get("/api/sessions")
    data = resp.json()
    assert data["total"] == 1
    s = data["sessions"][0]
    assert s["has_audio"] is True
    assert s["audio_session_id"] is not None


@pytest.mark.asyncio
async def test_api_session_detail_includes_audio_start_utc(
    storage: Storage, tmp_path: Path
) -> None:
    """Session detail exposes audio_start_utc so the JS playback clock can map
    transcript/audio offsets onto the session UTC timeline (#446)."""
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")

        resp = await client.get(f"/api/sessions/{r['id']}/detail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_audio"] is True
    assert data["audio_session_id"] is not None
    assert data["audio_start_utc"] is not None
    # Should be a parseable ISO-8601 timestamp
    from datetime import datetime as _dt

    _dt.fromisoformat(data["audio_start_utc"])


@pytest.mark.asyncio
async def test_api_session_detail_includes_debrief_audio(storage: Storage, tmp_path: Path) -> None:
    """Session detail exposes a separate debrief_audio block when a race has an
    attached debrief recording, so the session page can surface both WAVs (#546)."""
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")
        await client.post(f"/api/races/{r['id']}/debrief/start")
        await client.post("/api/debrief/stop")

        resp = await client.get(f"/api/sessions/{r['id']}/detail")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_audio"] is True
    assert data["audio_session_id"] is not None
    debrief = data.get("debrief_audio")
    assert debrief is not None, "debrief_audio should be populated when a debrief exists"
    assert debrief["audio_session_id"] != data["audio_session_id"]
    assert debrief["stream_url"] == f"/api/audio/{debrief['audio_session_id']}/stream"
    assert debrief["start_utc"] is not None


@pytest.mark.asyncio
async def test_api_session_detail_debrief_audio_absent_without_debrief(
    storage: Storage, tmp_path: Path
) -> None:
    """Session detail returns debrief_audio=None when no debrief exists (#546)."""
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")

        resp = await client.get(f"/api/sessions/{r['id']}/detail")

    assert resp.status_code == 200
    assert resp.json().get("debrief_audio") is None


@pytest.mark.asyncio
async def test_api_sessions_hides_attached_debriefs_by_default(
    storage: Storage, tmp_path: Path
) -> None:
    """Debriefs attached to a race should not appear in the default history
    list — they're now reachable from the race session page (#546). Explicit
    type=debrief filter should still return them."""
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")
        await client.post(f"/api/races/{r['id']}/debrief/start")
        await client.post("/api/debrief/stop")

        default_resp = await client.get("/api/sessions")
        filtered_resp = await client.get("/api/sessions?type=debrief")

    default_rows = default_resp.json()["sessions"]
    assert len(default_rows) == 1
    assert default_rows[0]["type"] == "race"

    filtered_rows = filtered_resp.json()["sessions"]
    assert any(s["type"] == "debrief" for s in filtered_rows)


@pytest.mark.asyncio
async def _get_pos_ids(client: httpx.AsyncClient) -> dict[str, int]:
    """Helper: return position name → id mapping."""
    resp = await client.get("/api/crew/positions")
    return {p["name"]: p["id"] for p in resp.json()["positions"]}


async def _make_crew_user(client: httpx.AsyncClient, name: str) -> int:
    """Helper: create a placeholder user and return the id."""
    resp = await client.post("/api/crew/placeholder", json={"name": name})
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_api_sessions_includes_crew(storage: Storage) -> None:
    """GET /api/sessions returns crew list per race/practice session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        race_id = r["id"]
        pos_ids = await _get_pos_ids(client)
        mark_id = await _make_crew_user(client, "Mark")
        dave_id = await _make_crew_user(client, "Dave")
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position_id": pos_ids["helm"], "user_id": mark_id},
                {"position_id": pos_ids["main"], "user_id": dave_id},
            ],
        )
        await client.post(f"/api/races/{race_id}/end")

        resp = await client.get("/api/sessions")
    data = resp.json()
    assert data["total"] == 1
    session = data["sessions"][0]
    assert "crew" in session


@pytest.mark.asyncio
async def test_api_sessions_includes_debriefs(storage: Storage, tmp_path: Path) -> None:
    """Completed debriefs are reachable via the explicit type=debrief filter.

    As of #546 they're hidden from the default history list when attached to
    a race (reachable from the race session page instead), but the explicit
    filter still surfaces them with their parent race metadata.
    """
    recorder = _make_recorder()
    app = create_app(
        storage,
        recorder=recorder,
        audio_config=AudioConfig(
            device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r['id']}/end")
        await client.post(f"/api/races/{r['id']}/debrief/start")
        await client.post("/api/debrief/stop")

        resp_debrief = await client.get("/api/sessions?type=debrief")

    deb_data = resp_debrief.json()
    assert deb_data["total"] == 1
    deb = deb_data["sessions"][0]
    assert deb["type"] == "debrief"
    assert deb["parent_race_id"] == r["id"]
    assert deb["has_audio"] is True


# ---------------------------------------------------------------------------
# Crew API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_crew_sets_crew(storage: Storage) -> None:
    """POST /api/races/{id}/crew then GET returns the same crew in canonical order."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        pos_ids = await _get_pos_ids(client)
        mark_id = await _make_crew_user(client, "Mark")
        dave_id = await _make_crew_user(client, "Dave")
        bill_id = await _make_crew_user(client, "Bill")

        post_resp = await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position_id": pos_ids["helm"], "user_id": mark_id},
                {"position_id": pos_ids["main"], "user_id": dave_id},
                {"position_id": pos_ids["tactician"], "user_id": bill_id},
            ],
        )
        assert post_resp.status_code == 204

        get_resp = await client.get(f"/api/races/{race_id}/crew")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert "crew" in data
    positions = [c["position"] for c in data["crew"]]
    assert positions == ["helm", "main", "tactician"]
    names = {c["position"]: c["user_name"] for c in data["crew"]}
    assert names["helm"] == "Mark"
    assert names["main"] == "Dave"
    assert names["tactician"] == "Bill"


@pytest.mark.asyncio
async def test_post_crew_invalid_position(storage: Storage) -> None:
    """POST /api/races/{id}/crew with an unknown position_id returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/races/{race_id}/crew",
            json=[{"position_id": 99999, "user_id": 1}],
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_crew_duplicate_user_rejected(storage: Storage) -> None:
    """POST /api/races/{id}/crew with same user in two positions returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        pos_ids = await _get_pos_ids(client)
        alice_id = await _make_crew_user(client, "Alice")
        resp = await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position_id": pos_ids["helm"], "user_id": alice_id},
                {"position_id": pos_ids["main"], "user_id": alice_id},
            ],
        )
    assert resp.status_code == 422
    assert "Duplicate" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_crew_unknown_race(storage: Storage) -> None:
    """GET /api/races/{id}/crew for a non-existent race returns 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/races/99999/crew")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_state_includes_crew(storage: Storage) -> None:
    """/api/state today_races include crew list per race."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        pos_ids = await _get_pos_ids(client)
        helm_uid = await _make_crew_user(client, "TestHelm")
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[{"position_id": pos_ids["helm"], "user_id": helm_uid}],
        )

        state = (await client.get("/api/state")).json()

    assert state["current_race"] is not None
    assert "crew" in state["current_race"]
    names = {c["position"]: c["user_name"] for c in state["current_race"]["crew"]}
    assert names.get("helm") == "TestHelm"

    # today_races also includes crew
    assert len(state["today_races"]) == 1
    assert "crew" in state["today_races"][0]


@pytest.mark.asyncio
async def test_crew_users_endpoint(storage: Storage) -> None:
    """GET /api/crew/users returns created users."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _make_crew_user(client, "Alice")
        await _make_crew_user(client, "Bob")

        resp = await client.get("/api/crew/users")
    assert resp.status_code == 200
    data = resp.json()
    assert "users" in data
    names = [u["name"] for u in data["users"]]
    assert "Alice" in names
    assert "Bob" in names
    # weight_lbs should be present in user records for crew weight defaulting
    assert all("weight_lbs" in u for u in data["users"])


@pytest.mark.asyncio
async def test_crew_users_pending_flag(storage: Storage) -> None:
    """GET /api/crew/users marks invited-but-not-accepted users as pending."""
    from helmlog.auth import generate_token, invite_expires_at

    # Create a regular active user
    active_id = await storage.create_user("active@x.com", "Active", "crew")
    # Create an invited (inactive) user with a pending invitation
    invited_id = await storage.create_user("invited@x.com", "Invited", "crew", is_active=False)
    await storage.create_invitation(
        generate_token(),
        "invited@x.com",
        "crew",
        "Invited",
        False,
        active_id,
        invite_expires_at(),
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/crew/users")
    assert resp.status_code == 200
    users = resp.json()["users"]
    active_user = next(u for u in users if u["id"] == active_id)
    invited_user = next(u for u in users if u["id"] == invited_id)
    assert active_user["pending"] is False
    assert invited_user["pending"] is True


@pytest.mark.asyncio
async def test_post_crew_non_attributed(storage: Storage) -> None:
    """POST /api/races/{id}/crew with attributed=false stores non-attributed entries."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        pos_ids = await _get_pos_ids(client)
        mark_id = await _make_crew_user(client, "Mark")
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position_id": pos_ids["helm"], "user_id": mark_id},
                {"position_id": pos_ids["main"], "attributed": False},
            ],
        )

        resp = await client.get(f"/api/races/{race_id}/crew")
    crew = resp.json()["crew"]
    by_pos = {c["position"]: c for c in crew}
    assert by_pos["helm"]["user_name"] == "Mark"
    assert by_pos["main"]["attributed"] == 0


# ---------------------------------------------------------------------------
# Issue #30: debrief auto-stop + crew carry-forward
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_race_while_debrief_active_stops_debrief(
    storage: Storage, tmp_path: Path
) -> None:
    """Starting a race while a debrief is active auto-stops the debrief."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        # Start + end race 1, then start a debrief
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        await client.post(f"/api/races/{r1['id']}/debrief/start")

        # Verify debrief is active
        state = (await client.get("/api/state")).json()
        assert state["current_debrief"] is not None

        # Start race 2 without explicitly stopping the debrief
        r2 = await client.post("/api/races/start")
        assert r2.status_code == 201

        # Debrief should have been auto-stopped
        state = (await client.get("/api/state")).json()
        assert state["current_debrief"] is None

    # recorder.stop() called: once for race 1 end, once for debrief auto-stop
    assert recorder.stop.await_count == 2


@pytest.mark.asyncio
async def test_debrief_end_utc_written_when_race_starts(storage: Storage, tmp_path: Path) -> None:
    """When a race starts auto-stopping a debrief, end_utc is persisted to the DB."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r1 = (await client.post("/api/races/start")).json()
        await client.post(f"/api/races/{r1['id']}/end")
        await client.post(f"/api/races/{r1['id']}/debrief/start")

        # Start race 2 — should auto-stop the debrief
        await client.post("/api/races/start")

    # Check that the debrief audio_session row has end_utc set
    db = storage._conn()
    cur = await db.execute(
        "SELECT end_utc FROM audio_sessions WHERE session_type = 'debrief' ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["end_utc"] is not None


@pytest.mark.asyncio
async def test_debrief_auto_ends_open_race(storage: Storage, tmp_path: Path) -> None:
    """Starting a debrief on an in-progress race auto-ends the race first (defensive AC #2)."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r = (await client.post("/api/races/start")).json()
        race_id = r["id"]

        # Start debrief without ending the race first
        debrief_resp = await client.post(f"/api/races/{race_id}/debrief/start")
        assert debrief_resp.status_code == 201

    # Race recording should have been stopped (once for the auto-end)
    # Debrief recording starts after, so recorder.start called twice total
    assert recorder.start.await_count == 2

    # The race row should now have end_utc set
    db = storage._conn()
    cur = await db.execute("SELECT end_utc FROM races WHERE id = ?", (race_id,))
    row = await cur.fetchone()
    assert row is not None
    assert row["end_utc"] is not None


@pytest.mark.asyncio
async def test_start_race_uses_boat_level_defaults(storage: Storage) -> None:
    """Starting a new race auto-applies boat-level crew defaults via resolve_crew."""
    app = create_app(storage)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        pos_ids = await _get_pos_ids(client)
        alice_id = await _make_crew_user(client, "Alice")
        bob_id = await _make_crew_user(client, "Bob")

        # Set boat-level defaults
        await client.post(
            "/api/crew/defaults",
            json=[
                {"position_id": pos_ids["helm"], "user_id": alice_id},
                {"position_id": pos_ids["main"], "user_id": bob_id},
            ],
        )

        # Start race without posting any crew
        r = (await client.post("/api/races/start")).json()

        # Crew should come from boat-level defaults
        crew_resp = await client.get(f"/api/races/{r['id']}/crew")

    assert crew_resp.status_code == 200
    crew = crew_resp.json()["crew"]
    by_pos = {c["position"]: c["user_name"] for c in crew}
    assert by_pos.get("helm") == "Alice"
    assert by_pos.get("main") == "Bob"


# ---------------------------------------------------------------------------
# Video API tests
# ---------------------------------------------------------------------------

_SYNC_UTC = "2026-02-26T14:05:00+00:00"
_YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


async def _start_race_for_videos(client: httpx.AsyncClient) -> int:
    await _set_event(client)
    return (await client.post("/api/races/start")).json()["id"]


@pytest.mark.asyncio
async def test_list_videos_empty(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos returns [] when no videos are linked."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        resp = await client.get(f"/api/sessions/{race_id}/videos")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_videos_unknown_session(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos returns 404 for an unknown session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/99999/videos")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_add_video_returns_201_and_lists(storage: Storage) -> None:
    """POST /api/sessions/{id}/videos creates a video and GET returns it."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/videos",
            json={
                "youtube_url": _YT_URL,
                "label": "Bow cam",
                "sync_utc": _SYNC_UTC,
                "sync_offset_s": 323.0,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["youtube_url"] == _YT_URL
        assert data["label"] == "Bow cam"
        assert data["sync_offset_s"] == 323.0
        assert "id" in data

        list_resp = await client.get(f"/api/sessions/{race_id}/videos")
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) == 1
    assert items[0]["id"] == data["id"]


@pytest.mark.asyncio
async def test_add_video_unknown_session_returns_404(storage: Storage) -> None:
    """POST /api/sessions/{id}/videos returns 404 for an unknown session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/99999/videos",
            json={"youtube_url": _YT_URL, "sync_utc": _SYNC_UTC, "sync_offset_s": 0.0},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_add_video_invalid_sync_utc_returns_422(storage: Storage) -> None:
    """POST /api/sessions/{id}/videos returns 422 when sync_utc is not parseable."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/videos",
            json={"youtube_url": _YT_URL, "sync_utc": "not-a-date", "sync_offset_s": 0.0},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_videos_at_param_returns_deep_link(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos?at= returns a computed deep_link for each video."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        # Add video manually via storage so we control duration_s
        sync_utc = datetime(2026, 2, 26, 14, 5, 0, tzinfo=UTC)
        await storage.add_race_video(
            race_id=race_id,
            youtube_url=_YT_URL,
            video_id="dQw4w9WgXcQ",
            title="Test",
            label="",
            sync_utc=sync_utc,
            sync_offset_s=323.0,
            duration_s=600.0,
        )
        # Request 30 seconds after sync — expected video pos = 323 + 30 = 353
        at = "2026-02-26T14:05:30Z"
        resp = await client.get(f"/api/sessions/{race_id}/videos?at={at}")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["deep_link"] == "https://youtu.be/dQw4w9WgXcQ?t=353"


@pytest.mark.asyncio
async def test_list_videos_at_invalid_returns_422(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos?at=bad returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        resp = await client.get(f"/api/sessions/{race_id}/videos?at=not-a-date")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_video_label(storage: Storage) -> None:
    """PATCH /api/videos/{id} updates the label and returns 200."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        add_resp = await client.post(
            f"/api/sessions/{race_id}/videos",
            json={
                "youtube_url": _YT_URL,
                "label": "Old",
                "sync_utc": _SYNC_UTC,
                "sync_offset_s": 0.0,
            },
        )
        vid_id = add_resp.json()["id"]
        patch_resp = await client.patch(f"/api/videos/{vid_id}", json={"label": "New"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["updated"] is True


@pytest.mark.asyncio
async def test_update_video_not_found(storage: Storage) -> None:
    """PATCH /api/videos/{id} returns 404 for an unknown video."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch("/api/videos/99999", json={"label": "Ghost"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_video(storage: Storage) -> None:
    """DELETE /api/videos/{id} removes the video and returns 204."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        add_resp = await client.post(
            f"/api/sessions/{race_id}/videos",
            json={"youtube_url": _YT_URL, "sync_utc": _SYNC_UTC, "sync_offset_s": 0.0},
        )
        vid_id = add_resp.json()["id"]
        del_resp = await client.delete(f"/api/videos/{vid_id}")
        assert del_resp.status_code == 204
        list_resp = await client.get(f"/api/sessions/{race_id}/videos")
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_delete_video_not_found(storage: Storage) -> None:
    """DELETE /api/videos/{id} returns 404 for an unknown video."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/videos/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/sessions/{id}/videos/redirect tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_videos_redirect_302(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos/redirect returns 302 to computed YouTube URL."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as client:
        race_id = await _start_race_for_videos(client)
        sync_utc = datetime(2026, 2, 26, 14, 5, 0, tzinfo=UTC)
        await storage.add_race_video(
            race_id=race_id,
            youtube_url=_YT_URL,
            video_id="dQw4w9WgXcQ",
            title="Test",
            label="",
            sync_utc=sync_utc,
            sync_offset_s=323.0,
            duration_s=600.0,
        )
        # 30 s after sync → video pos = 323 + 30 = 353
        at = "2026-02-26T14:05:30Z"
        resp = await client.get(f"/api/sessions/{race_id}/videos/redirect?at={at}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://youtu.be/dQw4w9WgXcQ?t=353"


@pytest.mark.asyncio
async def test_videos_redirect_no_videos_returns_404(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos/redirect returns 404 when no videos are linked."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        resp = await client.get(f"/api/sessions/{race_id}/videos/redirect?at=2026-02-26T14:05:30Z")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_videos_redirect_unknown_session_returns_404(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos/redirect returns 404 for an unknown session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/99999/videos/redirect?at=2026-02-26T14:05:30Z")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_videos_redirect_invalid_at_returns_422(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos/redirect returns 422 when 'at' is not parseable."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        resp = await client.get(f"/api/sessions/{race_id}/videos/redirect?at=not-a-date")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_videos_redirect_missing_at_returns_422(storage: Storage) -> None:
    """GET /api/sessions/{id}/videos/redirect returns 422 when 'at' is absent."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _start_race_for_videos(client)
        resp = await client.get(f"/api/sessions/{race_id}/videos/redirect")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Issue #38 — guest crew position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guest_crew_accepted_and_returned(storage: Storage) -> None:
    """POST /api/races/{id}/crew accepts 'guest' position and GET returns it."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        pos_ids = await _get_pos_ids(client)
        alice_id = await _make_crew_user(client, "Alice")
        charlie_id = await _make_crew_user(client, "Charlie")
        resp = await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position_id": pos_ids["helm"], "user_id": alice_id},
                {"position_id": pos_ids["guest"], "user_id": charlie_id},
            ],
        )
        assert resp.status_code == 204

        crew_resp = await client.get(f"/api/races/{race_id}/crew")
    assert crew_resp.status_code == 200
    positions = {c["position"]: c["user_name"] for c in crew_resp.json()["crew"]}
    assert positions.get("helm") == "Alice"
    assert positions.get("guest") == "Charlie"


@pytest.mark.asyncio
async def test_guest_crew_in_state(storage: Storage) -> None:
    """GET /api/state includes guest crew member in current_race.crew."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        pos_ids = await _get_pos_ids(client)
        guest_id = await _make_crew_user(client, "GuestSailor")
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[{"position_id": pos_ids["guest"], "user_id": guest_id}],
        )
        state = (await client.get("/api/state")).json()

    crew = {c["position"]: c["user_name"] for c in state["current_race"]["crew"]}
    assert crew.get("guest") == "GuestSailor"


# ---------------------------------------------------------------------------
# Issue #49 — WAV download on home page race cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_race_has_audio_fields_when_audio_linked(
    storage: Storage, tmp_path: Path
) -> None:
    """GET /api/state today_races include has_audio=True and audio_session_id."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        await client.post("/api/races/start")
        await client.post("/api/races/end")
        state = (await client.get("/api/state")).json()

    assert len(state["today_races"]) == 1
    race = state["today_races"][0]
    assert race["has_audio"] is True
    assert race["audio_session_id"] is not None


@pytest.mark.asyncio
async def test_state_race_has_audio_false_when_no_audio(storage: Storage) -> None:
    """GET /api/state today_races entries have has_audio=False when no recording."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        await client.post("/api/races/start")
        await client.post("/api/races/end")
        state = (await client.get("/api/state")).json()

    assert len(state["today_races"]) == 1
    race = state["today_races"][0]
    assert race["has_audio"] is False
    assert race["audio_session_id"] is None


# ---------------------------------------------------------------------------
# Issue #31 — debrief crew from parent race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debrief_session_includes_parent_race_crew(storage: Storage, tmp_path: Path) -> None:
    """GET /api/sessions returns debrief sessions with crew from the parent race."""
    recorder = _make_recorder()
    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    app = create_app(storage, recorder=recorder, audio_config=config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        pos_ids = await _get_pos_ids(client)
        helm_id = await _make_crew_user(client, "DebriefHelm")
        main_id = await _make_crew_user(client, "DebriefMain")
        await client.post(
            f"/api/races/{race_id}/crew",
            json=[
                {"position_id": pos_ids["helm"], "user_id": helm_id},
                {"position_id": pos_ids["main"], "user_id": main_id},
            ],
        )
        await client.post("/api/races/end")
        # Start and stop a debrief
        await client.post(f"/api/races/{race_id}/debrief/start")
        await client.post("/api/debrief/stop")

        # Attached debriefs are hidden from the default history view as of
        # #546; fetch them via the explicit type filter.
        resp = await client.get("/api/sessions?type=debrief")

    assert resp.status_code == 200
    data = resp.json()
    debriefs = [s for s in data["sessions"] if s["type"] == "debrief"]
    assert len(debriefs) == 1
    crew = {c["position"]: c["user_name"] for c in debriefs[0]["crew"]}
    assert crew.get("helm") == "DebriefHelm"
    assert crew.get("main") == "DebriefMain"


# ---------------------------------------------------------------------------
# Issue #57 — Sail inventory and per-race sail selection
# ---------------------------------------------------------------------------


async def _add_sail(client: httpx.AsyncClient, sail_type: str, name: str) -> int:
    resp = await client.post("/api/sails", json={"type": sail_type, "name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_list_sails_empty(storage: Storage) -> None:
    """GET /api/sails returns empty lists per type when no sails exist."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sails")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"main": [], "jib": [], "spinnaker": []}


@pytest.mark.asyncio
async def test_add_sail_201(storage: Storage) -> None:
    """POST /api/sails creates a sail and returns 201."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sails", json={"type": "main", "name": "Full Main"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "main"
    assert data["name"] == "Full Main"
    assert "id" in data


@pytest.mark.asyncio
async def test_add_sail_duplicate_409(storage: Storage) -> None:
    """POST /api/sails with duplicate (type, name) returns 409."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/sails", json={"type": "jib", "name": "Code 3"})
        resp = await client.post("/api/sails", json={"type": "jib", "name": "Code 3"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_add_sail_invalid_type_422(storage: Storage) -> None:
    """POST /api/sails with unknown type returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sails", json={"type": "foresail", "name": "Genoa"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_sail_retire(storage: Storage) -> None:
    """PATCH /api/sails/{id} active=false retires the sail; GET /api/sails no longer shows it."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sail_id = await _add_sail(client, "spinnaker", "A2")
        patch_resp = await client.patch(f"/api/sails/{sail_id}", json={"active": False})
        assert patch_resp.status_code == 200
        list_resp = await client.get("/api/sails")
    data = list_resp.json()
    ids = [s["id"] for s in data["spinnaker"]]
    assert sail_id not in ids


@pytest.mark.asyncio
async def test_get_session_sails_empty(storage: Storage) -> None:
    """GET /api/sessions/{id}/sails returns all-None slots when no sails are set."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.get(f"/api/sessions/{race_id}/sails")
    assert resp.status_code == 200
    data = resp.json()
    assert data["main"] is None
    assert data["jib"] is None
    assert data["spinnaker"] is None


@pytest.mark.asyncio
async def test_set_session_sails(storage: Storage) -> None:
    """PUT /api/sessions/{id}/sails sets sails; GET returns them."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        main_id = await _add_sail(client, "main", "Full Main")
        jib_id = await _add_sail(client, "jib", "Jib Top")

        put_resp = await client.put(
            f"/api/sessions/{race_id}/sails",
            json={"main_id": main_id, "jib_id": jib_id, "spinnaker_id": None},
        )
        assert put_resp.status_code == 200

        get_resp = await client.get(f"/api/sessions/{race_id}/sails")
    data = get_resp.json()
    assert data["main"] is not None
    assert data["main"]["id"] == main_id
    assert data["jib"] is not None
    assert data["jib"]["id"] == jib_id
    assert data["spinnaker"] is None


@pytest.mark.asyncio
async def test_set_session_sails_wrong_type_422(storage: Storage) -> None:
    """PUT /api/sessions/{id}/sails returns 422 if sail id doesn't match the slot type."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        # Add a jib, then try to put it in the main slot
        jib_id = await _add_sail(client, "jib", "Jib Top")
        resp = await client.put(
            f"/api/sessions/{race_id}/sails",
            json={"main_id": jib_id},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_session_sails_unknown_session_404(storage: Storage) -> None:
    """PUT /api/sessions/{id}/sails returns 404 for an unknown session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/sessions/99999/sails", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_state_includes_sails(storage: Storage) -> None:
    """GET /api/state today_races entries include sails dict."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        main_id = await _add_sail(client, "main", "Full Main")
        await client.put(
            f"/api/sessions/{race_id}/sails",
            json={"main_id": main_id},
        )
        state = (await client.get("/api/state")).json()

    assert "sails" in state["current_race"]
    assert state["current_race"]["sails"]["main"]["id"] == main_id


# ---------------------------------------------------------------------------
# Issue #308 — Point-of-sail field for sails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_sail_with_explicit_point_of_sail(storage: Storage) -> None:
    """POST /api/sails with explicit point_of_sail stores and returns it."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/sails", json={"type": "main", "name": "Reef 1", "point_of_sail": "upwind"}
        )
        resp = await client.get("/api/sails")
    data = resp.json()
    main_sails = data["main"]
    assert len(main_sails) == 1
    assert main_sails[0]["point_of_sail"] == "upwind"


@pytest.mark.asyncio
async def test_add_sail_default_point_of_sail(storage: Storage) -> None:
    """POST /api/sails without point_of_sail defaults by type."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _add_sail(client, "main", "Full Main")
        await _add_sail(client, "jib", "J1")
        await _add_sail(client, "spinnaker", "A2")
        resp = await client.get("/api/sails")
    data = resp.json()
    assert data["main"][0]["point_of_sail"] == "both"
    assert data["jib"][0]["point_of_sail"] == "upwind"
    assert data["spinnaker"][0]["point_of_sail"] == "downwind"


@pytest.mark.asyncio
async def test_update_sail_point_of_sail(storage: Storage) -> None:
    """PATCH /api/sails/{id} can update point_of_sail."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sail_id = await _add_sail(client, "main", "Full Main")
        patch_resp = await client.patch(f"/api/sails/{sail_id}", json={"point_of_sail": "upwind"})
        assert patch_resp.status_code == 200
        resp = await client.get("/api/sails")
    data = resp.json()
    assert data["main"][0]["point_of_sail"] == "upwind"


@pytest.mark.asyncio
async def test_add_sail_invalid_point_of_sail_422(storage: Storage) -> None:
    """POST /api/sails with invalid point_of_sail returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sails", json={"type": "main", "name": "Test", "point_of_sail": "sideways"}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_sail_invalid_point_of_sail_422(storage: Storage) -> None:
    """PATCH /api/sails/{id} with invalid point_of_sail returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sail_id = await _add_sail(client, "main", "Full Main")
        resp = await client.patch(f"/api/sails/{sail_id}", json={"point_of_sail": "sideways"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_sails_includes_point_of_sail(storage: Storage) -> None:
    """GET /api/sails includes point_of_sail in each sail entry."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _add_sail(client, "jib", "J1")
        resp = await client.get("/api/sails")
    data = resp.json()
    assert "point_of_sail" in data["jib"][0]


# ---------------------------------------------------------------------------
# Sail defaults (#306)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sail_defaults_empty(storage: Storage) -> None:
    """GET /api/sails/defaults returns all-None when no defaults are set."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sails/defaults")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"main": None, "jib": None, "spinnaker": None}


@pytest.mark.asyncio
async def test_set_and_get_sail_defaults(storage: Storage) -> None:
    """PUT /api/sails/defaults persists and GET returns them."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        main_id = await _add_sail(client, "main", "Full Main")
        jib_id = await _add_sail(client, "jib", "J1")
        resp = await client.put(
            "/api/sails/defaults",
            json={"main_id": main_id, "jib_id": jib_id, "spinnaker_id": None},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["main"]["id"] == main_id
        assert data["jib"]["id"] == jib_id
        assert data["spinnaker"] is None

        # GET should return the same
        resp2 = await client.get("/api/sails/defaults")
        assert resp2.json() == data


@pytest.mark.asyncio
async def test_set_sail_defaults_invalid_id_422(storage: Storage) -> None:
    """PUT /api/sails/defaults with non-existent sail returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/sails/defaults", json={"main_id": 999})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_sail_defaults_wrong_type_422(storage: Storage) -> None:
    """PUT /api/sails/defaults with sail in wrong slot returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        jib_id = await _add_sail(client, "jib", "J1")
        resp = await client.put("/api/sails/defaults", json={"main_id": jib_id})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_clear_sail_defaults(storage: Storage) -> None:
    """PUT /api/sails/defaults with all None clears defaults."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        main_id = await _add_sail(client, "main", "Full Main")
        await client.put("/api/sails/defaults", json={"main_id": main_id})
        # Clear
        resp = await client.put(
            "/api/sails/defaults",
            json={"main_id": None, "jib_id": None, "spinnaker_id": None},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"main": None, "jib": None, "spinnaker": None}


@pytest.mark.asyncio
async def test_sail_defaults_do_not_affect_session_sails(storage: Storage) -> None:
    """Session sails remain independent of defaults."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        main_id = await _add_sail(client, "main", "Full Main")
        jib_id = await _add_sail(client, "jib", "J1")

        # Set defaults to main only
        await client.put("/api/sails/defaults", json={"main_id": main_id})

        # Start a session and set sails to jib only
        await _set_event(client)
        race_resp = await client.post("/api/races/start")
        session_id = race_resp.json()["id"]
        await client.put(
            f"/api/sessions/{session_id}/sails",
            json={"main_id": None, "jib_id": jib_id, "spinnaker_id": None},
        )

        # Session sails should be jib only
        sails_resp = await client.get(f"/api/sessions/{session_id}/sails")
        sails = sails_resp.json()
        assert sails["main"] is None
        assert sails["jib"]["id"] == jib_id

        # Defaults should still be main only
        defaults_resp = await client.get("/api/sails/defaults")
        defaults = defaults_resp.json()
        assert defaults["main"]["id"] == main_id
        assert defaults["jib"] is None


# ---------------------------------------------------------------------------
# Sail stats & session history (#307)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sail_stats_empty(storage: Storage) -> None:
    """GET /api/sails/stats returns empty list when no sails exist."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sails/stats")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_sail_stats_with_sails(storage: Storage) -> None:
    """GET /api/sails/stats returns sails with zero counts when no sessions."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _add_sail(client, "main", "Full Main")
        await _add_sail(client, "jib", "J1")
        resp = await client.get("/api/sails/stats")
    data = resp.json()
    assert len(data) == 2
    for s in data:
        assert s["total_tacks"] == 0
        assert s["total_gybes"] == 0
        assert s["total_sessions"] == 0


@pytest.mark.asyncio
async def test_sail_session_history_empty(storage: Storage) -> None:
    """GET /api/sails/{id}/sessions returns empty list for unused sail."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        sail_id = await _add_sail(client, "main", "Full Main")
        resp = await client.get(f"/api/sails/{sail_id}/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_sail_session_history_with_session(storage: Storage) -> None:
    """GET /api/sails/{id}/sessions returns session info when sail is used."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        main_id = await _add_sail(client, "main", "Full Main")
        await _set_event(client)
        race = (await client.post("/api/races/start")).json()
        await client.put(
            f"/api/sessions/{race['id']}/sails",
            json={"main_id": main_id},
        )
        resp = await client.get(f"/api/sails/{main_id}/sessions")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == race["id"]
    assert data[0]["tacks"] == 0
    assert data[0]["gybes"] == 0  # main is 'both', so both tacks and gybes are shown


@pytest.mark.asyncio
async def test_sail_stats_counts_sessions(storage: Storage) -> None:
    """GET /api/sails/stats counts sessions a sail was used in."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        main_id = await _add_sail(client, "main", "Full Main")
        await _set_event(client)
        race = (await client.post("/api/races/start")).json()
        await client.put(
            f"/api/sessions/{race['id']}/sails",
            json={"main_id": main_id},
        )
        resp = await client.get("/api/sails/stats")
    data = resp.json()
    main_stat = next(s for s in data if s["id"] == main_id)
    assert main_stat["total_sessions"] == 1


@pytest.mark.asyncio
async def test_sails_page_renders(storage: Storage) -> None:
    """GET /sails returns 200."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/sails")
    assert resp.status_code == 200
    assert "Sails" in resp.text


# ---------------------------------------------------------------------------
# Audio download / stream endpoints (#21)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_audio_404_unknown_session(storage: Storage) -> None:
    """GET /api/audio/999/download returns 404 when session_id does not exist."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/audio/999/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_audio_404_missing_file(storage: Storage, tmp_path: Path) -> None:
    """GET /api/audio/{id}/download returns 404 when DB row exists but file is gone."""
    from helmlog.audio import AudioSession

    session = AudioSession(
        file_path=str(tmp_path / "missing.wav"),
        device_name="Test",
        start_utc=_START_UTC,
        end_utc=_END_UTC,
        sample_rate=48000,
        channels=1,
    )
    session_id = await storage.write_audio_session(session)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{session_id}/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_audio_200(storage: Storage, tmp_path: Path) -> None:
    """GET /api/audio/{id}/download returns 200 with Content-Disposition attachment."""
    from helmlog.audio import AudioSession

    wav_file = tmp_path / "test.wav"
    wav_file.write_bytes(b"RIFF")  # minimal stub
    session = AudioSession(
        file_path=str(wav_file),
        device_name="Test",
        start_utc=_START_UTC,
        end_utc=_END_UTC,
        sample_rate=48000,
        channels=1,
    )
    session_id = await storage.write_audio_session(session)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{session_id}/download")
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "test.wav" in resp.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_stream_audio_200(storage: Storage, tmp_path: Path) -> None:
    """GET /api/audio/{id}/stream returns 200 with audio/wav media type."""
    from helmlog.audio import AudioSession

    wav_file = tmp_path / "stream.wav"
    wav_file.write_bytes(b"RIFF")
    session = AudioSession(
        file_path=str(wav_file),
        device_name="Test",
        start_utc=_START_UTC,
        end_utc=_END_UTC,
        sample_rate=48000,
        channels=1,
    )
    session_id = await storage.write_audio_session(session)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{session_id}/stream")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("audio/wav")


# ---------------------------------------------------------------------------
# Photo caching / lazy loading (#44)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_attachment_cache_headers(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /attachments/{path} returns Cache-Control and ETag headers."""
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/attachments/photo.jpg")
    assert resp.status_code == 200
    assert "max-age=31536000" in resp.headers.get("cache-control", "")
    assert resp.headers.get("etag", "") != ""


@pytest.mark.asyncio
async def test_serve_attachment_304_if_none_match(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /attachments/{path} returns 304 when ETag matches If-None-Match."""
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/attachments/photo.jpg")
        etag = first.headers["etag"]
        second = await client.get("/attachments/photo.jpg", headers={"If-None-Match": etag})
    assert second.status_code == 304


@pytest.mark.asyncio
async def test_serve_attachment_403_traversal(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /attachments/{path} with percent-encoded path traversal is blocked."""
    base = tmp_path / "atts"
    base.mkdir()
    monkeypatch.setenv("ATTACHMENTS_DIR", str(base))
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/attachments/%2e%2e/secret.txt")
    assert resp.status_code in {403, 404}


@pytest.mark.asyncio
async def test_serve_attachment_404(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /attachments/{path} returns 404 when the file does not exist."""
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/attachments/nonexistent.jpg")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# System health endpoint (#39)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_health_returns_200(storage: Storage) -> None:
    """GET /api/system-health returns 200 with cpu_pct, mem_pct, disk_pct keys."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system-health")
    assert resp.status_code == 200
    data = resp.json()
    assert "cpu_pct" in data
    assert "mem_pct" in data
    assert "disk_pct" in data
    assert isinstance(data["cpu_pct"], float | int)
    assert isinstance(data["mem_pct"], float | int)
    assert isinstance(data["disk_pct"], float | int)


# ---------------------------------------------------------------------------
# Transcription endpoints (#42)
# ---------------------------------------------------------------------------


async def _create_audio_session(storage: Storage, tmp_path: Path) -> int:
    """Helper: insert a real audio session row with a stub WAV file; return session_id."""
    from helmlog.audio import AudioSession

    wav_file = tmp_path / "test.wav"
    wav_file.write_bytes(b"RIFF")
    session = AudioSession(
        file_path=str(wav_file),
        device_name="Test",
        start_utc=_START_UTC,
        end_utc=_END_UTC,
        sample_rate=48000,
        channels=1,
    )
    return await storage.write_audio_session(session)


@pytest.mark.asyncio
async def test_get_transcript_no_job_404(storage: Storage, tmp_path: Path) -> None:
    """GET /api/audio/{id}/transcript returns 404 when no job exists."""
    session_id = await _create_audio_session(storage, tmp_path)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{session_id}/transcript")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_transcript_job_202(storage: Storage, tmp_path: Path) -> None:
    """POST /api/audio/{id}/transcribe returns 202; GET shows status pending/running."""
    from unittest.mock import AsyncMock, patch

    session_id = await _create_audio_session(storage, tmp_path)
    app = create_app(storage)

    # Mock transcribe_session so it doesn't actually run faster-whisper
    with patch("helmlog.web.asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            post_resp = await client.post(f"/api/audio/{session_id}/transcribe")
            assert post_resp.status_code == 202
            assert post_resp.json()["status"] == "accepted"

            # Job exists; second POST returns 409
            post_resp2 = await client.post(f"/api/audio/{session_id}/transcribe")
            assert post_resp2.status_code == 409

            # GET returns the job row
            get_resp = await client.get(f"/api/audio/{session_id}/transcript")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["status"] in {"pending", "running"}


@pytest.mark.asyncio
async def test_transcript_done(storage: Storage, tmp_path: Path) -> None:
    """After transcription completes, GET returns {status:'done', text:...}."""
    from unittest.mock import patch

    session_id = await _create_audio_session(storage, tmp_path)

    # Directly exercise the storage + transcribe_session with mocked WhisperModel
    _segs = [(0.0, 2.0, "Hello"), (2.1, 4.0, "world")]
    with patch("helmlog.transcribe._run_whisper_segments", return_value=_segs):
        from helmlog.transcribe import transcribe_session

        transcript_id = await storage.create_transcript_job(session_id, "base")
        await transcribe_session(storage, session_id, transcript_id, model_size="base")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{session_id}/transcript")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["text"] == "Hello world"
    assert "segments_json" not in data


@pytest.mark.asyncio
async def test_transcript_done_with_segments(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diarised transcription stores segments; GET exposes them and omits segments_json."""
    from unittest.mock import patch

    monkeypatch.setenv("HF_TOKEN", "fake-token")

    _whisper_segs = [(0.0, 3.0, "Ready about."), (3.1, 6.5, "Ready.")]
    _diar_segs = [(0.0, 3.5, "A"), (3.5, 7.0, "B")]

    session_id = await _create_audio_session(storage, tmp_path)

    with (
        patch("helmlog.transcribe._run_whisper_segments", return_value=_whisper_segs),
        patch("helmlog.transcribe._run_diarizer", return_value=_diar_segs),
        patch("helmlog.transcribe._pyannote_available", return_value=True),
    ):
        from helmlog.transcribe import transcribe_session

        transcript_id = await storage.create_transcript_job(session_id, "base")
        await transcribe_session(
            storage, session_id, transcript_id, model_size="base", diarize=True
        )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{session_id}/transcript")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert "segments_json" not in data
    segs = data["segments"]
    assert isinstance(segs, list)
    assert len(segs) == 2
    # #648: every segment now carries audio_session_id + segment_index so the
    # client can address it for per-segment speaker-override POSTs.
    assert {"start", "end", "speaker", "text", "audio_session_id", "segment_index"} <= set(
        segs[0].keys()
    )
    assert segs[0]["speaker"] == "SPEAKER_00"
    assert segs[1]["speaker"] == "SPEAKER_01"


# ---------------------------------------------------------------------------
# /api/polar/current
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_polar_current_empty_db(storage: Storage) -> None:
    """GET /api/polar/current with no data → 200, all nulls, sufficient_data=False."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/polar/current")

    assert resp.status_code == 200
    data = resp.json()
    assert data["sufficient_data"] is False
    assert data["bsp"] is None
    assert data["baseline_bsp"] is None
    assert data["delta"] is None


@pytest.mark.asyncio
async def test_polar_current_with_baseline(storage: Storage) -> None:
    """GET /api/polar/current with baseline seeded → sufficient_data=True, correct delta."""
    from datetime import timedelta

    from helmlog.polar import build_polar_baseline

    base_ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

    # Seed 3 races, each with BSP=6.0, TWS=10.0, TWA=45°
    for race_num in range(1, 4):
        start = base_ts + timedelta(hours=race_num)
        end = start + timedelta(seconds=10)
        db = storage._conn()
        await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
            " VALUES (?, 'TestEvent', ?, ?, ?, ?)",
            (
                f"TestEvent-R{race_num}",
                race_num,
                start.date().isoformat(),
                start.isoformat(),
                end.isoformat(),
            ),
        )
        await db.commit()
        for i in range(10):
            ts = start + timedelta(seconds=i)
            await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.0))
            await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))

    await build_polar_baseline(storage)

    # Set live instruments: BSP=7.0, TWS=10.2, TWA=46°
    live_ts = datetime(2024, 6, 10, 12, 0, 0, tzinfo=UTC)
    storage.update_live(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, live_ts, 7.0))
    storage.update_live(WindRecord(PGN_WIND_DATA, 5, live_ts, 10.2, 46.0, 0))

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/polar/current")

    assert resp.status_code == 200
    data = resp.json()
    assert data["sufficient_data"] is True
    assert data["baseline_bsp"] == pytest.approx(6.0, rel=1e-2)
    assert data["delta"] == pytest.approx(1.0, rel=1e-2)


# ---------------------------------------------------------------------------
# Camera CRUD via web API (#147)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_camera_crud_api(storage: Storage) -> None:
    """Add, list, update, and delete cameras via the web API."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Add a camera
        resp = await client.post(
            "/api/cameras",
            json={"name": "bow", "ip": "192.168.42.1"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "bow"
        assert data["ip"] == "192.168.42.1"
        assert data["model"] == "insta360-x4"

        # Duplicate should fail
        resp = await client.post(
            "/api/cameras",
            json={"name": "bow", "ip": "10.0.0.1"},
        )
        assert resp.status_code == 409

        # Update IP
        resp = await client.put(
            "/api/cameras/bow",
            json={"ip": "10.0.0.2"},
        )
        assert resp.status_code == 200

        # Rename
        resp = await client.put(
            "/api/cameras/bow",
            json={"name": "stern", "ip": "10.0.0.3"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "stern"

        # Delete
        resp = await client.delete("/api/cameras/stern")
        assert resp.status_code == 204

        # Delete again should 404
        resp = await client.delete("/api/cameras/stern")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_camera_add_validation(storage: Storage) -> None:
    """Missing name or ip should return 400."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/cameras", json={"name": "", "ip": "1.2.3.4"})
        assert resp.status_code == 400

        resp = await client.post("/api/cameras", json={"name": "bow", "ip": ""})
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_camera_admin_page_loads(storage: Storage) -> None:
    """GET /admin/cameras returns the camera admin HTML page."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/cameras")

    assert resp.status_code == 200
    assert "Cameras" in resp.text
    assert "Add Camera" in resp.text


# ---------------------------------------------------------------------------
# Boat settings API tests
# ---------------------------------------------------------------------------

_BS_START_UTC = datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)


async def _make_race_for_settings(client: httpx.AsyncClient) -> int:
    """Set an event and start a race, return race_id."""
    await client.post("/api/event", json={"event_name": "SettingsTest"})
    resp = await client.post("/api/races/start")
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_boat_settings_parameters(storage: Storage) -> None:
    """GET /api/boat-settings/parameters returns canonical definitions."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/boat-settings/parameters")
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    assert "weight_distribution_presets" in data
    cats = [c["category"] for c in data["categories"]]
    assert "sail_controls" in cats
    assert "rig" in cats


@pytest.mark.asyncio
async def test_boat_settings_create_and_list(storage: Storage) -> None:
    """POST /api/boat-settings creates entries; GET lists them."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_settings(client)
        ts = _BS_START_UTC.isoformat()
        resp = await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "manual",
                "entries": [
                    {"ts": ts, "parameter": "backstay", "value": "3.5"},
                    {"ts": ts, "parameter": "vang", "value": "2.0"},
                ],
            },
        )
        assert resp.status_code == 201
        assert len(resp.json()["ids"]) == 2

        resp = await client.get(f"/api/boat-settings?race_id={race_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_boat_settings_current(storage: Storage) -> None:
    """GET /api/boat-settings/current returns latest per parameter."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_settings(client)
        ts1 = _BS_START_UTC.isoformat()
        ts2 = datetime(2026, 3, 12, 14, 1, 0, tzinfo=UTC).isoformat()
        await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "manual",
                "entries": [{"ts": ts1, "parameter": "backstay", "value": "3.0"}],
            },
        )
        await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "manual",
                "entries": [{"ts": ts2, "parameter": "backstay", "value": "4.5"}],
            },
        )
        resp = await client.get(f"/api/boat-settings/current?race_id={race_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["value"] == "4.5"


@pytest.mark.asyncio
async def test_boat_settings_rejects_unknown_param(storage: Storage) -> None:
    """POST /api/boat-settings with unknown parameter returns 400."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_settings(client)
        resp = await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "manual",
                "entries": [
                    {"ts": _BS_START_UTC.isoformat(), "parameter": "fake_param", "value": "1"}
                ],
            },
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_boat_settings_delete_extraction_run(storage: Storage) -> None:
    """DELETE extraction run removes only those entries."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_settings(client)
        ts = _BS_START_UTC.isoformat()
        await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "manual",
                "entries": [{"ts": ts, "parameter": "backstay", "value": "3.0"}],
            },
        )
        await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "transcript:whisper-base",
                "extraction_run_id": 99,
                "entries": [{"ts": ts, "parameter": "vang", "value": "2.0"}],
            },
        )
        resp = await client.delete("/api/boat-settings/extraction-run/99")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

        resp = await client.get(f"/api/boat-settings?race_id={race_id}")
        assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_boat_settings_null_race_id(storage: Storage) -> None:
    """Boat settings can be saved and loaded without a race (dock setup)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ts = "2024-08-01T10:00:00Z"
        resp = await client.post(
            "/api/boat-settings",
            json={
                "race_id": None,
                "source": "manual",
                "entries": [{"ts": ts, "parameter": "shroud_tension_upper", "value": "28"}],
            },
        )
        assert resp.status_code == 201

        resp = await client.get("/api/boat-settings/current")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["parameter"] == "shroud_tension_upper"
        assert rows[0]["value"] == "28"

        resp = await client.get("/api/boat-settings")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_boat_settings_resolve(storage: Storage) -> None:
    """GET /api/boat-settings/resolve merges race-specific over boat-level."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_settings(client)
        ts_dock = datetime(2026, 3, 12, 13, 0, 0, tzinfo=UTC).isoformat()
        ts_race = datetime(2026, 3, 12, 14, 5, 0, tzinfo=UTC).isoformat()
        as_of = datetime(2026, 3, 12, 14, 10, 0, tzinfo=UTC).isoformat()

        # Boat-level default
        await client.post(
            "/api/boat-settings",
            json={
                "race_id": None,
                "source": "manual",
                "entries": [
                    {"ts": ts_dock, "parameter": "backstay", "value": "3.0"},
                    {"ts": ts_dock, "parameter": "vang", "value": "2.0"},
                ],
            },
        )
        # Race-specific override
        await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "transcript:whisper-base",
                "entries": [{"ts": ts_race, "parameter": "backstay", "value": "5.0"}],
            },
        )

        resp = await client.get(f"/api/boat-settings/resolve?race_id={race_id}&as_of={as_of}")
        assert resp.status_code == 200
        data = resp.json()
        by_param = {r["parameter"]: r for r in data}

        # backstay: race-specific wins
        assert by_param["backstay"]["value"] == "5.0"
        assert by_param["backstay"]["supersedes_value"] == "3.0"

        # vang: boat-level fallback
        assert by_param["vang"]["value"] == "2.0"
        assert by_param["vang"]["supersedes_value"] is None


@pytest.mark.asyncio
async def test_instrument_calibration_in_parameters(storage: Storage) -> None:
    """GET /api/boat-settings/parameters includes the instrument_calibration category."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/boat-settings/parameters")
    assert resp.status_code == 200
    data = resp.json()
    cats = [c["category"] for c in data["categories"]]
    assert "instrument_calibration" in cats
    # Should be the last category
    assert cats[-1] == "instrument_calibration"

    # All 15 calibration params present
    cal_params = next(c for c in data["categories"] if c["category"] == "instrument_calibration")
    param_names = [p["name"] for p in cal_params["parameters"]]
    expected = [
        "speed_correction",
        "speed_damping",
        "heading_offset",
        "heading_damping",
        "wind_angle_offset",
        "wind_speed_correction",
        "wind_damping",
        "depth_offset",
        "depth_damping",
        "sea_temp_offset",
        "heel_offset",
        "trim_offset",
        "leeway_coefficient",
        "rudder_angle_offset",
        "mast_height",
    ]
    assert param_names == expected


@pytest.mark.asyncio
async def test_instrument_calibration_h5000_labels(storage: Storage) -> None:
    """H5000-only parameters include (H5000) in the label."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/boat-settings/parameters")
    data = resp.json()
    cal_params = next(c for c in data["categories"] if c["category"] == "instrument_calibration")
    by_name = {p["name"]: p for p in cal_params["parameters"]}

    for name in ("heel_offset", "trim_offset", "leeway_coefficient", "rudder_angle_offset"):
        assert "H5000" in by_name[name]["label"], f"{name} should have H5000 in label"

    # Non-H5000 params should NOT have H5000 in label
    for name in ("speed_correction", "heading_offset", "wind_angle_offset", "mast_height"):
        assert "H5000" not in by_name[name]["label"], f"{name} should not have H5000 in label"


@pytest.mark.asyncio
async def test_instrument_calibration_create_and_retrieve(storage: Storage) -> None:
    """POST/GET boat-settings works for calibration parameters."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_settings(client)
        ts = _BS_START_UTC.isoformat()
        resp = await client.post(
            "/api/boat-settings",
            json={
                "race_id": race_id,
                "source": "manual",
                "entries": [
                    {"ts": ts, "parameter": "heading_offset", "value": "2.5"},
                    {"ts": ts, "parameter": "speed_correction", "value": "-3"},
                ],
            },
        )
        assert resp.status_code == 201
        assert len(resp.json()["ids"]) == 2

        resp = await client.get(f"/api/boat-settings/current?race_id={race_id}")
        assert resp.status_code == 200
        by_param = {r["parameter"]: r for r in resp.json()}
        assert by_param["heading_offset"]["value"] == "2.5"
        assert by_param["speed_correction"]["value"] == "-3"


@pytest.mark.asyncio
async def test_home_page_has_setup_panel(storage: Storage) -> None:
    """GET / includes the boat setup accordion card."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="setup-card"' in resp.text
    assert "Boat Setup" in resp.text


# ---------------------------------------------------------------------------
# Weight endpoint tests (#305)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_weight_requires_biometric_consent(storage: Storage) -> None:
    """PATCH /api/me/weight rejects weight update without biometric consent.

    Note: when AUTH_DISABLED=true, the mock admin has id=None, so the consent
    lookup returns empty and the 403 fires.
    """
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch("/api/me/weight", json={"weight_lbs": 175.0})
    assert resp.status_code == 403
    assert "Biometric consent" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_patch_weight_null_clears_without_consent(storage: Storage) -> None:
    """PATCH /api/me/weight with null weight clears it without needing consent."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch("/api/me/weight", json={"weight_lbs": None})
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_patch_weight_invalid_type(storage: Storage) -> None:
    """PATCH /api/me/weight rejects non-numeric weight."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch("/api/me/weight", json={"weight_lbs": "heavy"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_name_updates_display_name(storage: Storage) -> None:
    """PATCH /api/me/name updates the user's display name."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch("/api/me/name", json={"name": "New Name"})
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_patch_name_rejects_blank(storage: Storage) -> None:
    """PATCH /api/me/name rejects blank name."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch("/api/me/name", json={"name": ""})
    assert resp.status_code == 422
    assert "blank" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Consent / anonymize endpoint tests (#305)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_consent(storage: Storage) -> None:
    """PUT then GET consent for a crew member."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        uid = await _make_crew_user(client, "ConsentPerson")
        put_resp = await client.put(
            f"/api/crew/{uid}/consents",
            json={"consent_type": "audio", "granted": True},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["granted"] is True

        get_resp = await client.get(f"/api/crew/{uid}/consents")
    assert get_resp.status_code == 200
    consents = get_resp.json()
    assert len(consents) == 1
    assert consents[0]["consent_type"] == "audio"


@pytest.mark.asyncio
async def test_set_consent_invalid_type(storage: Storage) -> None:
    """PUT consent with invalid type returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        uid = await _make_crew_user(client, "BadConsent")
        resp = await client.put(
            f"/api/crew/{uid}/consents",
            json={"consent_type": "invalid", "granted": True},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_anonymize_sailor(storage: Storage) -> None:
    """POST anonymize replaces user name."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        uid = await _make_crew_user(client, "NamedSailor")
        resp = await client.post(f"/api/crew/{uid}/anonymize")
    assert resp.status_code == 200
    assert resp.json()["rows_updated"] == 1


# ---------------------------------------------------------------------------
# Issue #311 — Timestamped sail changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_sails_creates_sail_change(storage: Storage) -> None:
    """PUT /api/sessions/{id}/sails creates a timestamped sail_changes row."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        main_id = await _add_sail(client, "main", "Full Main")

        await client.put(
            f"/api/sessions/{race_id}/sails",
            json={"main_id": main_id, "jib_id": None, "spinnaker_id": None},
        )
        changes_resp = await client.get(f"/api/sessions/{race_id}/sail-changes")
    assert changes_resp.status_code == 200
    changes = changes_resp.json()["changes"]
    # At least the auto-applied defaults row + the PUT row
    assert len(changes) >= 1
    latest = changes[-1]
    assert latest["main"] is not None
    assert latest["main"]["id"] == main_id


@pytest.mark.asyncio
async def test_multiple_puts_get_returns_latest(storage: Storage) -> None:
    """Multiple PUTs → GET returns latest; GET sail-changes returns full history."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        main1 = await _add_sail(client, "main", "Full Main")
        main2 = await _add_sail(client, "main", "Reef 1")
        jib1 = await _add_sail(client, "jib", "J1")

        await client.put(
            f"/api/sessions/{race_id}/sails",
            json={"main_id": main1, "jib_id": jib1, "spinnaker_id": None},
        )
        await client.put(
            f"/api/sessions/{race_id}/sails",
            json={"main_id": main2, "jib_id": jib1, "spinnaker_id": None},
        )

        # GET returns latest
        get_resp = await client.get(f"/api/sessions/{race_id}/sails")
        data = get_resp.json()
        assert data["main"]["id"] == main2
        assert data["jib"]["id"] == jib1

        # GET sail-changes returns full history
        changes_resp = await client.get(f"/api/sessions/{race_id}/sail-changes")
        changes = changes_resp.json()["changes"]
        # At least the 2 explicit PUTs
        assert len(changes) >= 2
        main_ids = [c["main"]["id"] for c in changes if c["main"] is not None]
        assert main2 in main_ids


@pytest.mark.asyncio
async def test_sail_changes_404_unknown_session(storage: Storage) -> None:
    """GET /api/sessions/{id}/sail-changes returns 404 for unknown session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/99999/sail-changes")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_race_start_auto_applies_sail_defaults(storage: Storage) -> None:
    """Starting a race auto-applies sail defaults as initial sail_changes row."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Create sails and set defaults
        main_id = await _add_sail(client, "main", "Full Main")
        jib_id = await _add_sail(client, "jib", "J1")
        await client.put(
            "/api/sails/defaults",
            json={"main_id": main_id, "jib_id": jib_id, "spinnaker_id": None},
        )

        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]

        # Session sails should reflect defaults
        sails_resp = await client.get(f"/api/sessions/{race_id}/sails")
        sails = sails_resp.json()
        assert sails["main"] is not None
        assert sails["main"]["id"] == main_id
        assert sails["jib"] is not None
        assert sails["jib"]["id"] == jib_id

        # sail-changes should have the initial row
        changes_resp = await client.get(f"/api/sessions/{race_id}/sail-changes")
        changes = changes_resp.json()["changes"]
        assert len(changes) >= 1
        assert changes[0]["main"]["id"] == main_id


@pytest.mark.asyncio
async def test_v41_migration_creates_sail_changes_table(storage: Storage) -> None:
    """Migration v41 creates the sail_changes table."""
    db = storage._conn()
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sail_changes'"
    )
    row = await cur.fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Replay endpoint (#464, #465, #468, #470)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_endpoint_unknown_session_returns_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/9999/replay")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_replay_endpoint_returns_samples_and_grades(storage: Storage) -> None:
    """Seeds a completed race with instrument data, hits /replay, and
    verifies the payload includes per-second samples, graded segments,
    and the expected top-level fields.
    """
    from datetime import timedelta

    from helmlog.nmea2000 import (
        PGN_POSITION_RAPID,
        PGN_SPEED_THROUGH_WATER,
        PGN_WIND_DATA,
        PositionRecord,
        SpeedRecord,
        WindRecord,
    )

    start = datetime(2024, 8, 1, 12, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=30)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Replay', 'E', 1, ?, ?, ?)",
        (start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    race_id = int((await cur.fetchone())["id"])
    for i in range(30):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.0))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))
        await storage.write(
            PositionRecord(PGN_POSITION_RAPID, 5, ts, 37.80 + i * 1e-5, -122.27 + i * 1e-5)
        )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/replay")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == race_id
    assert data["start_utc"].startswith("2024-08-01T12:00:00")
    assert data["end_utc"].startswith("2024-08-01T12:00:30")
    assert data["segment_seconds"] == 10
    assert len(data["samples"]) == 30
    sample = data["samples"][0]
    assert sample["stw"] == pytest.approx(6.0)
    assert sample["tws"] == pytest.approx(10.0)
    assert sample["twa"] == pytest.approx(45.0)
    # ref=0 is boat-referenced true wind and we seeded no heading, so TWD
    # cannot be derived and must surface as None (not NaN or 0).
    assert sample["twd"] is None
    # 30s / 10s per segment → 3 graded segments (grade is "unknown" because
    # no baseline has been built — we only check shape here).
    assert len(data["grades"]) == 3
    for g in data["grades"]:
        assert "grade" in g
        assert "t_start" in g
        assert "t_end" in g


@pytest.mark.asyncio
async def test_replay_endpoint_returns_twd_when_heading_present(storage: Storage) -> None:
    """Boat-referenced true wind + heading → TWD = (heading + wind_angle) mod 360."""
    from datetime import timedelta

    from helmlog.nmea2000 import (
        PGN_VESSEL_HEADING,
        PGN_WIND_DATA,
        HeadingRecord,
        WindRecord,
    )

    start = datetime(2024, 8, 2, 12, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=5)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('TWD', 'E', 1, ?, ?, ?)",
        (start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    race_id = int((await cur.fetchone())["id"])
    for i in range(5):
        ts = start + timedelta(seconds=i)
        await storage.write(HeadingRecord(PGN_VESSEL_HEADING, 5, ts, 180.0, None, None))
        # ref=0 (boat-referenced), wind_angle=45 (TWA). Heading 180 → TWD 225.
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/replay")

    assert resp.status_code == 200
    data = resp.json()
    sample = data["samples"][0]
    assert sample["twd"] == pytest.approx(225.0)


@pytest.mark.asyncio
async def test_replay_endpoint_returns_heel_and_trim(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attitude rows inside the session window surface as heel and trim on
    every replay sample. Sign convention: heel positive = starboard down,
    trim positive = bow up (see nmea2000.AttitudeRecord)."""
    # _write_attitude rate-limits to 2 Hz by default; the test loop fires
    # writes in microseconds so only the first would land. Disable it.
    monkeypatch.setenv("ATTITUDE_STORAGE_HZ", "0")
    from datetime import timedelta

    from helmlog.nmea2000 import PGN_ATTITUDE, AttitudeRecord

    start = datetime(2024, 8, 3, 12, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=5)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('Attitude', 'E', 1, ?, ?, ?)",
        (start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    race_id = int((await cur.fetchone())["id"])
    for i in range(5):
        ts = start + timedelta(seconds=i)
        await storage.write(
            AttitudeRecord(PGN_ATTITUDE, 5, ts, heel_deg=12.5 + i, trim_deg=-1.5 + i * 0.1)
        )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/replay")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["samples"]) == 5
    first = data["samples"][0]
    assert first["heel"] == pytest.approx(12.5)
    assert first["trim"] == pytest.approx(-1.5)
    last = data["samples"][-1]
    assert last["heel"] == pytest.approx(16.5)
    assert last["trim"] == pytest.approx(-1.1)


@pytest.mark.asyncio
async def test_replay_endpoint_heel_trim_absent_when_no_attitude_rows(storage: Storage) -> None:
    """Sessions recorded before schema v75 (no attitudes) still return the
    heel/trim keys, set to None — the frontend relies on the key being
    present so it can render em-dashes rather than crashing on undefined."""
    from datetime import timedelta

    from helmlog.nmea2000 import PGN_SPEED_THROUGH_WATER, SpeedRecord

    start = datetime(2024, 8, 4, 12, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=3)
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES ('NoAttitude', 'E', 1, ?, ?, ?)",
        (start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    race_id = int((await cur.fetchone())["id"])
    for i in range(3):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.0))

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/replay")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["samples"]) == 3
    for s in data["samples"]:
        assert s["heel"] is None
        assert s["trim"] is None


@pytest.mark.asyncio
async def test_course_overlay_unknown_session_returns_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/9999/course-overlay")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_course_overlay_returns_marks_and_empty_line(storage: Storage) -> None:
    """Synth race with course marks and no Vakaros match: marks come back,
    start_line is null."""
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc)"
        " VALUES ('Course', 'E', 1, '2024-08-01',"
        " '2024-08-01T12:00:00+00:00')"
    )
    await db.commit()
    cur = await db.execute("SELECT id FROM races ORDER BY id DESC LIMIT 1")
    race_id = int((await cur.fetchone())["id"])
    await storage.save_synth_course_marks(
        race_id,
        [
            {"mark_key": "1", "mark_name": "Windward", "lat": 37.81, "lon": -122.26},
            {"mark_key": "2", "mark_name": "Leeward", "lat": 37.79, "lon": -122.28},
        ],
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/course-overlay")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == race_id
    assert len(data["marks"]) == 2
    assert {m["name"] for m in data["marks"]} == {"Windward", "Leeward"}
    assert data["start_line"] is None
    assert data["finish_line"] is None


@pytest.mark.asyncio
async def test_api_state_tolerates_date_only_start_utc(storage: Storage) -> None:
    """Regression for #532: /api/state must return 200 even when a race row
    on today's date has a date-only (naive, no time) start_utc value left
    behind by the imported-results path."""
    db = storage._conn()
    from helmlog.races import local_today

    today = local_today().isoformat()
    await db.execute(
        "INSERT INTO races"
        " (name, event, race_num, date, start_utc, end_utc, session_type)"
        " VALUES (?, 'Flying Sails', 1, ?, ?, '', 'race')",
        (f"Imported-{today}", today, today),
    )
    await db.commit()

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/state")

    assert resp.status_code == 200
    data = resp.json()
    # Imported row with only a date must not be reported as current.
    assert data["current_race"] is None
    # But it should still appear in today_races with a valid ISO timestamp.
    assert len(data["today_races"]) == 1
    assert "T" in data["today_races"][0]["start_utc"]


# ---------------------------------------------------------------------------
# Session video overlays (#639) — Gauges + Track toggle buttons on the
# session page video, overlays active only during maneuver windows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_page_has_video_overlay_buttons(storage: Storage) -> None:
    """Session page exposes Gauges and Track toggle buttons for the video (#639)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.get(f"/session/{race_id}")
    assert resp.status_code == 200
    assert 'id="video-gauges-btn"' in resp.text
    assert 'id="video-track-btn"' in resp.text
    assert "toggleVideoGauges" in resp.text
    assert "toggleVideoTrack" in resp.text


# ---------------------------------------------------------------------------
# Session deep-link via ?t= (#642) — the share button + URL parser need to
# be present in the rendered page. Dynamic seek behaviour is JS-only and is
# covered by manual verification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_page_has_share_button(storage: Storage) -> None:
    """Session page renders a Share button wired to shareVideoLink() (#642)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=True
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.get(f"/session/{race_id}")
    assert resp.status_code == 200
    assert 'id="video-share-btn"' in resp.text
    assert "shareVideoLink" in resp.text


@pytest.mark.asyncio
async def test_session_js_has_deeplink_parser(storage: Storage) -> None:
    """session.js ships the ?t= parser + share formatter (#642).

    The parser itself is JS-only, but we can at least confirm the bundled
    file contains the hook points so a refactor that accidentally strips
    them will fail loudly here.
    """
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/static/session.js")
    assert resp.status_code == 200
    assert "_parseDeepLinkTime" in resp.text
    assert "_applyDeepLink" in resp.text
    assert "_fmtDeepLinkTime" in resp.text


# ---------------------------------------------------------------------------
# Maneuver compare
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_page_returns_200(storage: Storage) -> None:
    """GET /session/{id}/compare returns 200 for a valid session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.get(f"/session/{race_id}/compare")
    assert resp.status_code == 200
    assert "Compare Maneuvers" in resp.text


@pytest.mark.asyncio
async def test_compare_page_returns_404_unknown(storage: Storage) -> None:
    """GET /session/{id}/compare returns 404 for an unknown session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/session/99999/compare")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_compare_api_returns_maneuvers(storage: Storage) -> None:
    """GET /api/sessions/{id}/maneuvers/compare returns filtered maneuvers."""
    from helmlog.maneuver_detector import Maneuver

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]

    # Seed maneuvers directly via storage
    m1 = Maneuver(
        type="tack",
        ts=datetime(2026, 2, 26, 14, 10, 0, tzinfo=UTC),
        end_ts=datetime(2026, 2, 26, 14, 10, 8, tzinfo=UTC),
        duration_sec=8.0,
        loss_kts=0.5,
        vmg_loss_kts=None,
        tws_bin=12,
        twa_bin=40,
    )
    m2 = Maneuver(
        type="tack",
        ts=datetime(2026, 2, 26, 14, 15, 0, tzinfo=UTC),
        end_ts=datetime(2026, 2, 26, 14, 15, 10, tzinfo=UTC),
        duration_sec=10.0,
        loss_kts=0.8,
        vmg_loss_kts=None,
        tws_bin=12,
        twa_bin=42,
    )
    await storage.write_maneuvers(race_id, [m1, m2])

    # Look up actual IDs
    maneuvers = await storage.get_session_maneuvers(race_id)
    id1, id2 = maneuvers[0]["id"], maneuvers[1]["id"]

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/maneuvers/compare?ids={id1},{id2}")
    assert resp.status_code == 200
    data = resp.json()
    assert "maneuvers" in data
    assert "video_sync" in data
    assert len(data["maneuvers"]) == 2


@pytest.mark.asyncio
async def test_compare_api_invalid_ids(storage: Storage) -> None:
    """GET /api/sessions/{id}/maneuvers/compare returns 422 for invalid ids."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.get(f"/api/sessions/{race_id}/maneuvers/compare?ids=abc")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_compare_api_empty_ids(storage: Storage) -> None:
    """GET /api/sessions/{id}/maneuvers/compare returns 422 for empty ids."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.get(f"/api/sessions/{race_id}/maneuvers/compare?ids=")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Cross-session maneuver compare + browser (#584)
# ---------------------------------------------------------------------------


async def _seed_maneuvers(
    storage: Storage, client: httpx.AsyncClient, *, event: str, count: int = 2
) -> tuple[int, list[int]]:
    """Create a race and seed ``count`` tacks; return (race_id, [maneuver_ids])."""
    from helmlog.maneuver_detector import Maneuver

    await _set_event(client, event)
    race_id = (await client.post("/api/races/start")).json()["id"]
    maneuvers = [
        Maneuver(
            type="tack",
            ts=datetime(2026, 2, 26, 14, 10 + i, 0, tzinfo=UTC),
            end_ts=datetime(2026, 2, 26, 14, 10 + i, 8, tzinfo=UTC),
            duration_sec=8.0,
            loss_kts=0.5,
            vmg_loss_kts=None,
            tws_bin=12,
            twa_bin=40,
        )
        for i in range(count)
    ]
    await storage.write_maneuvers(race_id, maneuvers)
    rows = await storage.get_session_maneuvers(race_id)
    return race_id, [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_cross_session_compare_api_parses_pairs(storage: Storage) -> None:
    """GET /api/maneuvers/compare?ids=sid:mid returns maneuvers keyed by session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, mids = await _seed_maneuvers(storage, client, event="CrossA", count=2)
        ids = f"{race_id}:{mids[0]},{race_id}:{mids[1]}"
        resp = await client.get(f"/api/maneuvers/compare?ids={ids}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["maneuvers"]) == 2
    assert str(race_id) in data["video_sync_by_session"]
    # Each maneuver must carry its session context for cross-session rendering.
    for m in data["maneuvers"]:
        assert m["session_id"] == race_id
        assert m.get("session_name")


@pytest.mark.asyncio
async def test_cross_session_compare_api_mixes_sessions(storage: Storage) -> None:
    """Pairs from two different sessions return a per-session video_sync map."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1, m1_ids = await _seed_maneuvers(storage, client, event="MixA", count=1)
        # End the first race before starting the second to avoid overlap.
        await client.post("/api/races/stop")
        r2, m2_ids = await _seed_maneuvers(storage, client, event="MixB", count=1)
        ids = f"{r1}:{m1_ids[0]},{r2}:{m2_ids[0]}"
        resp = await client.get(f"/api/maneuvers/compare?ids={ids}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["maneuvers"]) == 2
    sessions = {m["session_id"] for m in data["maneuvers"]}
    assert sessions == {r1, r2}


@pytest.mark.asyncio
async def test_cross_session_compare_api_rejects_bare_ids(storage: Storage) -> None:
    """Legacy comma-separated integer ids must fail on the cross-session endpoint."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/compare?ids=1,2,3")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cross_session_compare_api_rejects_malformed(storage: Storage) -> None:
    """Malformed id pairs (missing half, non-integer) return 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for bad in ("foo:bar", "1:", ":1", "abc"):
            resp = await client.get(f"/api/maneuvers/compare?ids={bad}")
            assert resp.status_code == 422, f"{bad} should be rejected"


@pytest.mark.asyncio
async def test_cross_session_compare_page_renders(storage: Storage) -> None:
    """GET /compare renders without a session path parameter."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/compare")
    assert resp.status_code == 200
    assert b"compare-grid" in resp.content


@pytest.mark.asyncio
async def test_maneuvers_browser_page_renders(storage: Storage) -> None:
    """GET /maneuvers renders the browser page."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/maneuvers")
    assert resp.status_code == 200
    assert b"mv-sessions" in resp.content


@pytest.mark.asyncio
async def test_maneuver_browse_sessions_endpoint(storage: Storage) -> None:
    """GET /api/maneuvers/sessions lists sessions with maneuver counts."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="BrowseA", count=3)
        resp = await client.get("/api/maneuvers/sessions")
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    match = [s for s in sessions if s["id"] == race_id]
    assert match, "seeded race must appear in sessions list"
    assert match[0]["maneuver_count"] == 3


@pytest.mark.asyncio
async def test_maneuver_browse_sessions_excludes_empty(storage: Storage) -> None:
    """Sessions with zero maneuvers (imported-results rows) are filtered out."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with_race, _ = await _seed_maneuvers(storage, client, event="HasMan", count=1)
        await client.post("/api/races/stop")
        await _set_event(client, "NoMan")
        empty_race = (await client.post("/api/races/start")).json()["id"]
        resp = await client.get("/api/maneuvers/sessions")
    assert resp.status_code == 200
    ids = {s["id"] for s in resp.json()["sessions"]}
    assert with_race in ids
    assert empty_race not in ids


@pytest.mark.asyncio
async def test_maneuver_browse_sessions_excludes_imported(storage: Storage) -> None:
    """Races imported from external results (source != 'live') are hidden even
    if they have maneuver rows (duplicated across classes in the same window)."""
    from helmlog.maneuver_detector import Maneuver

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        live_race, _ = await _seed_maneuvers(storage, client, event="Live", count=1)
        # Create a second race, tag it as imported (source != 'live'), and
        # attach a maneuver so only the source filter can exclude it.
        await client.post("/api/races/stop")
        await _set_event(client, "Imported")
        imported_race = (await client.post("/api/races/start")).json()["id"]
        await storage.write_maneuvers(
            imported_race,
            [
                Maneuver(
                    type="tack",
                    ts=datetime(2026, 2, 26, 15, 0, 0, tzinfo=UTC),
                    end_ts=datetime(2026, 2, 26, 15, 0, 8, tzinfo=UTC),
                    duration_sec=8.0,
                    loss_kts=0.5,
                    vmg_loss_kts=None,
                    tws_bin=12,
                    twa_bin=40,
                )
            ],
        )
        await storage._conn().execute(
            "UPDATE races SET source = 'clubspot' WHERE id = ?", (imported_race,)
        )
        await storage._conn().commit()
        resp = await client.get("/api/maneuvers/sessions")
    assert resp.status_code == 200
    ids = {s["id"] for s in resp.json()["sessions"]}
    assert live_race in ids
    assert imported_race not in ids


@pytest.mark.asyncio
async def test_maneuver_browse_regattas_endpoint(storage: Storage) -> None:
    """GET /api/maneuvers/regattas returns only regattas with linked sessions."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/regattas")
    assert resp.status_code == 200
    assert "regattas" in resp.json()


@pytest.mark.asyncio
async def test_maneuver_browse_by_session_ids(storage: Storage) -> None:
    """GET /api/maneuvers/browse?session_ids= returns only those sessions' maneuvers."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="BrowseB", count=2)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_ids"] == [race_id]
    # All returned maneuvers must belong to that session.
    for m in data["maneuvers"]:
        assert m["session_id"] == race_id


@pytest.mark.asyncio
async def test_maneuver_browse_filters_by_type(storage: Storage) -> None:
    """Type filter excludes non-matching maneuvers."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="BrowseC", count=2)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}&type=gybe")
    assert resp.status_code == 200
    assert resp.json()["maneuvers"] == []


@pytest.mark.asyncio
async def test_maneuver_browse_filters_by_wind(storage: Storage) -> None:
    """Wind-range filter drops maneuvers whose entry_tws is None or out of band."""
    # Seeded maneuvers have no instrument data, so entry_tws is None; any
    # explicit wind filter must exclude them.
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="BrowseD", count=2)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}&tws_min=8&tws_max=10")
    assert resp.status_code == 200
    assert resp.json()["maneuvers"] == []


@pytest.mark.asyncio
async def test_maneuver_browse_rejects_bad_type(storage: Storage) -> None:
    """Invalid type value returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/browse?type=turtle")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_maneuver_browse_rejects_bad_direction(storage: Storage) -> None:
    """Invalid direction value returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/browse?direction=X")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_maneuver_browse_rejects_bad_rank_by(storage: Storage) -> None:
    """Unknown rank_by metric returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/browse?rank_by=vibes")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_maneuver_browse_default_rank_by_in_response(
    storage: Storage,
) -> None:
    """Response carries rank_by so the client knows which metric is rendered."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="RankDefault", count=2)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}")
    assert resp.status_code == 200
    assert resp.json()["rank_by"] == "distance_loss_m"


@pytest.mark.asyncio
async def test_maneuver_browse_accepts_rank_by_alternative(
    storage: Storage,
) -> None:
    """A valid alternate metric round-trips and labels the response."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="RankAlt", count=2)
        resp = await client.get(
            f"/api/maneuvers/browse?session_ids={race_id}&rank_by=time_to_recover_s"
        )
    assert resp.status_code == 200
    assert resp.json()["rank_by"] == "time_to_recover_s"


@pytest.mark.asyncio
async def test_maneuver_browse_sessions_filters_by_session_type(storage: Storage) -> None:
    """session_type=race hides practice sessions from the picker (and vice versa)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="RaceOnly", count=1)
        # Tag a second seeded session as practice
        await client.post("/api/races/stop")
        practice_id, _ = await _seed_maneuvers(storage, client, event="PracOnly", count=1)
        await storage._conn().execute(
            "UPDATE races SET session_type = 'practice' WHERE id = ?", (practice_id,)
        )
        await storage._conn().commit()

        race_resp = await client.get("/api/maneuvers/sessions?session_type=race")
        prac_resp = await client.get("/api/maneuvers/sessions?session_type=practice")

    race_ids = {s["id"] for s in race_resp.json()["sessions"]}
    prac_ids = {s["id"] for s in prac_resp.json()["sessions"]}
    assert race_id in race_ids and practice_id not in race_ids
    assert practice_id in prac_ids and race_id not in prac_ids


@pytest.mark.asyncio
async def test_maneuver_browse_sessions_rejects_bad_session_type(storage: Storage) -> None:
    """Unknown session_type value returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/sessions?session_type=dinghy")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_maneuver_browse_tws_bands_multi(storage: Storage) -> None:
    """tws_bands accepts multiple non-contiguous bands as a logical OR."""
    # No instrument data is seeded, so entry_tws is None and any wind
    # filter excludes all maneuvers — exercising the code path is enough
    # to catch parse/filter regressions.
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="BandMulti", count=2)
        ok = await client.get(
            f"/api/maneuvers/browse?session_ids={race_id}&tws_bands=6-8,12-15,15-"
        )
    assert ok.status_code == 200
    assert ok.json()["maneuvers"] == []


@pytest.mark.asyncio
async def test_maneuver_browse_tws_bands_rejects_non_numeric(storage: Storage) -> None:
    """Non-numeric tws_bands token returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/browse?tws_bands=foo-bar")
    assert resp.status_code == 422


async def _attach_vakaros_gun(storage: Storage, race_id: int, gun_ts: datetime) -> None:
    """Give a race a matched Vakaros session + race_start event at ``gun_ts``."""
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO vakaros_sessions "
        "(source_hash, source_file, start_utc, end_utc, ingested_at) "
        "VALUES (?, 'test.csv', ?, ?, ?)",
        (
            f"hash-{race_id}",
            gun_ts.isoformat(),
            (gun_ts + timedelta(hours=1)).isoformat(),
            gun_ts.isoformat(),
        ),
    )
    vak_session_id = cur.lastrowid
    await db.execute(
        "INSERT INTO vakaros_race_events "
        "(session_id, event_type, ts, timer_value_s) VALUES (?, 'race_start', ?, 0)",
        (vak_session_id, gun_ts.isoformat()),
    )
    await db.execute(
        "UPDATE races SET vakaros_session_id = ?, start_utc = ?, end_utc = ? WHERE id = ?",
        (
            vak_session_id,
            (gun_ts - timedelta(minutes=10)).isoformat(),
            (gun_ts + timedelta(hours=1)).isoformat(),
            race_id,
        ),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_maneuver_browse_synthesizes_start(storage: Storage) -> None:
    """A session with a Vakaros race_start event gets a synthetic start entry."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="Start", count=1)
        gun_ts = datetime(2026, 2, 26, 14, 20, 0, tzinfo=UTC)
        await _attach_vakaros_gun(storage, race_id, gun_ts)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}")
    assert resp.status_code == 200
    data = resp.json()
    starts = [m for m in data["maneuvers"] if m["type"] == "start"]
    assert len(starts) == 1
    assert starts[0]["session_id"] == race_id
    assert starts[0]["id"] == "S"
    assert starts[0]["ts"].startswith("2026-02-26T14:20:00")


@pytest.mark.asyncio
async def test_maneuver_browse_start_filter_excludes_others(storage: Storage) -> None:
    """type=start returns only synthesized starts; real maneuvers are dropped."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="StartOnly", count=2)
        gun_ts = datetime(2026, 2, 26, 14, 20, 0, tzinfo=UTC)
        await _attach_vakaros_gun(storage, race_id, gun_ts)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}&type=start")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["maneuvers"]) == 1
    assert data["maneuvers"][0]["type"] == "start"


@pytest.mark.asyncio
async def test_maneuver_browse_no_start_without_vakaros_gun(storage: Storage) -> None:
    """Sessions without a Vakaros race_start event get no synthesized start."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="NoGun", count=1)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}")
    assert resp.status_code == 200
    data = resp.json()
    starts = [m for m in data["maneuvers"] if m["type"] == "start"]
    assert starts == []


@pytest.mark.asyncio
async def test_cross_session_compare_resolves_start_token(storage: Storage) -> None:
    """ids=<sid>:S resolves to a synthesized start on the compare endpoint."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="CmpStart", count=1)
        gun_ts = datetime(2026, 2, 26, 14, 20, 0, tzinfo=UTC)
        await _attach_vakaros_gun(storage, race_id, gun_ts)
        resp = await client.get(f"/api/maneuvers/compare?ids={race_id}:S")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["maneuvers"]) == 1
    assert data["maneuvers"][0]["type"] == "start"
    assert data["maneuvers"][0]["id"] == "S"


@pytest.mark.asyncio
async def test_maneuver_browse_tags_rounding_mark(storage: Storage) -> None:
    """Each rounding gets a mark=weather|leeward field classified by exit_twa."""
    # Without instrument data, enrichment produces entry_twa/exit_twa = None,
    # so _classify_rounding_mark returns None. We patch exit_twa directly on
    # the enriched payload via a monkey-patched maneuver — easier to just
    # verify the classifier directly.
    from helmlog.routes.sessions import _classify_rounding_mark

    assert _classify_rounding_mark({"type": "rounding", "exit_twa": 120}) == "weather"
    assert _classify_rounding_mark({"type": "rounding", "exit_twa": 40}) == "leeward"
    assert _classify_rounding_mark({"type": "rounding", "entry_twa": 40}) == "weather"
    assert _classify_rounding_mark({"type": "rounding", "entry_twa": 120}) == "leeward"
    assert _classify_rounding_mark({"type": "rounding"}) is None
    assert _classify_rounding_mark({"type": "tack", "exit_twa": 40}) is None


@pytest.mark.asyncio
async def test_maneuver_browse_filter_weather_implies_rounding(storage: Storage) -> None:
    """type=weather only returns roundings (tacks/gybes are excluded)."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="Marks", count=2)
        resp = await client.get(f"/api/maneuvers/browse?session_ids={race_id}&type=weather")
    assert resp.status_code == 200
    # Seeded maneuvers are tacks — none should pass a weather-mark filter.
    assert resp.json()["maneuvers"] == []


@pytest.mark.asyncio
async def test_maneuver_browse_rejects_bad_mark_type(storage: Storage) -> None:
    """Unknown type token (not tack/gybe/rounding/weather/leeward/start) → 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/maneuvers/browse?type=windward")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_maneuver_browse_post_start_filter(storage: Storage) -> None:
    """post_start=1 drops maneuvers whose ts is before the session's start_utc.

    With no Vakaros race_start event, the server falls back to the race's
    stored start_utc as the effective gun. The seeded tacks use
    ts = 14:10, 14:11 UTC; start_utc is set to 14:30 via _END_UTC below
    so both maneuvers land pre-start.
    """
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id, _ = await _seed_maneuvers(storage, client, event="PostStart", count=2)
        # Push the race start forward so the seeded maneuvers (ts 14:10,14:11)
        # are firmly before the "gun" (14:30 Z).
        await storage._conn().execute(
            "UPDATE races SET start_utc = ? WHERE id = ?",
            ("2026-02-26T14:30:00+00:00", race_id),
        )
        await storage._conn().commit()

        without_filter = await client.get(f"/api/maneuvers/browse?session_ids={race_id}")
        with_filter = await client.get(f"/api/maneuvers/browse?session_ids={race_id}&post_start=1")
    assert without_filter.status_code == 200
    assert with_filter.status_code == 200
    assert len(without_filter.json()["maneuvers"]) == 2
    assert with_filter.json()["maneuvers"] == []
