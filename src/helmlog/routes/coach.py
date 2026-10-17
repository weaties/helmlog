"""Coach access routes (#848): the coach's landing page + admin grant management.

Policy and decision tables: spec on issue #848. Grants are admin-only; the
coach principal sees only ``/coach`` and the allowlisted race pages.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from helmlog.auth import (
    COACH_ROLE,
    coach_scope,
    generate_token,
    invite_expires_at,
    is_coach,
    require_auth,
    require_reader,
)
from helmlog.coach import grant_state, validate_grant_window
from helmlog.routes._helpers import audit, coach_json, get_storage, templates, tpl_ctx

router = APIRouter()


# ---------------------------------------------------------------------------
# Coach-facing
# ---------------------------------------------------------------------------


async def _coach_sessions(
    storage: Any,  # noqa: ANN401
    user: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """The coach's granted races (coach-safe fields) and latest grant expiry."""
    scope = coach_scope(user)
    if not scope.grant_ids:
        return [], None
    rows = await storage.list_races_for_coach(
        None if scope.all_sessions else sorted(scope.session_ids)
    )
    first_video = await _first_video_urls(storage, [r["id"] for r in rows])
    sessions = []
    for r in rows:
        if not scope.can_read(r["id"]):
            continue
        item = {
            k: r[k]
            for k in ("id", "name", "event", "race_num", "date", "start_utc", "end_utc", "slug")
        }
        item["type"] = r["session_type"]
        if scope.can_video(r["id"]) and first_video.get(r["id"]):
            item["first_video_url"] = first_video[r["id"]]
        sessions.append(item)
    expires: str | None = None
    if user.get("coach_via") == "account":
        grants = await storage.list_coach_grants_for_email(user["email"])
        expires = max((g["expires_at"] for g in grants if g["id"] in scope.grant_ids), default=None)
    return sessions, expires


@router.get("/coach", response_class=HTMLResponse, include_in_schema=False)
async def coach_landing(request: Request) -> Response:
    """Races shared with the signed-in coach. Staff are sent home."""
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    if user is None or not is_coach(user):
        return RedirectResponse(url="/", status_code=303)
    sessions, expires = await _coach_sessions(get_storage(request), user)
    return templates.TemplateResponse(
        request,
        "coach.html",
        tpl_ctx(request, "/coach", sessions=sessions, expires_at=expires),
    )


@router.get("/api/coach/sessions")
async def api_coach_sessions(
    request: Request,
    user: dict[str, Any] = Depends(require_reader()),  # noqa: B008
) -> JSONResponse:
    """The coach's granted races (spec R9). Coach principals only."""
    if not is_coach(user):
        raise HTTPException(status_code=403, detail="Coach principals only")
    sessions, expires = await _coach_sessions(get_storage(request), user)
    return coach_json({"sessions": sessions, "expires_at": expires})


@router.get("/api/coach/timezone")
async def api_coach_timezone(
    request: Request,
    _user: dict[str, Any] = Depends(require_reader()),  # noqa: B008
) -> JSONResponse:
    """The boat's display timezone. Coaches can't read /api/state (live data)."""
    from helmlog.routes._helpers import SETTINGS_BY_KEY
    from helmlog.storage import get_effective_setting

    tz_def = SETTINGS_BY_KEY.get("TIMEZONE")
    tz = await get_effective_setting(
        get_storage(request), "TIMEZONE", tz_def.default if tz_def else "UTC"
    )
    return JSONResponse({"timezone": tz})


async def _first_video_urls(storage: Any, race_ids: list[int]) -> dict[int, str]:  # noqa: ANN401
    if not race_ids:
        return {}
    placeholders = ",".join("?" * len(race_ids))
    cur = await storage._conn().execute(  # noqa: SLF001
        f"SELECT race_id, MIN(youtube_url) AS url FROM race_videos"
        f" WHERE race_id IN ({placeholders}) GROUP BY race_id",
        race_ids,
    )
    return {int(r["race_id"]): r["url"] for r in await cur.fetchall()}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class GrantCreate(BaseModel):
    label: str
    coach_email: str = ""
    all_sessions: bool = False
    allow_video: bool = False
    session_ids: list[int] = []
    starts_at: str | None = None
    expires_at: str


class GrantUpdate(BaseModel):
    label: str
    all_sessions: bool = False
    allow_video: bool = False
    session_ids: list[int] = []
    expires_at: str


def _parse_ts(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{field} must be an ISO-8601 datetime"
        ) from None
    if dt.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{field} must include a timezone")
    return dt.astimezone(UTC)


async def _validate_common(
    storage: Any,  # noqa: ANN401
    *,
    label: str,
    all_sessions: bool,
    session_ids: list[int],
    starts_at: datetime,
    expires_at: datetime,
) -> list[int]:
    if not label.strip():
        raise HTTPException(status_code=422, detail="label must not be blank")
    try:
        validate_grant_window(starts_at, expires_at, datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if all_sessions:
        return []
    ids = sorted(set(session_ids))
    if not ids:
        raise HTTPException(status_code=422, detail="choose at least one race, or all races")
    peer_ids = await storage.list_peer_race_ids()
    for sid in ids:
        if await storage.get_race(sid) is None:
            raise HTTPException(status_code=422, detail=f"unknown race id {sid}")
        if sid in peer_ids:
            # Policy: per-boat opt-in — another co-op boat's race isn't ours to share.
            raise HTTPException(
                status_code=422, detail=f"race {sid} belongs to another boat and can't be shared"
            )
    return ids


def _public_grant(g: dict[str, Any], now: datetime) -> dict[str, Any]:
    out = {k: v for k, v in g.items() if k != "token_hash"}
    out["state"] = grant_state(g, now)
    return out


@router.get("/admin/coaches", response_class=HTMLResponse, include_in_schema=False)
async def admin_coaches_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    return templates.TemplateResponse(
        request, "admin/coaches.html", tpl_ctx(request, "/admin/coaches")
    )


@router.get("/api/admin/coach-grants")
async def api_list_coach_grants(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    now = datetime.now(UTC)
    grants = await get_storage(request).list_coach_grants()
    return JSONResponse({"grants": [_public_grant(g, now) for g in grants]})


@router.post("/api/admin/coach-grants", status_code=201)
async def api_create_coach_grant(
    request: Request,
    body: GrantCreate,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Create an account grant and, if the coach has no account yet, an invite."""
    storage = get_storage(request)
    email = body.coach_email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="coach_email must be an email address")
    starts_at = _parse_ts(body.starts_at, "starts_at") if body.starts_at else datetime.now(UTC)
    expires_at = _parse_ts(body.expires_at, "expires_at")
    session_ids = await _validate_common(
        storage,
        label=body.label,
        all_sessions=body.all_sessions,
        session_ids=body.session_ids,
        starts_at=starts_at,
        expires_at=expires_at,
    )
    existing = await storage.get_user_by_email(email)
    if existing is not None and existing["role"] != COACH_ROLE:
        raise HTTPException(
            status_code=409,
            detail=f"{email} is already a {existing['role']} user — coaches need their own account",
        )
    grant_id = await storage.create_coach_grant(
        kind="account",
        label=body.label.strip(),
        coach_email=email,
        token_hash=None,
        all_sessions=body.all_sessions,
        allow_video=body.allow_video,
        landing_path=None,
        starts_at=starts_at.isoformat(),
        expires_at=expires_at.isoformat(),
        created_by=user["id"],
        session_ids=session_ids,
    )
    invite_url = None
    if existing is None:
        # Deliberately no pre-created inactive user (unlike crew invites):
        # coaches must never appear in crew selectors.
        token = generate_token()
        await storage.create_invitation(
            token, email, COACH_ROLE, body.label.strip(), False, user["id"], invite_expires_at()
        )
        invite_url = f"{str(request.base_url).rstrip('/')}/auth/accept-invite?token={token}"
    await audit(
        request,
        "coach.grant_create",
        detail=json.dumps(
            {
                "grant_id": grant_id,
                "kind": "account",
                "coach_email": email,
                "all_sessions": body.all_sessions,
                "session_ids": session_ids,
                "allow_video": body.allow_video,
                "expires_at": expires_at.isoformat(),
            }
        ),
        user=user,
    )
    return JSONResponse({"id": grant_id, "invite_url": invite_url}, status_code=201)


async def _editable_grant(storage: Any, grant_id: int) -> dict[str, Any]:  # noqa: ANN401
    grant = await storage.get_coach_grant(grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    if grant_state(grant, datetime.now(UTC)) in ("expired", "revoked"):
        raise HTTPException(status_code=409, detail="Expired or revoked grants can't change")
    return grant  # type: ignore[no-any-return]


@router.put("/api/admin/coach-grants/{grant_id}", status_code=204)
async def api_update_coach_grant(
    request: Request,
    grant_id: int,
    body: GrantUpdate,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    grant = await _editable_grant(storage, grant_id)
    expires_at = _parse_ts(body.expires_at, "expires_at")
    session_ids = await _validate_common(
        storage,
        label=body.label,
        all_sessions=body.all_sessions,
        session_ids=body.session_ids,
        starts_at=datetime.fromisoformat(grant["starts_at"]),
        expires_at=expires_at,
    )
    await storage.update_coach_grant(
        grant_id,
        label=body.label.strip(),
        all_sessions=body.all_sessions,
        allow_video=body.allow_video,
        expires_at=expires_at.isoformat(),
        session_ids=session_ids,
    )
    await audit(
        request,
        "coach.grant_update",
        detail=json.dumps(
            {
                "grant_id": grant_id,
                "all_sessions": body.all_sessions,
                "session_ids": session_ids,
                "allow_video": body.allow_video,
                "expires_at": expires_at.isoformat(),
            }
        ),
        user=user,
    )


@router.post("/api/admin/coach-grants/{grant_id}/revoke", status_code=204)
async def api_revoke_coach_grant(
    request: Request,
    grant_id: int,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    if await storage.get_coach_grant(grant_id) is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    await storage.revoke_coach_grant(grant_id, datetime.now(UTC).isoformat())
    await audit(request, "coach.grant_revoke", detail=json.dumps({"grant_id": grant_id}), user=user)
