"""Route handlers for auth."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger

from helmlog.auth import (
    generate_token,
    hash_password,
    reset_token_expires_at,
    session_expires_at,
    verify_password,
)
from helmlog.routes._helpers import audit, get_storage, limiter, templates
from helmlog.routes.me import _login_ctx

router = APIRouter()


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    get_storage(request)
    return templates.TemplateResponse(request, "login.html", _login_ctx(next))


@router.post("/auth/login", include_in_schema=False)
@limiter.limit("5/minute")
async def auth_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
) -> Response:
    storage = get_storage(request)

    def _login_err(msg: str) -> HTMLResponse:
        ctx = _login_ctx(next, f'<p style="color:var(--danger, #f87171);margin-top:12px">{msg}</p>')
        return templates.TemplateResponse(request, "login.html", ctx, status_code=400)

    email = email.strip().lower()
    if not email or not password:
        return _login_err("Email and password are required.")

    user = await storage.get_user_by_email(email)
    if user is None:
        return _login_err("Invalid email or password.")

    if not user.get("is_active", 1):
        return _login_err("Account is deactivated.")

    cred = await storage.get_credential(user["id"], "password")
    if cred is None or not cred.get("password_hash"):
        return _login_err("Invalid email or password.")

    if not verify_password(password, cred["password_hash"]):
        return _login_err("Invalid email or password.")

    session_id = generate_token()
    await storage.create_session(
        session_id,
        user["id"],
        session_expires_at(),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    response = RedirectResponse(url=next if next.startswith("/") else "/", status_code=303)
    response.set_cookie(
        "session",
        session_id,
        httponly=True,
        samesite="lax",
        max_age=int(os.getenv("AUTH_SESSION_TTL_DAYS", "90")) * 86400,
    )
    await audit(request, "auth.login", detail=email)

    from helmlog.email import send_device_alert, smtp_configured

    if smtp_configured():
        asyncio.ensure_future(
            send_device_alert(
                email,
                request.client.host if request.client else None,
                request.headers.get("user-agent"),
            )
        )

    return response


@router.get("/auth/accept-invite", response_class=HTMLResponse, include_in_schema=False)
async def accept_invite_page(request: Request, token: str = "") -> HTMLResponse:
    storage = get_storage(request)
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from helmlog.oauth import enabled_providers

    inv = await storage.get_invitation(token)
    if inv is None or inv["accepted_at"] is not None or inv["revoked_at"] is not None:
        return HTMLResponse("<h1>Invalid or expired invitation.</h1>", status_code=400)
    if _dt.now(_UTC) > _dt.fromisoformat(inv["expires_at"]):
        return HTMLResponse("<h1>Invitation has expired.</h1>", status_code=400)

    return templates.TemplateResponse(
        request,
        "auth/register.html",
        {
            "token": token,
            "email": inv["email"],
            "name": inv.get("name") or "",
            "role": inv["role"],
            "error_html": "",
            "oauth_providers": enabled_providers(),
        },
    )


@router.post("/auth/register", include_in_schema=False)
@limiter.limit("5/minute")
async def auth_register(
    request: Request,
    token: str = Form(...),
    email: str = Form(...),
    name: str = Form(default=""),
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> Response:
    storage = get_storage(request)
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from helmlog.oauth import enabled_providers

    def _reg_err(msg: str) -> HTMLResponse:
        return HTMLResponse(
            f"<h1>{msg}</h1><p><a href='/auth/accept-invite?token={token}'>Try again</a></p>",
            status_code=400,
        )

    inv = await storage.get_invitation(token)
    if inv is None or inv["accepted_at"] is not None or inv["revoked_at"] is not None:
        return HTMLResponse("<h1>Invalid or expired invitation.</h1>", status_code=400)
    if _dt.now(_UTC) > _dt.fromisoformat(inv["expires_at"]):
        return HTMLResponse("<h1>Invitation has expired.</h1>", status_code=400)

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {
                "token": token,
                "email": inv["email"],
                "name": name,
                "role": inv["role"],
                "error_html": '<p style="color:var(--danger, #f87171);margin-top:12px">Passwords do not match.</p>',
                "oauth_providers": enabled_providers(),
            },
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {
                "token": token,
                "email": inv["email"],
                "name": name,
                "role": inv["role"],
                "error_html": '<p style="color:var(--danger, #f87171);margin-top:12px">Password must be at least 8 characters.</p>',
                "oauth_providers": enabled_providers(),
            },
            status_code=400,
        )

    # Create user (or find existing pre-created inactive user from invitation)
    clean_name = name.strip() or inv.get("name") or None
    user = await storage.get_user_by_email(inv["email"])
    if user is None:
        user_id = await storage.create_user(
            inv["email"],
            clean_name,
            inv["role"],
            is_developer=bool(inv.get("is_developer")),
        )
    else:
        user_id = user["id"]
        # Activate the pre-created user and update name if provided
        await storage.activate_user(user_id)
        if clean_name:
            await storage.update_user_profile(user_id, clean_name, None)

    # Create password credential
    pw_hash = hash_password(password)
    existing_cred = await storage.get_credential(user_id, "password")
    if existing_cred is None:
        await storage.create_credential(user_id, "password", inv["email"], pw_hash)

    # Accept the invitation
    await storage.accept_invitation(token)

    # Create session
    session_id = generate_token()
    await storage.create_session(
        session_id,
        user_id,
        session_expires_at(),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "session",
        session_id,
        httponly=True,
        samesite="lax",
        max_age=int(os.getenv("AUTH_SESSION_TTL_DAYS", "90")) * 86400,
    )
    await audit(request, "auth.register", detail=f"{inv['email']} as {inv['role']}")
    return response


@router.get("/auth/forgot-password", response_class=HTMLResponse, include_in_schema=False)
async def forgot_password_page(request: Request) -> HTMLResponse:
    get_storage(request)
    return templates.TemplateResponse(request, "auth/forgot_password.html", {"message_html": ""})


@router.post("/auth/forgot-password", include_in_schema=False)
@limiter.limit("3/minute")
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
) -> HTMLResponse:
    storage = get_storage(request)
    _generic_msg = '<p style="color:var(--success, #34d399);margin-top:12px">If an account exists for that email, a reset link has been sent.</p>'
    email = email.strip().lower()

    from helmlog.email import smtp_configured

    if not email or not smtp_configured():
        return templates.TemplateResponse(
            request, "auth/forgot_password.html", {"message_html": _generic_msg}
        )

    user = await storage.get_user_by_email(email)
    if user is None:
        return templates.TemplateResponse(
            request, "auth/forgot_password.html", {"message_html": _generic_msg}
        )

    token = generate_token()
    await storage.create_password_reset_token(token, user["id"], reset_token_expires_at())
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    reset_url = f"{public_url}/auth/reset-password?token={token}"

    from helmlog.email import send_password_reset_email

    asyncio.ensure_future(send_password_reset_email(user.get("name"), email, reset_url))
    await audit(request, "auth.forgot_password", detail=email)

    return templates.TemplateResponse(
        request, "auth/forgot_password.html", {"message_html": _generic_msg}
    )


@router.get("/auth/reset-password", response_class=HTMLResponse, include_in_schema=False)
async def reset_password_page(request: Request, token: str = "") -> HTMLResponse:
    storage = get_storage(request)
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    row = await storage.get_password_reset_token(token)
    if row is None or row["used_at"] is not None:
        return HTMLResponse("<h1>Invalid or expired reset link.</h1>", status_code=400)
    if _dt.now(_UTC) > _dt.fromisoformat(row["expires_at"]):
        return HTMLResponse("<h1>Reset link has expired.</h1>", status_code=400)

    return templates.TemplateResponse(
        request, "auth/reset_password.html", {"token": token, "error_html": ""}
    )


@router.post("/auth/reset-password", include_in_schema=False)
@limiter.limit("5/minute")
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> Response:
    storage = get_storage(request)
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    row = await storage.get_password_reset_token(token)
    if row is None or row["used_at"] is not None:
        return HTMLResponse("<h1>Invalid or expired reset link.</h1>", status_code=400)
    if _dt.now(_UTC) > _dt.fromisoformat(row["expires_at"]):
        return HTMLResponse("<h1>Reset link has expired.</h1>", status_code=400)

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "auth/reset_password.html",
            {
                "token": token,
                "error_html": '<p style="color:var(--danger, #f87171);margin-top:12px">Passwords do not match.</p>',
            },
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "auth/reset_password.html",
            {
                "token": token,
                "error_html": '<p style="color:var(--danger, #f87171);margin-top:12px">Password must be at least 8 characters.</p>',
            },
            status_code=400,
        )

    pw_hash = hash_password(password)
    await storage.update_password_hash(row["user_id"], pw_hash)
    await storage.use_password_reset_token(token)
    await audit(request, "auth.reset_password", detail=f"user_id={row['user_id']}")

    return RedirectResponse(url="/login", status_code=303)


@router.get("/auth/oauth/{provider}", include_in_schema=False)
async def oauth_login(
    request: Request, provider: str, next: str = "/", token: str = ""
) -> Response:
    get_storage(request)
    from helmlog.oauth import oauth as _oauth

    client = _oauth.create_client(provider)
    if client is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider}")
    redirect_uri = str(request.base_url).rstrip("/") + f"/auth/oauth/{provider}/callback"
    # Store next and token in session for the callback
    request.session["oauth_next"] = next
    request.session["oauth_token"] = token
    return await client.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]


@router.get("/auth/oauth/{provider}/callback", include_in_schema=False)
async def oauth_callback(request: Request, provider: str) -> Response:
    storage = get_storage(request)
    from helmlog.oauth import oauth as _oauth

    client = _oauth.create_client(provider)
    if client is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider: {provider}")

    try:
        tok = await client.authorize_access_token(request)
    except Exception as exc:
        logger.warning("OAuth token exchange failed for {}: {}", provider, exc)
        return HTMLResponse(
            "<h1>OAuth login failed.</h1><p>Could not complete sign-in. Please try again.</p>",
            status_code=400,
        )

    if provider == "google":
        user_info = tok.get("userinfo", {})
        provider_email = user_info.get("email", "")
        provider_uid = user_info.get("sub", "")
        provider_name = user_info.get("name")
    elif provider == "github":
        try:
            resp = await client.get("user")
            user_info = resp.json()
            provider_email = user_info.get("email", "")
            provider_uid = str(user_info.get("id", ""))
            provider_name = user_info.get("name")
            if not provider_email:
                email_resp = await client.get("user/emails")
                for e in email_resp.json():
                    if e.get("primary"):
                        provider_email = e["email"]
                        break
        except Exception as exc:
            logger.warning("GitHub API call failed: {}", exc)
            return HTMLResponse(
                "<h1>OAuth login failed.</h1><p>Could not retrieve GitHub profile. Please try again.</p>",
                status_code=400,
            )
    else:  # apple
        user_info = tok.get("userinfo", {})
        provider_email = user_info.get("email", "")
        provider_uid = user_info.get("sub", "")
        provider_name = user_info.get("name")

    if not provider_uid:
        logger.warning("OAuth provider {} returned empty user ID", provider)
        return HTMLResponse(
            "<h1>OAuth login failed.</h1><p>Provider did not return a user identifier.</p>",
            status_code=400,
        )

    # Look up existing credential
    cred = await storage.get_credential_by_provider_uid(provider, provider_uid)

    invite_token = request.session.pop("oauth_token", "")
    next_url = request.session.pop("oauth_next", "/")

    if cred:
        # Existing user — create session
        user_id = cred["user_id"]
    elif invite_token:
        # Registering via invitation + OAuth
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        inv = await storage.get_invitation(invite_token)
        if inv is None or inv["accepted_at"] or inv["revoked_at"]:
            return HTMLResponse("<h1>Invalid invitation.</h1>", status_code=400)
        if _dt.now(_UTC) > _dt.fromisoformat(inv["expires_at"]):
            return HTMLResponse("<h1>Invitation expired.</h1>", status_code=400)

        user = await storage.get_user_by_email(inv["email"])
        if user is None:
            user_id = await storage.create_user(
                inv["email"],
                provider_name,
                inv["role"],
                is_developer=bool(inv.get("is_developer")),
            )
        else:
            user_id = user["id"]
            # Activate the pre-created user and update name if provided
            await storage.activate_user(user_id)
            if provider_name:
                await storage.update_user_profile(user_id, provider_name, None)
        await storage.create_credential(user_id, provider, provider_uid, None)
        await storage.accept_invitation(invite_token)
        await audit(request, "auth.register_oauth", detail=f"{inv['email']} via {provider}")
    else:
        # Try to link to existing user by email
        user = await storage.get_user_by_email(provider_email)
        if user is None:
            return HTMLResponse(
                "<h1>No account found. You must be invited first.</h1>", status_code=403
            )
        user_id = user["id"]
        await storage.create_credential(user_id, provider, provider_uid, None)

    session_id = generate_token()
    await storage.create_session(
        session_id,
        user_id,
        session_expires_at(),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    response = RedirectResponse(url=next_url if next_url.startswith("/") else "/", status_code=303)
    response.set_cookie(
        "session",
        session_id,
        httponly=True,
        samesite="lax",
        max_age=int(os.getenv("AUTH_SESSION_TTL_DAYS", "90")) * 86400,
    )
    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    storage = get_storage(request)
    from http.cookies import SimpleCookie

    raw_cookie = request.headers.get("cookie", "")
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(raw_cookie)
    if "session" in cookie:
        await storage.delete_session(cookie["session"].value)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    response.delete_cookie("coach_link")  # coach share link (#849)
    return response
