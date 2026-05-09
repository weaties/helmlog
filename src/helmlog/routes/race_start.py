"""Route handlers for race-start management (#644).

Mutation endpoints require ``crew`` role; reads require ``viewer``.
The page itself is a thin shell — all live computation happens client-side
from the snapshot returned by ``GET /api/race-start/state``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helmlog.storage import Storage

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from helmlog.auth import require_auth
from helmlog.race_start import (
    IDLE,
    SEQUENCE_KINDS,
    ClassEntry,
    SequenceState,
    StartLine,
    abandon,
    arm,
    flag_state,
    general_recall,
    line_metrics,
    nudge,
    postpone,
    reset,
    restart_after_recall,
    resume_from_postponement,
    sync_to_gun,
    tick,
)
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

router = APIRouter()


# ---------------------------------------------------------------------------
# Hydrate / persist SequenceState
# ---------------------------------------------------------------------------


def _sim_offset_s(request: Request) -> float:
    """Race-start simulator clock offset, in seconds. 0 in production.

    The simulator (#690, gated by ``RACE_START_SIMULATOR=true``) sets this
    on ``app.state`` to skew the FSM clock for offline validation. Outside
    the simulator the attribute is absent and the offset is 0.
    """
    return float(getattr(request.app.state, "race_start_sim_offset_s", 0.0))


def _now_utc(request: Request | None = None) -> datetime:
    """Wall-clock UTC, plus simulator skew when the simulator is active."""
    real = datetime.now(UTC)
    if request is None:
        return real
    offset = _sim_offset_s(request)
    if offset == 0.0:
        return real
    return real + timedelta(seconds=offset)


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _classes_from_json(raw: str) -> tuple[ClassEntry, ...]:
    if not raw:
        return ()
    items = json.loads(raw)
    return tuple(
        ClassEntry(
            name=item["name"],
            order=int(item["order"]),
            is_ours=bool(item.get("is_ours", False)),
            prep_flag=item.get("prep_flag", "P"),
        )
        for item in items
    )


def _classes_to_json(classes: tuple[ClassEntry, ...]) -> str:
    return json.dumps(
        [
            {
                "name": c.name,
                "order": c.order,
                "is_ours": c.is_ours,
                "prep_flag": c.prep_flag,
            }
            for c in classes
        ]
    )


async def _load_state(request: Request) -> SequenceState:
    storage = get_storage(request)
    row = await storage.get_race_start_state()
    if row is None:
        return IDLE
    return SequenceState(
        phase=row["phase"],
        kind=row["kind"],
        t0_utc=_parse_dt(row["t0_utc"]),
        sync_offset_s=row["sync_offset_s"],
        last_sync_at_utc=_parse_dt(row["last_sync_at_utc"]),
        started_at_utc=_parse_dt(row["started_at_utc"]),
        classes=_classes_from_json(row["classes_json"]),
    )


async def _save_state(request: Request, state: SequenceState) -> None:
    storage = get_storage(request)
    if state.phase == "idle":
        await storage.clear_race_start_state()
        return
    await storage.upsert_race_start_state(
        phase=state.phase,
        kind=state.kind,
        t0_utc=state.t0_utc,
        sync_offset_s=state.sync_offset_s,
        last_sync_at_utc=state.last_sync_at_utc,
        started_at_utc=state.started_at_utc,
        classes_json=_classes_to_json(state.classes),
        now_utc=_now_utc(request),
    )
    # When the gun fires (state.phase == "started") and a race is in
    # progress, anchor its start_utc to the actual gun time. Cheap to
    # wire and matches the user-visible "the race started here" intuition.
    if state.phase == "started" and state.t0_utc is not None:
        current = await storage.get_current_race()
        if current is not None and current.start_utc != state.t0_utc:
            await storage.set_race_start_utc(current.id, state.t0_utc)


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------


async def _build_snapshot(request: Request, state: SequenceState) -> dict[str, Any]:
    """Build the JSON snapshot returned by GET /api/race-start/state."""
    now = _now_utc(request)
    state = tick(state, now)
    if state != (await _load_state(request)):
        # tick() advanced the phase — persist so reloads don't replay.
        await _save_state(request, state)

    storage = get_storage(request)
    current_race = await storage.get_current_race()
    race_id = current_race.id if current_race else None
    # Storage handles per-end carry-over from prior same-date races when
    # *race_id* is set (#702). When no race is active we still fall back
    # to unscoped pre-arm pings so the helm can ping before the race row
    # exists.
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

    flags = flag_state(state, now)

    # Live line metrics from the latest position + cogsog + wind. Pulling
    # these into the state snapshot means the page can render bearing /
    # length / bias / dist / time-to-line on every poll without a separate
    # round-trip. Returns None for any field we can't compute (incomplete
    # line, low SOG, missing TWD — see EARS §E in the spec).
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
        "sim_offset_s": _sim_offset_s(request),
        "phase": state.phase,
        "kind": state.kind,
        "t0_utc": state.t0_utc.isoformat() if state.t0_utc else None,
        "sync_offset_s": state.sync_offset_s,
        "last_sync_at_utc": (
            state.last_sync_at_utc.isoformat() if state.last_sync_at_utc else None
        ),
        "classes": [
            {
                "name": c.name,
                "order": c.order,
                "is_ours": c.is_ours,
                "prep_flag": c.prep_flag,
            }
            for c in state.classes
        ],
        "flags": {
            "class_flag_up": flags.class_flag_up,
            "prep_flag_up": flags.prep_flag_up,
            "special_flag_up": flags.special_flag_up,
            "next_change_in_s": flags.next_change_in_s,
            "note": flags.note,
        },
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
        "race_id": race_id,
        "scheduled_start": await _scheduled_start_payload(storage),
    }


async def _scheduled_start_payload(storage: Storage) -> dict[str, Any] | None:
    """Surface the active scheduled-start row (if any) so /race-start can
    display the upcoming gun and offer an "Arm for scheduled start" button.

    Use case: pursuit starts where each boat's gun depends on rating —
    the helm sets the schedule the night before, and the page shows the
    countdown without anyone needing to remember to arm at the right
    moment.
    """
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
    state = await _load_state(request)
    return JSONResponse(await _build_snapshot(request, state))


# ---------------------------------------------------------------------------
# Mutations (crew)
# ---------------------------------------------------------------------------


class ArmRequest(BaseModel):
    kind: str = Field(..., description="Sequence kind, e.g. '5-4-1-0'")
    t0_utc: str = Field(..., description="ISO-8601 UTC start signal time")
    classes: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Optional class stack (list of {name, order, is_ours, prep_flag})",
    )


@router.post("/api/race-start/arm")
async def api_arm(
    request: Request,
    body: ArmRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    if body.kind not in SEQUENCE_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind: {body.kind!r}")
    try:
        t0 = datetime.fromisoformat(body.t0_utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad t0_utc: {exc}") from exc
    if t0.tzinfo is None:
        raise HTTPException(status_code=400, detail="t0_utc must include timezone")
    try:
        classes = tuple(
            ClassEntry(
                name=item["name"],
                order=int(item["order"]),
                is_ours=bool(item.get("is_ours", False)),
                prep_flag=item.get("prep_flag", "P"),
            )
            for item in body.classes
        )
        state = arm(body.kind, t0, classes)  # type: ignore[arg-type]
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _save_state(request, state)
    await audit(request, "race_start.arm", detail=body.kind, user=user)
    return JSONResponse(await _build_snapshot(request, state))


class SyncRequest(BaseModel):
    expected_signal_offset_s: int = Field(
        ..., description="Seconds before t0 the user is syncing against (e.g. 300, 60, 0)"
    )


@router.post("/api/race-start/sync")
async def api_sync(
    request: Request,
    body: SyncRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    state = await _load_state(request)
    try:
        new_state = sync_to_gun(state, _now_utc(request), body.expected_signal_offset_s)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _save_state(request, new_state)
    await audit(
        request,
        "race_start.sync",
        detail=f"offset={body.expected_signal_offset_s}",
        user=user,
    )
    return JSONResponse(await _build_snapshot(request, new_state))


class NudgeRequest(BaseModel):
    delta_s: int = Field(..., description="Shift t0 by this many seconds")


@router.post("/api/race-start/nudge")
async def api_nudge(
    request: Request,
    body: NudgeRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    state = await _load_state(request)
    try:
        new_state = nudge(state, body.delta_s)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _save_state(request, new_state)
    await audit(request, "race_start.nudge", detail=str(body.delta_s), user=user)
    return JSONResponse(await _build_snapshot(request, new_state))


@router.post("/api/race-start/postpone")
async def api_postpone(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    state = await _load_state(request)
    try:
        new_state = postpone(state)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _save_state(request, new_state)
    await audit(request, "race_start.postpone", user=user)
    return JSONResponse(await _build_snapshot(request, new_state))


class ResumeRequest(BaseModel):
    new_t0_utc: str = Field(..., description="New t0 after AP comes down")


@router.post("/api/race-start/resume")
async def api_resume(
    request: Request,
    body: ResumeRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    try:
        new_t0 = datetime.fromisoformat(body.new_t0_utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad new_t0_utc: {exc}") from exc
    if new_t0.tzinfo is None:
        raise HTTPException(status_code=400, detail="new_t0_utc must include timezone")
    state = await _load_state(request)
    try:
        new_state = resume_from_postponement(state, new_t0)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _save_state(request, new_state)
    await audit(request, "race_start.resume", user=user)
    return JSONResponse(await _build_snapshot(request, new_state))


@router.post("/api/race-start/recall")
async def api_recall(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    state = await _load_state(request)
    try:
        new_state = general_recall(state, _now_utc(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _save_state(request, new_state)
    await audit(request, "race_start.recall", user=user)
    return JSONResponse(await _build_snapshot(request, new_state))


class RestartRequest(BaseModel):
    new_t0_utc: str = Field(..., description="New t0 for the restarted sequence")


@router.post("/api/race-start/restart")
async def api_restart(
    request: Request,
    body: RestartRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    try:
        new_t0 = datetime.fromisoformat(body.new_t0_utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad new_t0_utc: {exc}") from exc
    if new_t0.tzinfo is None:
        raise HTTPException(status_code=400, detail="new_t0_utc must include timezone")
    state = await _load_state(request)
    try:
        new_state = restart_after_recall(state, new_t0)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _save_state(request, new_state)
    await audit(request, "race_start.restart", user=user)
    return JSONResponse(await _build_snapshot(request, new_state))


@router.post("/api/race-start/abandon")
async def api_abandon(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    state = await _load_state(request)
    try:
        new_state = abandon(state)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _save_state(request, new_state)
    await audit(request, "race_start.abandon", user=user)
    return JSONResponse(await _build_snapshot(request, new_state))


@router.post("/api/race-start/reset")
async def api_reset(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    new_state = reset()
    await _save_state(request, new_state)
    await audit(request, "race_start.reset", user=user)
    return JSONResponse(await _build_snapshot(request, new_state))


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
        # Fall back to the latest boat position from the GPS feed.
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
    state = await _load_state(request)
    return JSONResponse(await _build_snapshot(request, state))


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
    """Compute live line metrics from a boat snapshot.

    Posted from JS so we can keep the heavy geometry pure-Python and out of
    the browser. Returns ``null`` for fields that can't be computed
    (per EARS §E in the spec)."""
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
