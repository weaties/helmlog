"""Race-start simulator (#690).

Offline validation harness for #644: time skew, synthetic boat state, and
scenario presets.

Access is gated on the developer flag (``users.is_developer = 1``) — the
routes are always mounted, but a non-developer hits a 403, so the page
isn't usable to anyone but a dev. ``RACE_START_SIMULATOR=false`` (or
``=off``) is a hard kill switch that turns the routes into 404s for
defense in depth on prod boats; the default is "on" so devs can use it
without restarting the service.

The simulator exercises the *real* race-start routes — we don't fake the
FSM, the flag resolver, or the geometry. We only fake (a) the wall clock
that the FSM ticks against, and (b) the boat-state inserts (positions,
cogsog, winds) that the line-metrics endpoint reads from. Every path
through the real code stays in the test loop.
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from helmlog.auth import require_developer
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

router = APIRouter()


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def is_simulator_enabled() -> bool:
    """Whether the simulator routes are mounted at all.

    Defaults to True so devs can use the simulator without restarting the
    service. Set ``RACE_START_SIMULATOR=false`` (or ``off``/``no``/``0``)
    to disable as a kill switch on production boats. Per-route auth still
    enforces ``is_developer`` regardless of this flag.
    """
    val = os.environ.get("RACE_START_SIMULATOR", "true").lower()
    return val not in {"0", "false", "no", "off"}


def _require_sim() -> None:
    if not is_simulator_enabled():
        raise HTTPException(
            status_code=404,
            detail="race-start simulator disabled (RACE_START_SIMULATOR=false)",
        )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get(
    "/race-start/simulate",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def sim_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> Response:
    _require_sim()
    return templates.TemplateResponse(
        request,
        "race_start_sim.html",
        tpl_ctx(request, "/race-start/simulate"),
    )


# ---------------------------------------------------------------------------
# Virtual clock
# ---------------------------------------------------------------------------


class ClockRequest(BaseModel):
    offset_s: float = Field(
        ...,
        description=(
            "Seconds to add to wall-clock UTC. Negative jumps the FSM into the "
            "future relative to the user; positive into the past. Set to 0 to "
            "return to real time."
        ),
    )


@router.get("/api/race-start/sim/clock")
async def sim_get_clock(
    request: Request,
    _user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    offset = float(getattr(request.app.state, "race_start_sim_offset_s", 0.0))
    return JSONResponse(
        {
            "offset_s": offset,
            "real_now_utc": datetime.now(UTC).isoformat(),
            "virtual_now_utc": (
                datetime.now(UTC).timestamp() + offset
            ),  # epoch seconds; client formats
        }
    )


@router.post("/api/race-start/sim/clock")
async def sim_set_clock(
    request: Request,
    body: ClockRequest,
    user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    request.app.state.race_start_sim_offset_s = float(body.offset_s)
    await audit(request, "race_start_sim.clock", detail=str(body.offset_s), user=user)
    return JSONResponse({"offset_s": body.offset_s})


# ---------------------------------------------------------------------------
# Synthetic boat state — writes to positions / cogsog / winds tables
# ---------------------------------------------------------------------------


class BoatStateRequest(BaseModel):
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    sog_kn: float | None = None
    cog_deg: float | None = None
    twd_deg: float | None = None
    tws_kn: float | None = None


async def _write_synthetic_state(request: Request, body: BoatStateRequest) -> dict[str, Any]:
    """Insert a synthetic boat-state row into positions / cogsog / winds.

    Reuses the same SQL the real readers use so any downstream consumer
    (line-metrics, latest_position, etc.) sees identical rows.
    """
    storage = get_storage(request)
    # Use the simulator's virtual-now so the rows fall in the simulated
    # timeline (relevant for time-windowed queries).
    real = datetime.now(UTC)
    offset = float(getattr(request.app.state, "race_start_sim_offset_s", 0.0))
    ts = real.isoformat() if offset == 0.0 else (real.timestamp() + offset)
    if isinstance(ts, float):
        ts = datetime.fromtimestamp(ts, tz=UTC).isoformat()

    db = storage._conn()  # noqa: SLF001 — direct write is the simulator's job
    written: dict[str, Any] = {}
    if body.latitude_deg is not None and body.longitude_deg is not None:
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 0, body.latitude_deg, body.longitude_deg),
        )
        written["position"] = (body.latitude_deg, body.longitude_deg)
    if body.sog_kn is not None and body.cog_deg is not None:
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (ts, 0, body.cog_deg, body.sog_kn),
        )
        written["cogsog"] = (body.cog_deg, body.sog_kn)
    if body.twd_deg is not None:
        # Reference 0 = ground/true wind in helmlog's wind schema.
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 0, body.tws_kn or 0.0, body.twd_deg, 0),
        )
        written["wind"] = (body.twd_deg, body.tws_kn)
    await db.commit()
    return written


@router.post("/api/race-start/sim/boat")
async def sim_set_boat(
    request: Request,
    body: BoatStateRequest,
    user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    written = await _write_synthetic_state(request, body)
    await audit(request, "race_start_sim.boat", detail=str(written), user=user)
    return JSONResponse({"written": list(written.keys())})


# ---------------------------------------------------------------------------
# Scenario presets
# ---------------------------------------------------------------------------


# Each preset is a list of (offset_s, BoatStateRequest | None, label) — the
# user clicks "step" to advance, or "play" to fire them all in sequence on
# a real-time interval.
SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "boat-favoured-square-line": [
        {
            "offset_s": -300,
            "label": "Warning signal (5 min to start). Square line, 10 kn south wind.",
            "boat": {
                "latitude_deg": 47.6500,
                "longitude_deg": -122.4000,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
        {"offset_s": -60, "label": "1-min signal", "boat": None},
        {"offset_s": 0, "label": "Start gun (square line, neutral bias)", "boat": None},
    ],
    "pin-favoured-3-2-1-0": [
        {
            "offset_s": -180,
            "label": "Warning signal for 3-2-1-0 sequence. East wind, line east-west.",
            "boat": {
                "latitude_deg": 47.6500,
                "longitude_deg": -122.4000,
                "sog_kn": 5.0,
                "cog_deg": 90.0,
                "twd_deg": 90.0,  # wind from east → pin end favoured
                "tws_kn": 12.0,
            },
        },
        {"offset_s": -60, "label": "1-min signal", "boat": None},
        {"offset_s": 0, "label": "Start gun (pin favoured)", "boat": None},
    ],
    "general-recall-at-minus-30": [
        {
            "offset_s": -60,
            "label": "1-min signal in counting_down phase",
            "boat": {
                "latitude_deg": 47.65,
                "longitude_deg": -122.40,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
        {"offset_s": -30, "label": "30s — RC raises First Sub for general recall", "boat": None},
    ],
    "ap-then-resume": [
        {
            "offset_s": -120,
            "label": "Counting down → AP postpones",
            "boat": {
                "latitude_deg": 47.65,
                "longitude_deg": -122.40,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
    ],
    "ocs-at-plus-2": [
        {
            "offset_s": 2,
            "label": "FSM has fired the gun. Boat is across the line — flag X expected.",
            "boat": {
                "latitude_deg": 47.6505,  # north of the line
                "longitude_deg": -122.4000,
                "sog_kn": 6.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
    ],
    "multi-class-J70-then-PHRF": [
        {
            "offset_s": -360,
            "label": "PHRF-A start in 60s; J/70 (ours) in 360s",
            "boat": {
                "latitude_deg": 47.65,
                "longitude_deg": -122.40,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
        {"offset_s": -300, "label": "Our class flag goes up", "boat": None},
        {"offset_s": -240, "label": "Our prep flag (I) goes up", "boat": None},
        {"offset_s": -60, "label": "1-min signal", "boat": None},
        {"offset_s": 0, "label": "Our gun", "boat": None},
    ],
}


@router.get("/api/race-start/sim/scenarios")
async def sim_list_scenarios(
    request: Request,
    _user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    return JSONResponse(
        {"scenarios": [{"name": name, "steps": len(steps)} for name, steps in SCENARIOS.items()]}
    )


class StepRequest(BaseModel):
    scenario: str
    step_index: int = Field(0, ge=0)


@router.post("/api/race-start/sim/step")
async def sim_step(
    request: Request,
    body: StepRequest,
    user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    """Apply one step of a scenario.

    Sets the virtual clock offset and writes any synthetic boat state.
    Does NOT arm or mutate the FSM — callers do that via the real
    race-start endpoints (the simulator UI typically arms first, then
    steps through the timeline)."""
    _require_sim()
    if body.scenario not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario: {body.scenario!r}")
    steps = SCENARIOS[body.scenario]
    if body.step_index >= len(steps):
        raise HTTPException(status_code=400, detail="step_index out of range")
    step = steps[body.step_index]
    request.app.state.race_start_sim_offset_s = float(step["offset_s"])
    written: dict[str, Any] = {}
    if step.get("boat"):
        written = await _write_synthetic_state(request, BoatStateRequest(**step["boat"]))
    await audit(
        request,
        "race_start_sim.step",
        detail=f"{body.scenario}#{body.step_index}",
        user=user,
    )
    return JSONResponse(
        {
            "scenario": body.scenario,
            "step_index": body.step_index,
            "label": step.get("label", ""),
            "offset_s": step["offset_s"],
            "boat_written": list(written.keys()),
            "is_last": body.step_index == len(steps) - 1,
        }
    )


@router.post("/api/race-start/sim/reset")
async def sim_reset(
    request: Request,
    user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    """Reset clock offset to 0 and clear race-start state.

    Does NOT delete synthetic positions/cogsog/winds rows — those live
    on as part of the test history and are useful for inspecting what
    the simulator wrote.
    """
    _require_sim()
    request.app.state.race_start_sim_offset_s = 0.0
    storage = get_storage(request)
    await storage.clear_race_start_state()
    await audit(request, "race_start_sim.reset", user=user)
    return JSONResponse({"offset_s": 0.0, "fsm_cleared": True})


# ---------------------------------------------------------------------------
# Prestart drill — auto-walk the boat around the line for a few minutes
# ---------------------------------------------------------------------------


# A circular hold pattern around a synthetic start line. The drill writes
# one position every DRILL_TICK_S seconds for DRILL_DURATION_S real seconds.
DRILL_TICK_S: float = 1.0
DRILL_DURATION_S: float = 30.0  # 30 s of real time
DRILL_RADIUS_M: float = 60.0  # circle radius around midline
DRILL_TWS_KN: float = 10.0


def _offset_lat_lon(
    lat: float, lon: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """Project a (lat, lon) by *distance_m* metres along *bearing_deg*.

    Small-angle approximation — accurate enough at start-line scales.
    """
    rad = math.radians(bearing_deg)
    dlat = (distance_m * math.cos(rad)) / 111_320.0
    dlon = (distance_m * math.sin(rad)) / (111_320.0 * math.cos(math.radians(lat)))
    return (lat + dlat, lon + dlon)


class DrillRequest(BaseModel):
    center_lat: float = 47.6500
    center_lon: float = -122.4000
    line_bearing_deg: float = 90.0  # boat-end → pin-end bearing
    line_length_m: float = 100.0
    twd_deg: float = 180.0  # wind from south (square line)
    sog_kn: float = 4.0
    duration_s: float = DRILL_DURATION_S


async def _run_drill(app: Any, body: DrillRequest) -> None:  # noqa: ANN401 — FastAPI app type
    """Background task — writes one position per second around the start
    area for ``duration_s`` seconds. The dev sees positions accumulate on
    the session map in real time."""
    storage = app.state.storage
    n_ticks = int(body.duration_s / DRILL_TICK_S)
    for i in range(n_ticks):
        # Hold-pattern circle around the line midpoint.
        angle = (i / n_ticks) * 360.0
        lat, lon = _offset_lat_lon(body.center_lat, body.center_lon, angle, DRILL_RADIUS_M)
        # COG is tangent to the circle (angle + 90).
        cog = (angle + 90.0) % 360.0
        ts = datetime.now(UTC).isoformat()
        try:
            db = storage._conn()  # noqa: SLF001
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0, lat, lon),
            )
            await db.execute(
                "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
                (ts, 0, cog, body.sog_kn),
            )
            await db.execute(
                "INSERT INTO winds"
                " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, ?)",
                (ts, 0, DRILL_TWS_KN, body.twd_deg, 0),
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            # Drill is best-effort — never fail loudly.
            return
        await asyncio.sleep(DRILL_TICK_S)


@router.post("/api/race-start/sim/drill")
async def sim_drill(
    request: Request,
    body: DrillRequest,
    user: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    """Kick off the prestart drill in the background.

    Writes positions around a synthetic start line for ``duration_s``
    seconds. Also stamps the line endpoints into ``start_line_pings``
    so the line draws on the session map immediately. The dev should
    have already run ``/control`` → "Start race" so positions land in
    the active race's window.
    """
    _require_sim()
    # Stamp the line endpoints (boat = west, pin = east of centre along
    # line_bearing_deg). Tied to the active race if there is one.
    storage = get_storage(request)
    half = body.line_length_m / 2.0
    boat_lat, boat_lon = _offset_lat_lon(
        body.center_lat, body.center_lon, (body.line_bearing_deg + 180.0) % 360.0, half
    )
    pin_lat, pin_lon = _offset_lat_lon(
        body.center_lat, body.center_lon, body.line_bearing_deg, half
    )
    current_race = await storage.get_current_race()
    race_id = current_race.id if current_race else None
    now = datetime.now(UTC)
    await storage.add_start_line_ping(
        race_id=race_id,
        end_kind="boat",
        latitude_deg=boat_lat,
        longitude_deg=boat_lon,
        captured_at=now,
        captured_by=user.get("id"),
    )
    await storage.add_start_line_ping(
        race_id=race_id,
        end_kind="pin",
        latitude_deg=pin_lat,
        longitude_deg=pin_lon,
        captured_at=now,
        captured_by=user.get("id"),
    )

    # Fire-and-forget background task. Using request.app.state.storage so
    # we don't depend on the request scope after we return.
    asyncio.create_task(_run_drill(request.app, body))

    await audit(
        request,
        "race_start_sim.drill",
        detail=f"center=({body.center_lat:.4f},{body.center_lon:.4f})"
        f" len={body.line_length_m}m duration={body.duration_s}s",
        user=user,
    )
    return JSONResponse(
        {
            "started": True,
            "duration_s": body.duration_s,
            "center": [body.center_lat, body.center_lon],
            "line_endpoints": {
                "boat": [boat_lat, boat_lon],
                "pin": [pin_lat, pin_lon],
            },
            "race_id": race_id,
        }
    )
