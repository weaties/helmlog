"""Route handlers for admin."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from helmlog.auth import generate_token, invite_expires_at, require_auth
from helmlog.routes._helpers import (
    audit,
    get_storage,
    get_web_cache,
    limiter,
    templates,
    tpl_ctx,
)

router = APIRouter()


@router.get("/admin/boats", response_class=HTMLResponse, include_in_schema=False)
async def admin_boats_page(request: Request) -> Response:
    get_storage(request)
    return templates.TemplateResponse(request, "admin/boats.html", tpl_ctx(request, "/admin/boats"))


@router.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
async def admin_users_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    from helmlog.races import configured_tz

    tz = configured_tz()
    users = await storage.list_users()
    sessions = await storage.list_auth_sessions()
    pending_invitations = await storage.list_pending_invitations()
    await storage.delete_expired_sessions()

    def _local_ts(utc_str: str | None) -> str:
        if not utc_str:
            return "\u2014"
        try:
            from datetime import datetime as _dt

            dt = _dt.fromisoformat(utc_str).astimezone(tz)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return utc_str[:19]

    role_colors = {"admin": "#f59e0b", "crew": "#34d399", "viewer": "#60a5fa"}

    def _badge(role: str) -> str:
        color = role_colors.get(role, "#8892a4")
        return f'<span style="background:{color}22;color:{color};padding:1px 7px;border-radius:4px;font-size:.75rem">{role}</span>'

    def _esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    def _fmt_weight(w: float | None) -> str:
        return f"{w:.1f} lbs" if w else "\u2014"

    my_id = _user.get("id")

    def _del_btn(uid: int) -> str:
        if uid == my_id:
            return ""
        return (
            f' <button onclick="deleteUser({uid})" class="ubtn"'
            ' style="border-color:var(--danger);color:var(--danger)">Delete</button>'
        )

    user_rows = "".join(
        f'<tr data-uid="{u["id"]}">'
        f'<td class="u-email" data-label="Email">{_esc(u["email"])}</td>'
        f'<td class="u-name" data-label="Name">{_esc(u["name"] or "")}</td>'
        f'<td class="u-role" data-label="Role" data-role="{u["role"]}">{_badge(u["role"])}</td>'
        f'<td class="u-dev" data-label="Dev"><input type="checkbox" {"checked" if u.get("is_developer") else ""} disabled style="width:18px;height:18px"/></td>'  # noqa: E501
        f'<td class="u-weight" data-label="Weight">{_fmt_weight(u.get("weight_lbs"))}</td>'
        f'<td data-label="Last seen">{_local_ts(u["last_seen"])}</td>'
        f'<td class="u-actions"><button onclick="editUser({u["id"]})" class="ubtn ubtn-edit" style="border-color:var(--success);color:var(--success)">Edit</button>'  # noqa: E501
        f"{_del_btn(u['id'])}</td>"  # noqa: E501
        f"</tr>"
        for u in users
    )
    sess_rows = "".join(
        f'<tr><td data-label="User">{_esc(s.get("email") or "")}</td>'
        f'<td data-label="Role">{_esc(s.get("role") or "")}</td>'
        f'<td data-label="IP">{_esc(s.get("ip") or "\u2014")}</td>'
        f'<td data-label="Created">{_local_ts(s["created_at"])}</td>'
        f'<td data-label="Expires">{_local_ts(s["expires_at"])}</td>'
        f'<td><button onclick="revokeSession(\'{_esc(s["session_id"])}\')" style="cursor:pointer;background:var(--danger);border:none;color:var(--bg-primary);border-radius:4px;padding:6px 12px;font-size:.85rem">Revoke</button></td>'  # noqa: E501
        f"</tr>"
        for s in sessions
    )
    invite_rows = "".join(
        f'<tr><td data-label="Email">{_esc(inv["email"])}</td>'
        f'<td data-label="Name">{_esc(inv.get("name") or "\u2014")}</td>'
        f'<td data-label="Role">{_badge(inv["role"])}</td>'
        f'<td data-label="Dev">{"&#9989;" if inv.get("is_developer") else "\u2014"}</td>'
        f'<td data-label="Expires">{_local_ts(inv["expires_at"])}</td>'
        f'<td><button onclick="revokeInvite({int(inv["id"])})" style="cursor:pointer;background:var(--danger);border:none;color:var(--bg-primary);border-radius:4px;padding:6px 12px;font-size:.85rem">Revoke</button></td>'  # noqa: E501
        f"</tr>"
        for inv in pending_invitations
    )
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        tpl_ctx(
            request,
            "/admin/users",
            user_rows=user_rows,
            session_rows=sess_rows,
            invite_rows=invite_rows,
        ),
    )


@router.post("/admin/users/invite", status_code=201, include_in_schema=False)
@limiter.limit("5/minute")
async def admin_invite_user(
    request: Request,
    email: str = Form(...),
    role: str = Form(...),
    name: str = Form(default=""),
    is_developer: str = Form(default=""),
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    if role not in ("admin", "crew", "viewer"):
        raise HTTPException(status_code=422, detail="Invalid role")
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="email must not be blank")
    clean_name = name.strip() or None
    dev_flag = is_developer in ("1", "on", "true")
    token = generate_token()
    base = str(request.base_url).rstrip("/")
    await storage.create_invitation(
        token, email, role, clean_name, dev_flag, _user["id"], invite_expires_at()
    )
    # Pre-create an inactive user so they appear in crew selectors before accepting
    existing = await storage.get_user_by_email(email)
    if existing is None:
        await storage.create_user(email, clean_name, role, is_developer=dev_flag, is_active=False)
    invite_url = f"{base}/auth/accept-invite?token={token}"
    dev_label = " +developer" if dev_flag else ""
    await audit(request, "user.invite", detail=f"{email} as {role}{dev_label}", user=_user)

    from helmlog.email import send_welcome_email, smtp_configured

    email_sent = False
    if smtp_configured() and email:
        email_sent = await send_welcome_email(clean_name, email, role, invite_url)

    return JSONResponse(
        {"invite_url": invite_url, "token": token, "email_sent": email_sent},
        status_code=201,
    )


@router.post("/admin/invitations/{invitation_id}/revoke", status_code=204, include_in_schema=False)
async def admin_revoke_invitation(
    request: Request,
    invitation_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    await storage.revoke_invitation(invitation_id)
    await audit(request, "invitation.revoke", detail=f"id={invitation_id}", user=_user)


@router.put("/admin/users/{user_id}/role", status_code=204, include_in_schema=False)
async def admin_update_role(
    request: Request,
    user_id: int,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    role = (body.get("role") or "").strip()
    if role not in ("admin", "crew", "viewer"):
        raise HTTPException(status_code=422, detail="Invalid role")
    user = await storage.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await storage.update_user_role(user_id, role)
    await audit(request, "user.role", detail=f"user={user_id} role={role}", user=_user)


@router.put("/admin/users/{user_id}/developer", status_code=204, include_in_schema=False)
async def admin_update_developer(
    request: Request,
    user_id: int,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    user = await storage.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    is_dev = bool(body.get("is_developer"))
    await storage.update_user_developer(user_id, is_dev)
    await audit(
        request, "user.developer", detail=f"user={user_id} is_developer={is_dev}", user=_user
    )


@router.put("/admin/users/{user_id}", status_code=204, include_in_schema=False)
async def admin_update_user(
    request: Request,
    user_id: int,
    body: dict[str, Any],
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Update a user's name and/or email."""
    storage = get_storage(request)
    user = await storage.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    name = body.get("name")
    email = body.get("email")
    if email is not None:
        email = email.strip()
        if not email:
            raise HTTPException(status_code=422, detail="email must not be blank")
        # Check for email conflict
        existing = await storage.get_user_by_email(email)
        if existing and existing["id"] != user_id:
            raise HTTPException(status_code=409, detail="Email already in use")
    await storage.update_user_profile(user_id, name, email)
    # Role update
    role = body.get("role")
    if role is not None:
        if role not in ("viewer", "crew", "admin"):
            raise HTTPException(status_code=422, detail="Invalid role")
        await storage.update_user_role(user_id, role)
    # Developer flag update
    if "is_developer" in body:
        await storage.update_user_developer(user_id, bool(body["is_developer"]))
    # Weight update (admin bypass — no biometric consent required from admin)
    if "weight_lbs" in body:
        w = body["weight_lbs"]
        weight_val = float(w) if w is not None and w != "" else None
        await storage.update_user_weight(user_id, weight_val)
    changes = []
    if name is not None:
        changes.append(f"name={name!r}")
    if email is not None:
        changes.append(f"email={email!r}")
    if role is not None:
        changes.append(f"role={role!r}")
    if "is_developer" in body:
        changes.append(f"is_developer={body['is_developer']!r}")
    if "weight_lbs" in body:
        changes.append(f"weight_lbs={body['weight_lbs']!r}")
    await audit(request, "user.update", detail=f"user={user_id} {' '.join(changes)}", user=_user)


@router.delete("/admin/sessions/{session_id}", status_code=204, include_in_schema=False)
async def admin_revoke_session(
    request: Request,
    session_id: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    await storage.delete_session(session_id)
    await audit(request, "session.revoke", detail=session_id[:16], user=_user)


@router.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
async def admin_audit_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    from helmlog.races import configured_tz

    tz = configured_tz()

    def _local_ts(utc_str: str) -> str:
        try:
            from datetime import datetime as _dt

            dt = _dt.fromisoformat(utc_str).astimezone(tz)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return utc_str[:19]

    entries = await storage.list_audit_log(limit=200)
    audit_rows = "".join(
        f"<tr><td>{_local_ts(e['ts'])}</td>"
        f"<td>{e.get('user_name') or e.get('user_email') or '—'}</td>"
        f"<td><code>{e['action']}</code></td>"
        f'<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{e.get("detail") or ""}</td>'  # noqa: E501
        f'<td style="font-size:.7rem">{e.get("ip_address") or ""}</td></tr>'
        for e in entries
    )
    return templates.TemplateResponse(
        request,
        "admin/audit.html",
        tpl_ctx(request, "/admin/audit", audit_rows=audit_rows, has_entries=bool(entries)),
    )


@router.get("/api/audit")
async def api_audit_log(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    entries = await storage.list_audit_log(limit=limit, offset=offset)
    return JSONResponse(entries)


@router.get("/admin/cameras", response_class=HTMLResponse, include_in_schema=False)
async def admin_cameras_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request, "admin/cameras.html", tpl_ctx(request, "/admin/cameras")
    )


@router.get("/admin/events", response_class=HTMLResponse, include_in_schema=False)
async def admin_events_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request, "admin/events.html", tpl_ctx(request, "/admin/events")
    )


@router.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
async def admin_settings_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    from helmlog.themes import PRESET_ORDER, PRESETS

    preset_list = [{"id": pid, "name": PRESETS[pid].name} for pid in PRESET_ORDER if pid in PRESETS]
    custom_list = await storage.list_color_schemes()
    boat_default = await storage.get_setting("color_scheme_default") or ""
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        tpl_ctx(
            request,
            "/admin/settings",
            preset_schemes=preset_list,
            custom_schemes=custom_list,
            boat_default=boat_default,
        ),
    )


@router.get("/admin/network", response_class=HTMLResponse, include_in_schema=False)
async def admin_network_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request, "admin/network.html", tpl_ctx(request, "/admin/network")
    )


@router.get("/admin/deployment", response_class=HTMLResponse, include_in_schema=False)
async def admin_deployment_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request, "admin/deployment.html", tpl_ctx(request, "/admin/deployment")
    )


@router.get("/admin/federation", response_class=HTMLResponse, include_in_schema=False)
async def admin_federation_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request,
        "admin/federation.html",
        tpl_ctx(request, "/admin/federation"),
    )


@router.get("/admin/analysis", response_class=HTMLResponse, include_in_schema=False)
async def admin_analysis_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request,
        "admin/analysis.html",
        tpl_ctx(request, "/admin/analysis"),
    )


@router.get("/admin/vakaros", response_class=HTMLResponse, include_in_schema=False)
async def admin_vakaros_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Vakaros VKX inbox + ingested-sessions admin page (#458)."""
    from helmlog.vakaros_inbox import get_inbox_dir, list_inbox_files

    storage = get_storage(request)
    inbox = get_inbox_dir()
    inbox_files = [{"name": p.name, "size": p.stat().st_size} for p in list_inbox_files(inbox)]
    sessions = await storage.list_vakaros_sessions()
    # Surface any flash message from a prior POST (filename + status + error).
    flash_filename = request.query_params.get("flash_filename")
    flash_status = request.query_params.get("flash_status")
    flash_error = request.query_params.get("flash_error")
    flash_rematch = request.query_params.get("flash_rematch")
    return templates.TemplateResponse(
        request,
        "admin/vakaros.html",
        tpl_ctx(
            request,
            "/admin/vakaros",
            inbox_dir=str(inbox),
            inbox_files=inbox_files,
            sessions=sessions,
            flash_filename=flash_filename,
            flash_status=flash_status,
            flash_error=flash_error,
            flash_rematch=flash_rematch,
        ),
    )


@router.post("/admin/vakaros/rematch", include_in_schema=False)
async def admin_vakaros_rematch(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Re-run matching for every stored Vakaros session (#458)."""
    from fastapi.responses import RedirectResponse

    storage = get_storage(request)
    results = await storage.rematch_all_vakaros_sessions()
    total_linked = sum(len(v) for v in results.values())
    session_count = len(results)
    await audit(
        request,
        "vakaros_rematch",
        detail=f"{session_count} sessions, {total_linked} race links",
        user=user,
    )
    from urllib.parse import quote

    msg = f"Rematched {session_count} sessions, linked {total_linked} races."
    return RedirectResponse(url="/admin/vakaros?flash_rematch=" + quote(msg), status_code=303)


@router.post("/admin/vakaros/ingest", include_in_schema=False)
async def admin_vakaros_ingest(
    request: Request,
    filename: str = Form(...),
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Parse, store, and archive one inbox file (#458)."""
    from fastapi.responses import RedirectResponse

    from helmlog.vakaros_inbox import get_inbox_dir, ingest_inbox_file

    storage = get_storage(request)
    inbox = get_inbox_dir()
    try:
        result = await ingest_inbox_file(storage, inbox, filename)
    except ValueError as exc:  # path-traversal
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await audit(
        request,
        "vakaros_ingest",
        detail=f"{result.filename} -> {result.status} (session_id={result.session_id})",
        user=user,
    )

    params: list[str] = [f"flash_filename={result.filename}", f"flash_status={result.status}"]
    if result.error:
        from urllib.parse import quote

        params.append(f"flash_error={quote(result.error)}")
    return RedirectResponse(url="/admin/vakaros?" + "&".join(params), status_code=303)


# ---------------------------------------------------------------------------
# Tags admin (#587 / #588 slice 3)
# ---------------------------------------------------------------------------


@router.get("/admin/tags", response_class=HTMLResponse, include_in_schema=False)
async def admin_tags_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(request, "admin/tags.html", tpl_ctx(request, "/admin/tags"))


# ---------------------------------------------------------------------------
# Web-cache observability (#594 / #611)
# ---------------------------------------------------------------------------


@router.get("/api/admin/cache/stats", include_in_schema=False)
async def admin_cache_stats(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Return per-family hit/miss/invalidate counters for the web response
    cache (EARS Req. 18). Also surfaces cache row counts by tier so we can
    spot unexpected growth on the test Pi.
    """
    cache = get_web_cache(request)
    if cache is None:
        return JSONResponse({"families": {}, "t1_entries": 0, "t2_rows": 0})

    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache")
    t2_row = await cur.fetchone()
    t2_rows = int(t2_row["n"]) if t2_row else 0

    # Avoid touching protected attrs from the route layer: expose via
    # small public helpers on WebCache.
    return JSONResponse(
        {
            "families": cache.stats(),
            "t1_entries": cache.t1_size(),
            "t2_rows": t2_rows,
        }
    )


@router.post("/api/admin/cache/stats/reset", status_code=204, include_in_schema=False)
async def admin_cache_stats_reset(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Zero the per-family counters. Useful when measuring before/after a
    specific user flow on the test Pi without restarting the service."""
    cache = get_web_cache(request)
    if cache is not None:
        cache.reset_stats()
    await audit(request, action="cache_stats_reset")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Instrument smoothing (#727)
# ---------------------------------------------------------------------------


@router.get("/api/admin/instrument-smoothing", response_class=JSONResponse, include_in_schema=False)
async def admin_instrument_smoothing_get(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Return the currently-effective tau (seconds) for each smoothed
    channel. Falls back to defaults when a channel hasn't been overridden
    in app_settings."""
    from helmlog.smoothing import DEFAULT_TAUS, parse_tau

    storage = get_storage(request)
    out: dict[str, dict[str, float]] = {}
    for channel, default in DEFAULT_TAUS.items():
        raw = await storage.get_setting(f"smoothing.{channel}.tau_s")
        out[channel] = {"tau_s": parse_tau(raw, default), "default": default}
    return JSONResponse(out)


@router.put("/api/admin/instrument-smoothing", status_code=204, include_in_schema=False)
async def admin_instrument_smoothing_put(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Persist new tau values for one or more channels then refresh the
    live smoothers. Body: ``{channel: tau_seconds, ...}``. Unknown
    channels are rejected; tau<=0 or NaN are rejected."""
    from helmlog.smoothing import DEFAULT_TAUS

    storage = get_storage(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")
    for channel, tau in body.items():
        if channel not in DEFAULT_TAUS:
            raise HTTPException(status_code=400, detail=f"Unknown channel: {channel}")
        try:
            v = float(tau)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"{channel}: not a number") from e
        if not (v > 0) or v != v:  # rejects 0, negatives, NaN
            raise HTTPException(status_code=400, detail=f"{channel}: tau must be > 0")
        await storage.set_setting(f"smoothing.{channel}.tau_s", str(v))
    await storage.refresh_smoothing()
    await audit(request, action="instrument_smoothing_update", detail=str(body))
    return Response(status_code=204)


@router.get("/admin/instruments", response_class=HTMLResponse, include_in_schema=False)
async def admin_instruments_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Admin page with a slider per smoothed channel."""
    return templates.TemplateResponse(
        request, "admin/instruments.html", tpl_ctx(request, "/admin/instruments")
    )
