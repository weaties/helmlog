"""Tests for the race-start simulator (#690).

Covers:
- Simulator routes 404 when RACE_START_SIMULATOR=false (kill switch).
- Simulator routes are gated on the developer flag — non-devs get 403,
  AUTH_DISABLED uses the mock-admin (which is is_developer=1).
- Setting the virtual clock offset shifts the FSM clock so flag transitions
  fire deterministically without waiting real time.
- Synthetic boat-state writes feed line-metrics and ping fallback.
- Scenario presets apply offset + boat state in one call.
- Prestart drill stamps line endpoints + walks the boat in the background.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sim_env() -> dict[str, str]:
    """Force the simulator on. Default is also on, but tests are explicit."""
    return {"RACE_START_SIMULATOR": "true"}


def _kill_switch_env() -> dict[str, str]:
    return {"RACE_START_SIMULATOR": "false"}


# ---------------------------------------------------------------------------
# Guard — kill switch + developer auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sim_routes_404_when_kill_switch(storage: Storage) -> None:
    """RACE_START_SIMULATOR=false turns the routes into 404s."""
    with patch.dict(os.environ, _kill_switch_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/sim/clock")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_sim_routes_default_on(storage: Storage) -> None:
    """Default (env unset) is enabled — no opt-in required for devs."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RACE_START_SIMULATOR", None)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/sim/clock")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_sim_routes_403_for_non_developer(storage: Storage) -> None:
    """A non-developer crew user is blocked from the simulator."""
    from helmlog.auth import generate_token, session_expires_at

    with patch.dict(os.environ, {**_sim_env(), "AUTH_DISABLED": "false"}):
        user_id = await storage.create_user(
            "crew@test.com", "Test Crew", "crew", is_developer=False
        )
        sid = generate_token()
        await storage.create_session(sid, user_id, session_expires_at())

        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.get("/api/race-start/sim/clock")
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_sim_routes_ok_for_developer(storage: Storage) -> None:
    """A user with is_developer=1 reaches the simulator."""
    from helmlog.auth import generate_token, session_expires_at

    with patch.dict(os.environ, {**_sim_env(), "AUTH_DISABLED": "false"}):
        user_id = await storage.create_user("dev@test.com", "Test Dev", "crew", is_developer=True)
        sid = generate_token()
        await storage.create_session(sid, user_id, session_expires_at())

        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": sid},
        ) as client:
            r = await client.get("/api/race-start/sim/clock")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_sim_routes_mounted_with_env(storage: Storage) -> None:
    """AUTH_DISABLED=true (mock admin, is_developer=1) hits the simulator."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/sim/clock")
        assert r.status_code == 200
        assert r.json()["offset_s"] == 0.0


# ---------------------------------------------------------------------------
# Virtual clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_offset_persists_on_app_state(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post("/api/race-start/sim/clock", json={"offset_s": -300.0})
            assert r.status_code == 200
            r = await client.get("/api/race-start/sim/clock")
        assert r.json()["offset_s"] == -300.0
        assert app.state.race_start_sim_offset_s == -300.0


@pytest.mark.asyncio
async def test_offset_skews_fsm_clock(storage: Storage) -> None:
    """Arming with t0 = wall_now + 60s and then offsetting +120s should
    make the FSM see itself as past t0 — phase advances to 'started'."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            future_t0 = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
            await client.post(
                "/api/race-start/arm",
                json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
            )
            # Without offset, FSM is in counting_down.
            r = await client.get("/api/race-start/state")
            assert r.json()["phase"] == "counting_down"

            # Skew +120s — virtual now > t0 → started.
            await client.post("/api/race-start/sim/clock", json={"offset_s": 120.0})
            r = await client.get("/api/race-start/state")
        assert r.json()["phase"] == "started"


# ---------------------------------------------------------------------------
# Synthetic boat state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sim_boat_writes_position(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/boat",
                json={
                    "latitude_deg": 47.65,
                    "longitude_deg": -122.40,
                    "sog_kn": 5.0,
                    "cog_deg": 0.0,
                    "twd_deg": 180.0,
                    "tws_kn": 10.0,
                },
            )
        assert r.status_code == 200
        written = set(r.json()["written"])
        assert {"position", "cogsog", "wind"}.issubset(written)

        latest = await storage.latest_position()
        assert latest is not None
        assert latest["latitude_deg"] == pytest.approx(47.65)


@pytest.mark.asyncio
async def test_sim_boat_partial_payload(storage: Storage) -> None:
    """Only fields supplied get written; unspecified ones are skipped."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/boat",
                json={"latitude_deg": 47.65, "longitude_deg": -122.40},
            )
        assert r.json()["written"] == ["position"]


@pytest.mark.asyncio
async def test_sim_boat_then_ping_uses_db_position(storage: Storage) -> None:
    """Simulator-written position satisfies the real ping endpoint's
    DB fallback — closes the loop end-to-end."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/race-start/sim/boat",
                json={"latitude_deg": 47.65, "longitude_deg": -122.40},
            )
            r = await client.post("/api/race-start/ping/boat", json={})
        assert r.status_code == 200
        assert r.json()["start_line"]["boat_end_lat"] == pytest.approx(47.65)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenarios_listed(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/race-start/sim/scenarios")
        names = [s["name"] for s in r.json()["scenarios"]]
        assert "boat-favoured-square-line" in names
        assert "pin-favoured-3-2-1-0" in names
        assert "general-recall-at-minus-30" in names
        assert "ocs-at-plus-2" in names


@pytest.mark.asyncio
async def test_step_unknown_scenario_404(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/step",
                json={"scenario": "nope", "step_index": 0},
            )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_step_out_of_range_400(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/step",
                json={"scenario": "boat-favoured-square-line", "step_index": 99},
            )
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_step_applies_offset_and_boat(storage: Storage) -> None:
    """Stepping a scenario sets the offset and writes synthetic state."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/step",
                json={"scenario": "pin-favoured-3-2-1-0", "step_index": 0},
            )
            data = r.json()
            assert data["offset_s"] == -180
            assert "position" in data["boat_written"]
            # Wind row should be present in the DB now.
            wind_row = await storage._conn().execute(  # noqa: SLF001
                "SELECT wind_angle_deg FROM winds ORDER BY id DESC LIMIT 1"
            )
            row = await wind_row.fetchone()
            assert row is not None
            assert row[0] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_clears_offset_and_fsm(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            future_t0 = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            await client.post(
                "/api/race-start/arm",
                json={"kind": "5-4-1-0", "t0_utc": future_t0, "classes": []},
            )
            await client.post("/api/race-start/sim/clock", json={"offset_s": -200.0})
            r = await client.post("/api/race-start/sim/reset")
            assert r.status_code == 200

            # Offset back to 0
            assert app.state.race_start_sim_offset_s == 0.0
            # FSM cleared
            r = await client.get("/api/race-start/state")
        assert r.json()["phase"] == "idle"


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sim_page_renders(storage: Storage) -> None:
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/race-start/simulate")
        assert r.status_code == 200
        assert "Simulator" in r.text


@pytest.mark.asyncio
async def test_sim_page_404_when_kill_switch(storage: Storage) -> None:
    with patch.dict(os.environ, _kill_switch_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/race-start/simulate")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Prestart drill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drill_stamps_line_endpoints_and_returns_200(storage: Storage) -> None:
    """Drill returns immediately and stamps both line endpoints."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/race-start/sim/drill",
                json={
                    "center_lat": 47.65,
                    "center_lon": -122.40,
                    "line_bearing_deg": 90.0,
                    "line_length_m": 100.0,
                    "duration_s": 1.0,  # short for the test
                },
            )
        assert r.status_code == 200
        body = r.json()
        assert body["started"] is True
        assert "boat" in body["line_endpoints"]
        assert "pin" in body["line_endpoints"]

    # Both endpoint pings are in storage so the line is "complete"
    line = await storage.get_latest_start_line(race_id=None)
    assert line is not None
    assert line["boat_end_lat"] is not None
    assert line["pin_end_lat"] is not None


@pytest.mark.asyncio
async def test_drill_writes_positions_in_background(storage: Storage) -> None:
    """After waiting briefly for the background task, positions accumulate."""
    with patch.dict(os.environ, _sim_env()):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/race-start/sim/drill",
                json={
                    "center_lat": 47.65,
                    "center_lon": -122.40,
                    "duration_s": 2.0,
                },
            )
            # Let the background task fire a couple of ticks
            await asyncio.sleep(2.5)

    db = storage._conn()  # noqa: SLF001
    cur = await db.execute("SELECT COUNT(*) FROM positions")
    row = await cur.fetchone()
    assert row is not None
    # >= 2 because the drill writes 1 position per second for ~2 seconds
    assert row[0] >= 2
