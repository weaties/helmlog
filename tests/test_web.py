"""Tests for src/logger/web.py — race API and audio integration."""

from __future__ import annotations

from datetime import UTC, datetime
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
    """Completed debriefs appear as separate 'debrief' rows in /api/sessions."""
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

        resp_all = await client.get("/api/sessions")
        resp_debrief = await client.get("/api/sessions?type=debrief")

    all_data = resp_all.json()
    types = [s["type"] for s in all_data["sessions"]]
    assert "debrief" in types

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
# Session notes API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_note_returns_201(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes creates a note and returns 201."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": "Upwind leg, 15kts TWS", "note_type": "text"},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "ts" in data


@pytest.mark.asyncio
async def test_create_note_blank_body_returns_422(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes with a blank body returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": "   ", "note_type": "text"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_note_unknown_session_returns_404(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes for a non-existent session returns 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/sessions/9999/notes",
            json={"body": "note", "note_type": "text"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_notes_returns_notes(storage: Storage) -> None:
    """GET /api/sessions/{id}/notes returns notes for the session."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/sessions/{race_id}/notes", json={"body": "Note one"})
        await client.post(f"/api/sessions/{race_id}/notes", json={"body": "Note two"})
        resp = await client.get(f"/api/sessions/{race_id}/notes")
    assert resp.status_code == 200
    notes = resp.json()
    assert len(notes) == 2
    bodies = [n["body"] for n in notes]
    assert "Note one" in bodies
    assert "Note two" in bodies


@pytest.mark.asyncio
async def test_delete_note_returns_204(storage: Storage) -> None:
    """DELETE /api/notes/{id} returns 204 and the note is gone."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        create_resp = await client.post(
            f"/api/sessions/{race_id}/notes", json={"body": "To delete"}
        )
        note_id = create_resp.json()["id"]
        del_resp = await client.delete(f"/api/notes/{note_id}")
        list_resp = await client.get(f"/api/sessions/{race_id}/notes")
    assert del_resp.status_code == 204
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_delete_note_not_found_returns_404(storage: Storage) -> None:
    """DELETE /api/notes/{id} for a missing note returns 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/notes/99999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_grafana_annotations_returns_list(storage: Storage) -> None:
    """GET /api/grafana/annotations returns annotation objects."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/sessions/{race_id}/notes", json={"body": "Tack at mark"})
        from_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
        to_ms = int(datetime(2026, 12, 31, tzinfo=UTC).timestamp() * 1000)
        resp = await client.get(f"/api/grafana/annotations?from={from_ms}&to={to_ms}")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "time" in data[0]
    assert "timeEnd" in data[0]
    assert "title" in data[0]
    assert "text" in data[0]
    assert "tags" in data[0]
    assert data[0]["text"] == "Tack at mark"
    assert data[0]["title"] == "Text"
    assert data[0]["tags"] == ["text"]


@pytest.mark.asyncio
async def test_grafana_annotations_empty_when_no_params(storage: Storage) -> None:
    """Missing from/to returns empty list."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/grafana/annotations")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_grafana_annotations_session_filter(storage: Storage) -> None:
    """sessionId param scopes results to a single race."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        r1 = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/sessions/{r1}/notes", json={"body": "Race 1 note"})
        await client.post("/api/races/end")
        r2 = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/sessions/{r2}/notes", json={"body": "Race 2 note"})
        from_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
        to_ms = int(datetime(2026, 12, 31, tzinfo=UTC).timestamp() * 1000)
        resp_all = await client.get(f"/api/grafana/annotations?from={from_ms}&to={to_ms}")
        resp_r1 = await client.get(
            f"/api/grafana/annotations?from={from_ms}&to={to_ms}&sessionId={r1}"
        )
        resp_r2 = await client.get(
            f"/api/grafana/annotations?from={from_ms}&to={to_ms}&sessionId={r2}"
        )
    assert len(resp_all.json()) >= 2
    r1_notes = resp_r1.json()
    r2_notes = resp_r2.json()
    assert len(r1_notes) == 1
    assert r1_notes[0]["text"] == "Race 1 note"
    assert len(r2_notes) == 1
    assert r2_notes[0]["text"] == "Race 2 note"


@pytest.mark.asyncio
async def test_grafana_annotations_out_of_range_returns_empty(storage: Storage) -> None:
    """Time range that excludes the note returns empty list."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        await client.post(f"/api/sessions/{race_id}/notes", json={"body": "Some note"})
        # Query a window in 2020 — before any test data
        from_ms = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000)
        to_ms = int(datetime(2020, 12, 31, tzinfo=UTC).timestamp() * 1000)
        resp = await client.get(f"/api/grafana/annotations?from={from_ms}&to={to_ms}")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_grafana_annotations_photo_note_includes_img_tag(storage: Storage) -> None:
    """Photo notes include an <img> tag pointing at the photo URL."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        # Insert a photo note directly via storage to set photo_path without
        # needing a real file upload.
        await storage.create_note(
            datetime.now(UTC).isoformat(),
            "Caption text",
            race_id=race_id,
            note_type="photo",
            photo_path=f"{race_id}/test.jpg",
        )
        from_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
        to_ms = int(datetime(2026, 12, 31, tzinfo=UTC).timestamp() * 1000)
        resp = await client.get(f"/api/grafana/annotations?from={from_ms}&to={to_ms}")
    assert resp.status_code == 200
    data = resp.json()
    photo_annotations = [a for a in data if "photo" in a["tags"]]
    assert len(photo_annotations) == 1
    ann = photo_annotations[0]
    assert f"/notes/{race_id}/test.jpg" in ann["text"]
    assert "<img" in ann["text"]
    assert "Caption text" in ann["text"]


# ---------------------------------------------------------------------------
# Phase 2 notes: settings, photo, serve, traversal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_settings_note_returns_201(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes with note_type='settings' and valid JSON body returns 201."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": '{"TWS": "15", "TWD": "220"}', "note_type": "settings"},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "ts" in data


@pytest.mark.asyncio
async def test_create_settings_note_invalid_json_returns_422(storage: Storage) -> None:
    """POST /api/sessions/{id}/notes with note_type='settings' and non-JSON body returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes",
            json={"body": "not valid json", "note_type": "settings"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_photo_note_returns_201(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/sessions/{id}/notes/photo with a file creates a photo note and saves file."""
    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("NOTES_DIR", str(notes_dir))
    app = create_app(storage)
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        race_id = (await client.post("/api/races/start")).json()["id"]
        resp = await client.post(
            f"/api/sessions/{race_id}/notes/photo",
            files={"file": ("test.jpg", jpeg_bytes, "image/jpeg")},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert "photo_path" in data
    # File should exist on disk
    assert (notes_dir / data["photo_path"]).exists()


@pytest.mark.asyncio
async def test_serve_note_photo_returns_200(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /notes/{path} serves a file that exists in NOTES_DIR."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    photo_file = notes_dir / "test.jpg"
    photo_file.write_bytes(b"\xff\xd8\xff\xe0test")

    monkeypatch.setenv("NOTES_DIR", str(notes_dir))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/notes/test.jpg")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_serve_note_photo_traversal_blocked(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path traversal via URL-encoded dots is blocked with 403 (not served)."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    monkeypatch.setenv("NOTES_DIR", str(notes_dir))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Use URL-encoded dots to bypass client-side normalization
        resp = await client.get("/notes/%2e%2e/%2e%2e/etc/passwd")
    # Path traversal must not return 200 — either 403 (blocked) or 404 (not found)
    assert resp.status_code in (403, 404)


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

        resp = await client.get("/api/sessions")

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
async def test_serve_note_photo_cache_headers(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /notes/{path} returns Cache-Control and ETag headers."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8")  # minimal JPEG stub
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/notes/photo.jpg")
    assert resp.status_code == 200
    assert "max-age=31536000" in resp.headers.get("cache-control", "")
    assert resp.headers.get("etag", "") != ""


@pytest.mark.asyncio
async def test_serve_note_photo_304_if_none_match(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /notes/{path} returns 304 when ETag matches If-None-Match header."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/notes/photo.jpg")
        etag = first.headers["etag"]
        second = await client.get("/notes/photo.jpg", headers={"If-None-Match": etag})
    assert second.status_code == 304


@pytest.mark.asyncio
async def test_serve_note_photo_403_traversal(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /notes/{path} with percent-encoded path traversal returns 403.

    Standard HTTP clients normalize literal '..' segments in URLs, so the
    traversal must be encoded as %2e%2e to reach the handler.
    """
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    monkeypatch.setenv("NOTES_DIR", str(notes_dir))
    # Create a "secret" file one level above notes_dir
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # %2e%2e is URL-encoded '..'; httpx won't normalize it as a path segment
        resp = await client.get("/notes/%2e%2e/secret.txt")
    assert resp.status_code in {403, 404}  # 403 if handler catches it; 404 if server normalizes


@pytest.mark.asyncio
async def test_serve_note_photo_404(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /notes/{path} returns 404 when the file does not exist."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path))
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/notes/nonexistent.jpg")
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
    with patch("helmlog.transcribe._run_whisper", return_value="Hello world"):
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
    assert set(segs[0].keys()) == {"start", "end", "speaker", "text"}
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
# Threaded comments (#282)
# ---------------------------------------------------------------------------


async def _make_race_for_comments(client: httpx.AsyncClient) -> int:
    """Set an event and start a race, return race_id."""
    await client.post("/api/event", json={"event_name": "CommentTest"})
    resp = await client.post("/api/races/start")
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_create_thread_and_list(storage: Storage) -> None:
    """POST creates a thread; GET lists it with unread count."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "Bad tack at weather mark", "mark_reference": "weather_mark_1"},
        )
        assert resp.status_code == 201
        thread_id = resp.json()["id"]

        resp = await client.get(f"/api/sessions/{race_id}/threads")
        assert resp.status_code == 200
        threads = resp.json()
        assert len(threads) == 1
        assert threads[0]["id"] == thread_id
        assert threads[0]["title"] == "Bad tack at weather mark"
        assert threads[0]["mark_reference"] == "weather_mark_1"
        assert threads[0]["comment_count"] == 0
        assert threads[0]["unread_count"] == 0


@pytest.mark.asyncio
async def test_create_thread_invalid_mark_reference(storage: Storage) -> None:
    """POST with unknown mark reference returns 400."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"mark_reference": "bogus_mark"},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_thread_general_discussion(storage: Storage) -> None:
    """A thread with no anchor covers the whole race."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "General discussion"},
        )
        assert resp.status_code == 201
        thread_id = resp.json()["id"]

        resp = await client.get(f"/api/threads/{thread_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["anchor_timestamp"] is None
        assert data["mark_reference"] is None


@pytest.mark.asyncio
async def test_get_thread_not_found(storage: Storage) -> None:
    """GET /api/threads/999 returns 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/threads/999")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_and_list_comments(storage: Storage) -> None:
    """POST adds comments; GET thread includes them."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "Lane choice"},
        )
        thread_id = resp.json()["id"]

        resp = await client.post(
            f"/api/threads/{thread_id}/comments",
            json={"body": "We should have gone left"},
        )
        assert resp.status_code == 201
        assert resp.json()["id"]

        resp = await client.post(
            f"/api/threads/{thread_id}/comments",
            json={"body": "Agreed, the pressure was better"},
        )
        assert resp.status_code == 201

        resp = await client.get(f"/api/threads/{thread_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["comments"]) == 2
        assert data["comments"][0]["body"] == "We should have gone left"


@pytest.mark.asyncio
async def test_create_comment_empty_body(storage: Storage) -> None:
    """POST with blank body returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "test"},
        )
        thread_id = resp.json()["id"]
        resp = await client.post(
            f"/api/threads/{thread_id}/comments",
            json={"body": "  "},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_comment(storage: Storage) -> None:
    """PUT edits a comment body and sets edited_at."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "test"},
        )
        thread_id = resp.json()["id"]
        resp = await client.post(
            f"/api/threads/{thread_id}/comments",
            json={"body": "original"},
        )
        comment_id = resp.json()["id"]

        resp = await client.put(
            f"/api/comments/{comment_id}",
            json={"body": "edited"},
        )
        assert resp.status_code == 200

        resp = await client.get(f"/api/threads/{thread_id}")
        comments = resp.json()["comments"]
        assert comments[0]["body"] == "edited"
        assert comments[0]["edited_at"] is not None


@pytest.mark.asyncio
async def test_delete_comment(storage: Storage) -> None:
    """DELETE removes a comment."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "test"},
        )
        thread_id = resp.json()["id"]
        resp = await client.post(
            f"/api/threads/{thread_id}/comments",
            json={"body": "delete me"},
        )
        comment_id = resp.json()["id"]

        resp = await client.delete(f"/api/comments/{comment_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/api/threads/{thread_id}")
        assert len(resp.json()["comments"]) == 0


@pytest.mark.asyncio
async def test_resolve_and_unresolve_thread(storage: Storage) -> None:
    """Resolve and unresolve a thread."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "resolve me"},
        )
        thread_id = resp.json()["id"]

        resp = await client.post(
            f"/api/threads/{thread_id}/resolve",
            json={"resolution_summary": "We agreed to go left next time"},
        )
        assert resp.status_code == 200

        resp = await client.get(f"/api/threads/{thread_id}")
        data = resp.json()
        assert data["resolved"] == 1
        assert data["resolution_summary"] == "We agreed to go left next time"
        assert data["resolved_at"] is not None

        resp = await client.post(
            f"/api/threads/{thread_id}/unresolve",
            json={},
        )
        assert resp.status_code == 200

        resp = await client.get(f"/api/threads/{thread_id}")
        data = resp.json()
        assert data["resolved"] == 0
        assert data["resolution_summary"] is None


@pytest.mark.asyncio
async def test_mark_thread_read(storage: Storage) -> None:
    """POST /api/threads/{id}/read succeeds."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "read test"},
        )
        thread_id = resp.json()["id"]

        resp = await client.post(f"/api/threads/{thread_id}/read")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unread_count_with_real_user(storage: Storage) -> None:
    """Unread tracking works when there is a real user_id (not mock admin)."""
    # Directly test the storage layer with a real user_id
    db = storage._conn()
    await db.execute(
        "INSERT INTO users (id, email, name, role, is_developer, is_active, created_at)"
        " VALUES (1, 'helm@boat.test', 'Helm', 'crew', 0, 1, '2026-01-01T00:00:00Z')",
    )
    await db.commit()

    # Create a race
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc, session_type)"
        " VALUES (1, 'Test-1', 'Test', 1, '2026-03-12', '2026-03-12T14:00:00Z', 'race')",
    )
    await db.commit()

    thread_id = await storage.create_comment_thread(1, 1, title="test")
    await storage.create_comment(thread_id, 1, "first message")

    # Before marking read — should have 1 unread
    threads = await storage.list_comment_threads(1, 1)
    assert threads[0]["unread_count"] == 1

    # Mark as read
    await storage.mark_thread_read(thread_id, 1)

    # Should have 0 unread now
    threads = await storage.list_comment_threads(1, 1)
    assert threads[0]["unread_count"] == 0

    # Add another comment — should be unread again
    await storage.create_comment(thread_id, 1, "second message")
    threads = await storage.list_comment_threads(1, 1)
    assert threads[0]["unread_count"] == 1


@pytest.mark.asyncio
async def test_delete_thread_cascades(storage: Storage) -> None:
    """DELETE thread removes thread and all comments."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "delete me"},
        )
        thread_id = resp.json()["id"]
        await client.post(
            f"/api/threads/{thread_id}/comments",
            json={"body": "child"},
        )

        resp = await client.delete(f"/api/threads/{thread_id}")
        assert resp.status_code == 204

        resp = await client.get(f"/api/threads/{thread_id}")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_redact_comment_author(storage: Storage) -> None:
    """Redacting replaces author with NULL (storage-layer test with real user)."""
    db = storage._conn()
    await db.execute(
        "INSERT INTO users (id, email, name, role, is_developer, is_active, created_at)"
        " VALUES (1, 'crew@boat.test', 'Crew', 'crew', 0, 1, '2026-01-01T00:00:00Z')",
    )
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc, session_type)"
        " VALUES (1, 'Test-1', 'Test', 1, '2026-03-12', '2026-03-12T14:00:00Z', 'race')",
    )
    await db.commit()

    thread_id = await storage.create_comment_thread(1, 1, title="redact test")
    await storage.create_comment(thread_id, 1, "my comment")

    count = await storage.redact_comment_author(1)
    assert count == 1

    thread = await storage.get_comment_thread(thread_id)
    assert thread is not None
    assert thread["comments"][0]["author"] is None


@pytest.mark.asyncio
async def test_redact_comment_author_api(storage: Storage) -> None:
    """POST /api/comments/redact-author returns 200."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/comments/redact-author", json={})
        assert resp.status_code == 200
        assert "redacted" in resp.json()


@pytest.mark.asyncio
async def test_thread_with_anchor_timestamp(storage: Storage) -> None:
    """Thread can be anchored to a specific timestamp."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        race_id = await _make_race_for_comments(client)
        ts = "2026-03-12T14:05:30Z"
        resp = await client.post(
            f"/api/sessions/{race_id}/threads",
            json={"title": "At this moment", "anchor_timestamp": ts},
        )
        assert resp.status_code == 201
        thread_id = resp.json()["id"]

        resp = await client.get(f"/api/threads/{thread_id}")
        assert resp.json()["anchor_timestamp"] == ts
