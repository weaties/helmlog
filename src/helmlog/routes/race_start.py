"""Route handlers for race-start management.

Mutation endpoints require ``crew`` role; reads require ``viewer``.
The page itself is a thin shell — all live computation happens client-side
from the snapshot returned by ``GET /api/race-start/state``.

Timer state (simrad_timer_state) is the single source of truth for all
clock display, whether B&G instruments are connected or not.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helmlog.storage import Storage

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from loguru import logger
from pydantic import BaseModel, Field

from helmlog.auth import require_auth
from helmlog.race_start import StartLine, line_metrics
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

router = APIRouter()

_DEFAULT_DURATION_S = 300  # 5 minutes


def _now_utc(request: Request | None = None) -> datetime:
    return datetime.now(UTC)


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


async def _simrad_timer_payload(storage: Storage) -> dict[str, Any]:
    row = await storage.get_simrad_timer_state()
    if row is None:
        return {
            "instrument_timer_on": False,
            "duration_s": None,
            "t0_utc": None,
            "stopped_remaining_s": None,
            "is_running": False,
            "rolling_timer_on": False,
        }
    return {
        "instrument_timer_on": row["instrument_timer_on"],
        "duration_s": row["duration_s"],
        "t0_utc": row["t0_utc"],
        "stopped_remaining_s": row["stopped_remaining_s"],
        "is_running": row["is_running"],
        "rolling_timer_on": row["rolling_timer_on"],
    }


async def _scheduled_start_payload(storage: Storage) -> dict[str, Any] | None:
    row = await storage.get_scheduled_start()
    if row is None:
        return None
    fire_at = datetime.fromisoformat(row["scheduled_start_utc"])
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=UTC)
    seconds_until = max(0, int((fire_at - datetime.now(UTC)).total_seconds()))
    return {
        "scheduled_start_utc": fire_at.isoformat(),
        "event": row["event"],
        "session_type": row["session_type"],
        "seconds_until_start": seconds_until,
    }


async def _build_snapshot(request: Request) -> dict[str, Any]:
    """Build the JSON snapshot returned by GET /api/race-start/state."""
    now = _now_utc(request)
    storage = get_storage(request)
    current_race = await storage.get_current_race()
    race_id = current_race.id if current_race else None

    line_row = await storage.get_latest_start_line(race_id=race_id)
    if line_row is None and race_id is not None:
        line_row = await storage.get_latest_start_line(race_id=None)
    line = StartLine(
        boat_end_lat=line_row.get("boat_end_lat") if line_row else None,
        boat_end_lon=line_row.get("boat_end_lon") if line_row else None,
        boat_end_captured_at=_parse_dt(line_row.get("boat_end_captured_at") if line_row else None),
        pin_end_lat=line_row.get("pin_end_lat") if line_row else None,
        pin_end_lon=line_row.get("pin_end_lon") if line_row else None,
        pin_end_captured_at=_parse_dt(line_row.get("pin_end_captured_at") if line_row else None),
    )

    metrics_payload: dict[str, Any] | None = None
    if line.is_complete:
        latest_pos = await storage.latest_position()
        instr = await storage.latest_instruments()
        m = line_metrics(
            line,
            boat_lat=latest_pos["latitude_deg"] if latest_pos else None,
            boat_lon=latest_pos["longitude_deg"] if latest_pos else None,
            sog_kn=instr.get("sog_kts"),
            twd_deg=instr.get("twd_deg"),
            cog_deg=instr.get("cog_deg"),
        )
        if m is not None:
            metrics_payload = {
                "line_bearing_deg": m.line_bearing_deg,
                "line_length_m": m.line_length_m,
                "line_bias_deg": m.line_bias_deg,
                "favoured_end": m.favoured_end,
                "distance_to_line_m": m.distance_to_line_m,
                "side_of_line": m.side_of_line,
                "time_to_line_s": m.time_to_line_s,
                "time_to_burn_s": m.time_to_burn_s,
                "note": m.note,
            }

    return {
        "now_utc": now.isoformat(),
        "race_id": race_id,
        "start_line": {
            "boat_end_lat": line.boat_end_lat,
            "boat_end_lon": line.boat_end_lon,
            "boat_end_captured_at": (
                line.boat_end_captured_at.isoformat() if line.boat_end_captured_at else None
            ),
            "boat_end_carried_over_from_race_id": (
                line_row.get("boat_end_race_id")
                if (
                    line_row
                    and race_id is not None
                    and line_row.get("boat_end_race_id") not in (None, race_id)
                )
                else None
            ),
            "pin_end_lat": line.pin_end_lat,
            "pin_end_lon": line.pin_end_lon,
            "pin_end_captured_at": (
                line.pin_end_captured_at.isoformat() if line.pin_end_captured_at else None
            ),
            "pin_end_carried_over_from_race_id": (
                line_row.get("pin_end_race_id")
                if (
                    line_row
                    and race_id is not None
                    and line_row.get("pin_end_race_id") not in (None, race_id)
                )
                else None
            ),
            "is_complete": line.is_complete,
        },
        "line_metrics": metrics_payload,
        "scheduled_start": await _scheduled_start_payload(storage),
        "simrad_timer": await _simrad_timer_payload(storage),
    }


# ---------------------------------------------------------------------------
# Internal timer event — called directly by simrad_timer_sk.py bridge
# ---------------------------------------------------------------------------


class _InternalTimerEventRequest(BaseModel):
    path: str
    value: str | int
    ts: str  # ISO-8601 UTC from bridge hardware CAN timestamp


_SK_TIMER_STATE_PATH = "racing.startTimer.state"
_SK_TIMER_DURATION_PATH = "racing.startTimer.duration"


@router.post("/api/internal/timer-event")
async def api_internal_timer_event(
    request: Request,
    body: _InternalTimerEventRequest,
) -> JSONResponse:
    token = os.environ.get("HELMLOG_TIMER_TOKEN", "")
    if token:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    try:
        nmea_ts = datetime.fromisoformat(body.ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        nmea_ts = datetime.now(UTC)

    from helmlog.simrad_timer import (
        SimradTimerState,
        handle_duration,
        handle_nearest_minute,
        handle_reset,
        handle_running,
        handle_stopped,
    )

    storage: Storage = get_storage(request)
    row = await storage.get_simrad_timer_state()

    # When user has explicitly disabled B&G integration, ignore all inbound
    # events so the toggle cannot auto-re-enable itself from B&G broadcasts.
    if row is not None and not row["instrument_timer_on"]:
        return JSONResponse({"ok": True})

    state = (
        SimradTimerState(
            instrument_timer_on=row["instrument_timer_on"],
            duration_s=row["duration_s"],
            t0_utc=datetime.fromisoformat(row["t0_utc"]) if row["t0_utc"] else None,
            stopped_remaining_s=row["stopped_remaining_s"],
            is_running=bool(row["is_running"]),
        )
        if row
        else SimradTimerState()
    )
    rolling_timer_on = row["rolling_timer_on"] if row else False

    if body.path == _SK_TIMER_DURATION_PATH:
        state = handle_duration(state, duration_s=int(body.value), nmea_ts=nmea_ts)
    elif body.value == "running":
        try:
            state = handle_running(state, nmea_ts=nmea_ts)
        except ValueError:
            return JSONResponse({"ok": False, "error": "no duration set"}, status_code=400)
        current = await storage.get_current_race()
        if current is None:
            from helmlog.races import local_today

            today = local_today()
            date_str = today.isoformat()
            session_type = "race"
            race_num = await storage.count_sessions_for_date(date_str, session_type) + 1
            race_name = nmea_ts.strftime("%Y-%m-%d %H:%M:%S")
            await storage.start_race("", nmea_ts, date_str, race_num, race_name, session_type)
    elif body.value == "stopped":
        state = handle_stopped(state, nmea_ts=nmea_ts)
        current = await storage.get_current_race()
        if current is not None:
            await storage.end_race(current.id, nmea_ts)
    elif body.value == "reset":
        state = handle_reset(state, nmea_ts=nmea_ts)
    elif body.value == "nearest-minute":
        state = handle_nearest_minute(state, nmea_ts=nmea_ts)
    else:
        return JSONResponse(
            {"ok": False, "error": f"unknown value {body.value!r}"}, status_code=400
        )

    await storage.upsert_simrad_timer_state(
        instrument_timer_on=state.instrument_timer_on,
        duration_s=state.duration_s,
        t0_utc=state.t0_utc,
        stopped_remaining_s=state.stopped_remaining_s,
        is_running=state.is_running,
        rolling_timer_on=rolling_timer_on,
        now_utc=nmea_ts,
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Instrument Timer toggle (crew)
# ---------------------------------------------------------------------------


class InstrumentTimerRequest(BaseModel):
    on: bool


@router.post("/api/race-start/instrument-timer")
async def api_instrument_timer(
    request: Request,
    body: InstrumentTimerRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage: Storage = get_storage(request)
    row = await storage.get_simrad_timer_state()
    if row is None and not body.on:
        return JSONResponse({"ok": True})
    now = _now_utc(request)
    await storage.upsert_simrad_timer_state(
        instrument_timer_on=body.on,
        duration_s=row["duration_s"] if row else None,
        t0_utc=_parse_dt(row["t0_utc"]) if row else None,
        stopped_remaining_s=row["stopped_remaining_s"] if row else None,
        is_running=bool(row["is_running"]) if row else False,
        rolling_timer_on=row["rolling_timer_on"] if row else False,
        now_utc=now,
    )
    await audit(request, "instrument_timer_toggle", detail=str(body.on), user=user)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# B&G outbound helper
# ---------------------------------------------------------------------------


async def _bg_send(request: Request, command: str, minutes: int | None = None) -> None:
    """Forward a command to B&G instruments when integration is active.

    No-op if the Instrument Timer toggle is OFF or no CAN writer is wired up.
    Failures are logged as warnings and never propagate.
    """
    row = await get_storage(request).get_simrad_timer_state()
    if not row or not row["instrument_timer_on"]:
        return
    can_writer = getattr(request.app.state, "can_writer", None)
    if can_writer is None:
        return
    try:
        await can_writer.send(command, minutes)
    except Exception as exc:
        logger.warning("B&G outbound {!r} failed: {}", command, exc)


# ---------------------------------------------------------------------------
# Page (viewer)
# ---------------------------------------------------------------------------


@router.get("/race-start", response_class=HTMLResponse, include_in_schema=False)
async def race_start_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    is_writer = _user.get("role") in {"crew", "admin"}
    return templates.TemplateResponse(
        request,
        "race_start.html",
        tpl_ctx(request, "/race-start", is_writer=is_writer),
    )


# ---------------------------------------------------------------------------
# State read (viewer)
# ---------------------------------------------------------------------------


@router.get("/api/race-start/state")
async def api_state(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    return JSONResponse(await _build_snapshot(request))


# ---------------------------------------------------------------------------
# Timer mutations (crew)
# ---------------------------------------------------------------------------


@router.post("/api/race-start/start")
async def api_start(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Start the timer and open a new race (closing any open race first)."""
    now = _now_utc(request)
    storage = get_storage(request)
    row = await storage.get_simrad_timer_state()
    duration_s = row["duration_s"] if row and row["duration_s"] else _DEFAULT_DURATION_S
    rolling_timer_on = row["rolling_timer_on"] if row else False
    instr_on = row["instrument_timer_on"] if row else False

    # Close any in-progress race before opening a new one.
    current = await storage.get_current_race()
    if current is not None:
        await storage.end_race(current.id, now)

    from helmlog.races import local_today

    today = local_today()
    date_str = today.isoformat()
    session_type = "race"
    race_num = await storage.count_sessions_for_date(date_str, session_type) + 1
    race_name = now.strftime("%Y-%m-%d %H:%M:%S")
    await storage.start_race("", now, date_str, race_num, race_name, session_type)

    t0_utc = now + timedelta(seconds=duration_s)
    await storage.upsert_simrad_timer_state(
        instrument_timer_on=instr_on,
        duration_s=duration_s,
        t0_utc=t0_utc,
        stopped_remaining_s=None,
        is_running=True,
        rolling_timer_on=rolling_timer_on,
        now_utc=now,
    )
    await audit(request, "race_start.start", user=user)
    await _bg_send(request, "start")
    return JSONResponse(await _build_snapshot(request))


@router.post("/api/race-start/stop")
async def api_stop(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Stop the timer and end the current race."""
    now = _now_utc(request)
    storage = get_storage(request)
    row = await storage.get_simrad_timer_state()

    stopped_remaining_s: float | None = None
    if row and row["t0_utc"]:
        t0 = datetime.fromisoformat(row["t0_utc"])
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=UTC)
        stopped_remaining_s = max(0.0, (t0 - now).total_seconds())

    await storage.upsert_simrad_timer_state(
        instrument_timer_on=row["instrument_timer_on"] if row else False,
        duration_s=row["duration_s"] if row else None,
        t0_utc=_parse_dt(row["t0_utc"]) if row else None,
        stopped_remaining_s=stopped_remaining_s,
        is_running=False,
        rolling_timer_on=row["rolling_timer_on"] if row else False,
        now_utc=now,
    )

    current = await storage.get_current_race()
    if current is not None:
        await storage.end_race(current.id, now)

    await audit(request, "race_start.stop", user=user)
    await _bg_send(request, "stop")
    return JSONResponse(await _build_snapshot(request))


class SetDurationRequest(BaseModel):
    duration_s: int = Field(..., ge=60, le=3600, description="Timer duration in seconds")


@router.post("/api/race-start/set-duration")
async def api_set_duration(
    request: Request,
    body: SetDurationRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Set the timer duration. Only permitted when the timer is stopped."""
    now = _now_utc(request)
    storage = get_storage(request)
    row = await storage.get_simrad_timer_state()

    if row and row["is_running"]:
        raise HTTPException(status_code=409, detail="cannot change duration while timer is running")

    await storage.upsert_simrad_timer_state(
        instrument_timer_on=row["instrument_timer_on"] if row else False,
        duration_s=body.duration_s,
        t0_utc=None,
        stopped_remaining_s=None,
        is_running=False,
        rolling_timer_on=row["rolling_timer_on"] if row else False,
        now_utc=now,
    )
    await audit(request, "race_start.set_duration", detail=str(body.duration_s), user=user)
    minutes = max(1, round(body.duration_s / 60))
    await _bg_send(request, "set", minutes=minutes)
    return JSONResponse(await _build_snapshot(request))


@router.post("/api/race-start/sync")
async def api_sync(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Snap the running timer to the nearest whole minute."""
    now = _now_utc(request)
    storage = get_storage(request)
    row = await storage.get_simrad_timer_state()

    if row and row["is_running"] and row["t0_utc"]:
        t0 = datetime.fromisoformat(row["t0_utc"])
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=UTC)
        remaining_s = (t0 - now).total_seconds()
        # Round to nearest minute: if remainder >= 30s round up, else round down.
        nearest_minutes = round(remaining_s / 60)
        new_remaining_s = max(0, nearest_minutes * 60)
        new_t0 = (now + timedelta(seconds=new_remaining_s)).replace(second=0, microsecond=0)
        await storage.upsert_simrad_timer_state(
            instrument_timer_on=row["instrument_timer_on"],
            duration_s=row["duration_s"],
            t0_utc=new_t0,
            stopped_remaining_s=None,
            is_running=True,
            rolling_timer_on=row["rolling_timer_on"],
            now_utc=now,
        )

    await audit(request, "race_start.sync", user=user)
    await _bg_send(request, "nearest-minute")
    return JSONResponse(await _build_snapshot(request))


@router.post("/api/race-start/timer-reset")
async def api_timer_reset(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Reset the timer to the stored duration without stopping it."""
    now = _now_utc(request)
    storage = get_storage(request)
    row = await storage.get_simrad_timer_state()
    duration_s = row["duration_s"] if row and row["duration_s"] else _DEFAULT_DURATION_S
    is_running = bool(row["is_running"]) if row else False

    new_t0 = (now + timedelta(seconds=duration_s)) if is_running else None
    await storage.upsert_simrad_timer_state(
        instrument_timer_on=row["instrument_timer_on"] if row else False,
        duration_s=duration_s,
        t0_utc=new_t0,
        stopped_remaining_s=None,
        is_running=is_running,
        rolling_timer_on=row["rolling_timer_on"] if row else False,
        now_utc=now,
    )
    await audit(request, "race_start.timer_reset", user=user)
    await _bg_send(request, "reset")
    return JSONResponse(await _build_snapshot(request))


class RollingTimerRequest(BaseModel):
    on: bool


@router.post("/api/race-start/rolling-timer")
async def api_rolling_timer(
    request: Request,
    body: RollingTimerRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Toggle the rolling timer (auto-restart at 0:00)."""
    now = _now_utc(request)
    storage = get_storage(request)
    row = await storage.get_simrad_timer_state()
    await storage.upsert_simrad_timer_state(
        instrument_timer_on=row["instrument_timer_on"] if row else False,
        duration_s=row["duration_s"] if row else None,
        t0_utc=_parse_dt(row["t0_utc"]) if row else None,
        stopped_remaining_s=row["stopped_remaining_s"] if row else None,
        is_running=bool(row["is_running"]) if row else False,
        rolling_timer_on=body.on,
        now_utc=now,
    )
    await audit(request, "race_start.rolling_timer", detail=str(body.on), user=user)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Line pings (crew)
# ---------------------------------------------------------------------------


class PingRequest(BaseModel):
    """Body for ping endpoints.

    Both fields are optional. If omitted, the server uses the latest
    boat position from the ``positions`` table (Signal K / GPS feed).
    Manual lat/lon overrides exist for offline testing and edge cases
    where the GPS hasn't yet produced a fix.
    """

    latitude_deg: float | None = None
    longitude_deg: float | None = None


async def _ping(
    request: Request,
    end_kind: str,
    body: PingRequest,
    user: dict[str, Any],
) -> JSONResponse:
    storage = get_storage(request)
    lat = body.latitude_deg
    lon = body.longitude_deg
    if lat is None or lon is None:
        pos = await storage.latest_position()
        if pos is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no GPS fix available — supply latitude_deg/longitude_deg "
                    "manually or wait for a position record"
                ),
            )
        lat = pos["latitude_deg"]
        lon = pos["longitude_deg"]
    if not -90.0 <= lat <= 90.0:
        raise HTTPException(status_code=400, detail="latitude out of range")
    if not -180.0 <= lon <= 180.0:
        raise HTTPException(status_code=400, detail="longitude out of range")
    current_race = await storage.get_current_race()
    race_id = current_race.id if current_race else None
    await storage.add_start_line_ping(
        race_id=race_id,
        end_kind=end_kind,
        latitude_deg=lat,
        longitude_deg=lon,
        captured_at=_now_utc(request),
        captured_by=user.get("id"),
    )
    await audit(
        request,
        f"race_start.ping_{end_kind}",
        detail=f"{lat:.6f},{lon:.6f}",
        user=user,
    )
    return JSONResponse(await _build_snapshot(request))


@router.post("/api/race-start/ping/boat")
async def api_ping_boat(
    request: Request,
    body: PingRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    return await _ping(request, "boat", body, user)


@router.post("/api/race-start/ping/pin")
async def api_ping_pin(
    request: Request,
    body: PingRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    return await _ping(request, "pin", body, user)


# ---------------------------------------------------------------------------
# Live derived metrics (viewer) — drives the home-page status strip
# ---------------------------------------------------------------------------


class MetricsQuery(BaseModel):
    boat_lat: float | None = None
    boat_lon: float | None = None
    sog_kn: float | None = None
    twd_deg: float | None = None
    cog_deg: float | None = None


@router.post("/api/race-start/line-metrics")
async def api_line_metrics(
    request: Request,
    body: MetricsQuery,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Compute live line metrics from a boat snapshot."""
    storage = get_storage(request)
    current_race = await storage.get_current_race()
    race_id = current_race.id if current_race else None
    line_row = await storage.get_latest_start_line(race_id=race_id) or (
        await storage.get_latest_start_line(race_id=None)
    )
    if line_row is None:
        return JSONResponse({"metrics": None, "note": "ping both ends to enable line metrics"})
    line = StartLine(
        boat_end_lat=line_row.get("boat_end_lat"),
        boat_end_lon=line_row.get("boat_end_lon"),
        pin_end_lat=line_row.get("pin_end_lat"),
        pin_end_lon=line_row.get("pin_end_lon"),
    )
    if not line.is_complete:
        return JSONResponse({"metrics": None, "note": "ping both ends to enable line metrics"})

    metrics = line_metrics(
        line,
        boat_lat=body.boat_lat,
        boat_lon=body.boat_lon,
        sog_kn=body.sog_kn,
        twd_deg=body.twd_deg,
        cog_deg=body.cog_deg,
    )
    if metrics is None:
        return JSONResponse({"metrics": None})
    return JSONResponse(
        {
            "metrics": {
                "line_bearing_deg": metrics.line_bearing_deg,
                "line_length_m": metrics.line_length_m,
                "line_bias_deg": metrics.line_bias_deg,
                "favoured_end": metrics.favoured_end,
                "distance_to_line_m": metrics.distance_to_line_m,
                "side_of_line": metrics.side_of_line,
                "time_to_line_s": metrics.time_to_line_s,
                "time_to_burn_s": metrics.time_to_burn_s,
                "note": metrics.note,
            }
        }
    )
