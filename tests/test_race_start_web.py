"""Web tests for race-start routes.

Covers:
- /race-start page rendering (viewer)
- /api/race-start/state happy path + auth gating
- new timer endpoints: start, stop, sync, set-duration, timer-reset, rolling-timer
- ping endpoints + line metrics
- viewer is blocked from mutations (403)
- viewer can read state (200)
- simrad inbound timer-event
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.auth import generate_token, session_expires_at
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_viewer(storage: Storage) -> str:
    user_id = await storage.create_user("viewer-rs@test.com", "Viewer", "viewer")
    sid = generate_token()
    await storage.create_session(sid, user_id, session_expires_at())
    return sid


async def _create_crew(storage: Storage) -> str:
    user_id = await storage.create_user("crew-rs@test.com", "Crew", "crew")
    sid = generate_token()
    await storage.create_session(sid, user_id, session_expires_at())
    return sid


# ---------------------------------------------------------------------------
# Page / state reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_renders(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/race-start")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Instrument Timer" in resp.text


@pytest.mark.asyncio
async def test_state_returns_snapshot_initially(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/race-start/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["race_id"] is None
    assert body["start_line"]["is_complete"] is False
    assert body["scheduled_start"] is None
    assert body["simrad_timer"]["is_running"] is False


@pytest.mark.asyncio
async def test_state_surfaces_scheduled_start(storage: Storage) -> None:
    fire_at = datetime.now(UTC) + timedelta(hours=22)
    await storage.schedule_start(fire_at, event="R2TS", session_type="race")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/race-start/state")
    body = r.json()
    sched = body["scheduled_start"]
    assert sched is not None
    assert sched["event"] == "R2TS"
    assert sched["session_type"] == "race"
    assert sched["seconds_until_start"] > 22 * 3600 - 30


# ---------------------------------------------------------------------------
# Timer mutations — start / stop / set-duration / sync / timer-reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_creates_race_and_runs_timer(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/race-start/start")
    assert r.status_code == 200
    body = r.json()
    assert body["race_id"] is not None
    assert body["simrad_timer"]["is_running"] is True
    assert body["simrad_timer"]["t0_utc"] is not None


@pytest.mark.asyncio
async def test_start_closes_open_race_before_opening_new(storage: Storage) -> None:
    now = datetime.now(UTC)
    await storage.start_race("Old", now, now.date().isoformat(), 1, "old-race")
    old = await storage.get_current_race()
    assert old is not None

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/race-start/start")
    assert r.status_code == 200
    new_race_id = r.json()["race_id"]
    assert new_race_id != old.id


@pytest.mark.asyncio
async def test_stop_stops_timer_and_ends_race(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/race-start/start")
        r = await client.post("/api/race-start/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["simrad_timer"]["is_running"] is False
    assert body["race_id"] is None


@pytest.mark.asyncio
async def test_set_duration_updates_timer(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/race-start/set-duration", json={"duration_s": 180})
    assert r.status_code == 200
    body = r.json()
    assert body["simrad_timer"]["duration_s"] == 180
    assert body["simrad_timer"]["is_running"] is False


@pytest.mark.asyncio
async def test_set_duration_rejected_while_running(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/race-start/start")
        r = await client.post("/api/race-start/set-duration", json={"duration_s": 180})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_sync_snaps_to_nearest_minute(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Set 5-minute duration and start.
        await client.post("/api/race-start/set-duration", json={"duration_s": 300})
        await client.post("/api/race-start/start")
        r = await client.post("/api/race-start/sync")
    assert r.status_code == 200
    body = r.json()
    # t0_utc should now be a whole minute boundary.
    t0 = datetime.fromisoformat(body["simrad_timer"]["t0_utc"])
    assert t0.second == 0


@pytest.mark.asyncio
async def test_timer_reset_restores_duration(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/race-start/set-duration", json={"duration_s": 240})
        r = await client.post("/api/race-start/timer-reset")
    assert r.status_code == 200
    body = r.json()
    assert body["simrad_timer"]["duration_s"] == 240
    assert body["simrad_timer"]["is_running"] is False


@pytest.mark.asyncio
async def test_rolling_timer_toggle(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/race-start/rolling-timer", json={"on": True})
    assert r.status_code == 200
    row = await storage.get_simrad_timer_state()
    assert row is not None
    assert row["rolling_timer_on"] is True


# ---------------------------------------------------------------------------
# Line pings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_boat_then_pin_completes_line(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/race-start/ping/boat",
            json={"latitude_deg": 47.6500, "longitude_deg": -122.4000},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["start_line"]["boat_end_lat"] == 47.6500
        assert body["start_line"]["is_complete"] is False

        r = await client.post(
            "/api/race-start/ping/pin",
            json={"latitude_deg": 47.6510, "longitude_deg": -122.4000},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["start_line"]["pin_end_lat"] == 47.6510
        assert body["start_line"]["is_complete"] is True


async def _insert_position(storage: Storage, lat: float, lon: float) -> None:
    db = storage._conn()  # noqa: SLF001
    await db.execute(
        "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg) VALUES (?, ?, ?, ?)",
        (datetime.now(UTC).isoformat(), 0, lat, lon),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_ping_with_no_body_uses_latest_position(storage: Storage) -> None:
    await _insert_position(storage, 47.6499, -122.3998)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/race-start/ping/boat", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["start_line"]["boat_end_lat"] == pytest.approx(47.6499)
    assert body["start_line"]["boat_end_lon"] == pytest.approx(-122.3998)


@pytest.mark.asyncio
async def test_ping_no_position_no_body_returns_409(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/race-start/ping/boat", json={})
    assert r.status_code == 409
    assert "no GPS fix" in r.json()["detail"]


@pytest.mark.asyncio
async def test_ping_manual_override_wins_over_db(storage: Storage) -> None:
    await _insert_position(storage, 47.0, -122.0)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/race-start/ping/pin",
            json={"latitude_deg": 47.65, "longitude_deg": -122.40},
        )
    body = r.json()
    assert body["start_line"]["pin_end_lat"] == 47.65


@pytest.mark.asyncio
async def test_ping_invalid_coords_400(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/race-start/ping/boat",
            json={"latitude_deg": 999.0, "longitude_deg": 0.0},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_line_metrics_without_pings_returns_null(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/race-start/line-metrics",
            json={"boat_lat": 47.65, "boat_lon": -122.40, "sog_kn": 5.0},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is None


@pytest.mark.asyncio
async def test_line_metrics_with_complete_line(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/race-start/ping/boat",
            json={"latitude_deg": 47.65, "longitude_deg": -122.4000},
        )
        await client.post(
            "/api/race-start/ping/pin",
            json={"latitude_deg": 47.65, "longitude_deg": -122.3987},
        )
        r = await client.post(
            "/api/race-start/line-metrics",
            json={
                "boat_lat": 47.6505,
                "boat_lon": -122.40,
                "sog_kn": 5.0,
                "twd_deg": 0.0,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is not None
    assert body["metrics"]["line_length_m"] > 0


# ---------------------------------------------------------------------------
# Auth gating (AUTH_DISABLED=false)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_unauthenticated_is_401(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/state")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_viewer_can_read_state(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.get("/api/race-start/state")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_viewer_blocked_from_start(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.post("/api/race-start/start")
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_viewer_blocked_from_ping(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.post(
                "/api/race-start/ping/boat",
                json={"latitude_deg": 47.65, "longitude_deg": -122.40},
            )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_crew_can_start(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_crew(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.post("/api/race-start/start")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_viewer_template_shows_readonly_banner(storage: Storage) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        sid = await _create_viewer(storage)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.get("/race-start")
        assert r.status_code == 200
        assert "Viewer mode" in r.text
        assert "disabled" in r.text


# ---------------------------------------------------------------------------
# Start-line carry-over
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_snapshot_exposes_carry_over(storage: Storage) -> None:
    """Snapshot's start_line block surfaces carried_over_from_race_id per
    end so the UI can warn the helm to re-ping (#702)."""
    date = "2026-04-30"
    r1 = await storage.start_race("CYC", datetime.now(UTC), date, 1, "20260430-CYC-1")
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="boat",
        latitude_deg=47.6895,
        longitude_deg=-122.4160,
        captured_at=datetime.now(UTC),
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="pin",
        latitude_deg=47.6901,
        longitude_deg=-122.4189,
        captured_at=datetime.now(UTC),
        captured_by=None,
    )
    await storage.end_race(r1.id, datetime.now(UTC))
    await storage.start_race("CYC", datetime.now(UTC), date, 2, "20260430-CYC-2")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/race-start/state")
    body = r.json()
    sl = body["start_line"]
    assert sl["is_complete"] is True
    assert sl["boat_end_carried_over_from_race_id"] == r1.id
    assert sl["pin_end_carried_over_from_race_id"] == r1.id


@pytest.mark.asyncio
async def test_state_snapshot_no_carry_over_for_fresh_pings(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await storage.start_race("CYC", datetime.now(UTC), "2026-04-30", 1, "20260430-CYC-1")
        await client.post(
            "/api/race-start/ping/boat",
            json={"latitude_deg": 47.65, "longitude_deg": -122.40},
        )
        await client.post(
            "/api/race-start/ping/pin",
            json={"latitude_deg": 47.66, "longitude_deg": -122.41},
        )
        r = await client.get("/api/race-start/state")
    body = r.json()
    sl = body["start_line"]
    assert sl["is_complete"] is True
    assert sl["boat_end_carried_over_from_race_id"] is None
    assert sl["pin_end_carried_over_from_race_id"] is None


@pytest.mark.asyncio
async def test_timer_state_persists_across_clients(storage: Storage) -> None:
    """Starting via one client and reading via a fresh client sees the same state."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/race-start/set-duration", json={"duration_s": 240})
        await client.post("/api/race-start/start")

    app2 = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2), base_url="http://test"
    ) as client:
        r = await client.get("/api/race-start/state")
    body = r.json()
    assert body["simrad_timer"]["is_running"] is True
    assert body["simrad_timer"]["duration_s"] == 240


# ---------------------------------------------------------------------------
# Simrad inbound timer-event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simrad_stopped_ends_current_race(storage: Storage) -> None:
    """STOP from the B&G closes the current open race."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/internal/timer-event",
            json={"path": "racing.startTimer.duration", "value": 300, "ts": ts},
        )
        r = await client.post(
            "/api/internal/timer-event",
            json={"path": "racing.startTimer.state", "value": "running", "ts": ts},
        )
        assert r.status_code == 200
        assert await storage.get_current_race() is not None

        r = await client.post(
            "/api/internal/timer-event",
            json={"path": "racing.startTimer.state", "value": "stopped", "ts": ts},
        )
        assert r.status_code == 200

    assert await storage.get_current_race() is None


@pytest.mark.asyncio
async def test_simrad_stopped_with_no_race_is_noop(storage: Storage) -> None:
    """STOP with no open race does not error."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/internal/timer-event",
            json={"path": "racing.startTimer.duration", "value": 300, "ts": ts},
        )
        r = await client.post(
            "/api/internal/timer-event",
            json={"path": "racing.startTimer.state", "value": "stopped", "ts": ts},
        )
    assert r.status_code == 200
