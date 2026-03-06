"""FastAPI web interface for race marking.

Provides a mobile-optimised single-page app at http://<pi-hostname>:3002 that lets
crew tap a button to start/end races. The app factory pattern (create_app)
keeps this testable without running a live server.

Security:
  Magic-link invite-token auth with three roles: admin, crew, viewer.
  Set AUTH_DISABLED=true to bypass auth entirely (e.g. Tailscale-only deployments).
  See src/logger/auth.py and docs/https-deployment.md for details.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel

if TYPE_CHECKING:
    from logger.audio import AudioConfig, AudioRecorder
    from logger.storage import Storage

# ---------------------------------------------------------------------------
# Git version info — read once at import time
# ---------------------------------------------------------------------------


def _get_git_info() -> str:
    """Return 'branch @ shortsha · clean/dirty' from the current git repo."""
    import subprocess

    try:
        _repo = str(Path(__file__).resolve().parents[2])
        _git = ["git", "-c", f"safe.directory={_repo}"]

        def _run(args: list[str]) -> str:
            return subprocess.check_output(
                [*_git, *args], cwd=_repo, stderr=subprocess.DEVNULL, text=True
            ).strip()

        branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])
        sha = _run(["rev-parse", "--short=7", "HEAD"])

        # Check for uncommitted changes
        dirty = bool(_run(["status", "--porcelain"]))

        # Check for unpushed commits (best-effort; skip if no upstream)
        if not dirty:
            try:
                unpushed = _run(["rev-list", "@{upstream}..HEAD", "--count"])
                if int(unpushed) > 0:
                    dirty = True
            except Exception:  # noqa: BLE001
                pass  # no upstream configured

        status = "dirty" if dirty else "clean"
        return f"{branch} @ {sha} · {status}"
    except Exception:  # noqa: BLE001
        return ""


_GIT_INFO: str = _get_git_info()

# ---------------------------------------------------------------------------
# Jinja2 templates + static files
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@dataclass(frozen=True)
class _SettingDef:
    """Metadata for one admin-configurable setting."""

    key: str
    label: str
    input_type: str  # "text", "number", "select"
    default: str
    help_text: str = ""
    options: tuple[str, ...] = ()
    sensitive: bool = False


_SETTINGS_DEFS: tuple[_SettingDef, ...] = (
    _SettingDef(
        key="TRANSCRIBE_URL",
        label="Remote transcription URL",
        input_type="text",
        default="",
        help_text="Base URL for the remote transcription worker (e.g. http://macbook:8321). Leave blank for local transcription.",
    ),
    _SettingDef(
        key="WHISPER_MODEL",
        label="Whisper model size",
        input_type="select",
        default="base",
        options=("tiny", "base", "small", "medium", "large"),
        help_text="Larger models are more accurate but slower.",
    ),
    _SettingDef(
        key="PI_API_URL",
        label="Pi API URL",
        input_type="text",
        default="http://corvopi:3002",
        help_text="Base URL for the J105 Logger API (used by the video pipeline).",
    ),
    _SettingDef(
        key="TIMEZONE",
        label="Display timezone",
        input_type="text",
        default="America/Los_Angeles",
        help_text="IANA timezone name for display (e.g. America/Los_Angeles).",
    ),
    _SettingDef(
        key="VIDEO_PRIVACY",
        label="YouTube upload privacy",
        input_type="select",
        default="unlisted",
        options=("private", "unlisted", "public"),
        help_text="Privacy status for auto-uploaded YouTube videos.",
    ),
    _SettingDef(
        key="PI_SESSION_COOKIE",
        label="Pi session cookie",
        input_type="text",
        default="",
        sensitive=True,
        help_text="Session cookie for the Pi API (used by the video pipeline to link videos to sessions).",
    ),
    _SettingDef(
        key="CAMERA_START_TIMEOUT",
        label="Camera timeout (seconds)",
        input_type="number",
        default="10",
        help_text="Timeout in seconds for camera start/stop HTTP commands.",
    ),
)

_SETTINGS_BY_KEY: dict[str, _SettingDef] = {s.key: s for s in _SETTINGS_DEFS}


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class EventRequest(BaseModel):
    event_name: str


class CrewEntry(BaseModel):
    position: str
    sailor: str


class BoatCreate(BaseModel):
    sail_number: str
    name: str | None = None
    class_name: str | None = None


class BoatUpdate(BaseModel):
    sail_number: str | None = None
    name: str | None = None
    class_name: str | None = None


class RaceResultEntry(BaseModel):
    place: int
    boat_id: int | None = None
    sail_number: str | None = None
    finish_time: str | None = None
    dnf: bool = False
    dns: bool = False
    notes: str | None = None


class NoteCreate(BaseModel):
    body: str | None = None
    note_type: str = "text"
    ts: str | None = None  # UTC ISO 8601; defaults to server time if absent


class VideoCreate(BaseModel):
    youtube_url: str
    label: str = ""
    sync_utc: str  # UTC ISO 8601
    sync_offset_s: float = 0.0


class VideoUpdate(BaseModel):
    label: str | None = None
    sync_utc: str | None = None
    sync_offset_s: float | None = None


class SailCreate(BaseModel):
    type: str  # 'main' | 'jib' | 'spinnaker'
    name: str
    notes: str | None = None


class SailUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    active: bool | None = None


class RaceSailsSet(BaseModel):
    main_id: int | None = None
    jib_id: int | None = None
    spinnaker_id: int | None = None


POSITIONS: tuple[str, ...] = ("helm", "main", "pit", "bow", "tactician", "guest")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    storage: Storage,
    recorder: AudioRecorder | None = None,
    audio_config: AudioConfig | None = None,
) -> FastAPI:
    """Create and return the FastAPI application bound to the given Storage.

    If *recorder* and *audio_config* are provided, recording starts when a race
    starts and stops when the race ends.  Cameras are managed in the database
    and loaded dynamically for each operation.
    """
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address, config_filename="/dev/null")
    app = FastAPI(title="J105 Logger", docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.state.storage = storage
    _audio_session_id: int | None = None
    _debrief_audio_session_id: int | None = None
    _debrief_race_id: int | None = None
    _debrief_race_name: str | None = None
    _debrief_start_utc: datetime | None = None

    from logger.races import RaceConfig

    cfg = RaceConfig()

    # -- Static files + templates --
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    def _tpl_ctx(request: Request, page: str, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
        return {"request": request, "active_page": page, "git_info": _GIT_INFO, **extra}

    from logger.auth import (
        _is_auth_disabled,
        _resolve_user,
        generate_token,
        invite_expires_at,
        require_auth,
        session_expires_at,
    )

    _PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/avatars", "/auth/request-link", "/static"}

    async def _load_cameras() -> list[Any]:
        """Load cameras from the database and return Camera objects."""
        from logger.cameras import Camera

        rows = await storage.list_cameras()
        return [
            Camera(
                name=r["name"],
                ip=r["ip"],
                model=r["model"],
                wifi_ssid=r.get("wifi_ssid"),
                wifi_password=r.get("wifi_password"),
            )
            for r in rows
        ]

    async def _audit(
        request: Request,
        action: str,
        detail: str | None = None,
        user: dict[str, Any] | None = None,
    ) -> None:
        """Fire-and-forget audit log entry."""
        uid = user.get("id") if user else None
        ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")
        await storage.log_action(action, detail=detail, user_id=uid, ip_address=ip, user_agent=ua)

    @app.middleware("http")
    async def auth_middleware(  # type: ignore[no-untyped-def]
        request: Request,
        call_next,  # noqa: ANN001
    ) -> Response:
        path = request.url.path
        if _is_auth_disabled():
            from logger.auth import _MOCK_ADMIN

            request.state.user = _MOCK_ADMIN
            return await call_next(request)  # type: ignore[no-any-return]
        if path in _PUBLIC_PATHS or path.startswith(("/notes/", "/static/")):
            return await call_next(request)  # type: ignore[no-any-return]
        from http.cookies import SimpleCookie

        raw_cookie = request.headers.get("cookie", "")
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(raw_cookie)
        session_val = cookie["session"].value if "session" in cookie else None
        user = await _resolve_user(request, session=session_val)
        if user is None:
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                from starlette.responses import RedirectResponse as _RR

                return _RR(url=f"/login?next={path}", status_code=307)
            from starlette.responses import JSONResponse as _JR

            return _JR({"detail": "Not authenticated"}, status_code=401)
        request.state.user = user
        return await call_next(request)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Public routes: /healthz, /login, /logout
    # ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/api/me")
    async def api_me(request: Request) -> JSONResponse:
        """Return the current user's identity and role."""
        user: dict[str, Any] | None = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return JSONResponse(
            {
                "id": user.get("id"),
                "email": user.get("email"),
                "name": user.get("name"),
                "role": user.get("role"),
                "avatar_path": user.get("avatar_path"),
            }
        )

    _EMAIL_FORM_HTML = (
        '<div class="card" style="margin-top:16px">'
        '<form method="post" action="/auth/request-link">'
        "<label>Don't have a token?</label>"
        '<input type="email" name="email" placeholder="Enter your email address" required/>'
        '<button class="btn" type="submit" style="background:#475569">'
        "Send me a login link</button></form></div>"
    )

    def _login_ctx(
        next_url: str, token_value: str, error_html: str = "", email_form: str = ""
    ) -> dict[str, str]:
        return {
            "next_url": next_url,
            "token_value": token_value,
            "error_html": error_html,
            "email_form_html": email_form,
        }

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request, next: str = "/", token: str = "") -> HTMLResponse:
        from logger.email import smtp_configured

        email_form = _EMAIL_FORM_HTML if smtp_configured() else ""
        return _templates.TemplateResponse(
            request, "login.html", _login_ctx(next, token, email_form=email_form)
        )

    @app.post("/login", include_in_schema=False)
    @limiter.limit("10/minute")
    async def login_submit(
        request: Request,
        token: str = Form(...),
        next: str = Form(default="/"),
    ) -> Response:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        def _login_err(msg: str) -> HTMLResponse:
            ctx = _login_ctx(next, token, f'<p style="color:#f87171;margin-top:12px">{msg}</p>')
            return _templates.TemplateResponse(request, "login.html", ctx, status_code=400)

        token = token.strip()
        row = await storage.get_invite_token(token)
        if row is None:
            return _login_err("Invalid or expired token.")
        if row["used_at"] is not None:
            return _login_err("Token already used.")
        expires_dt = _dt.fromisoformat(row["expires_at"])
        if _dt.now(_UTC) > expires_dt:
            return _login_err("Token expired.")

        # Find or create the user
        user = await storage.get_user_by_email(row["email"])
        if user is None:
            user_id = await storage.create_user(row["email"], None, row["role"])
        else:
            user_id = user["id"]

        # Mark token used and create session
        await storage.redeem_invite_token(token)
        session_id = generate_token()
        await storage.create_session(
            session_id,
            user_id,
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

        # Best-effort new-device alert email
        from logger.email import send_device_alert, smtp_configured

        if smtp_configured():
            asyncio.ensure_future(
                send_device_alert(
                    row["email"],
                    request.client.host if request.client else None,
                    request.headers.get("user-agent"),
                )
            )

        return response

    _REQUEST_LINK_RESPONSE = (
        '<p style="color:#34d399;margin-top:12px">'
        "If an account exists for that email, a login link has been sent.</p>"
    )

    @app.post("/auth/request-link", include_in_schema=False)
    @limiter.limit("5/minute")
    async def request_login_link(
        request: Request,
        email: str = Form(default=""),
    ) -> HTMLResponse:
        from logger.email import send_login_link_email, smtp_configured

        ctx = _login_ctx("/", "", error_html=_REQUEST_LINK_RESPONSE)

        def _resp() -> HTMLResponse:
            return _templates.TemplateResponse(request, "login.html", ctx)

        email = email.strip().lower()
        if not email or not smtp_configured():
            return _resp()

        user = await storage.get_user_by_email(email)
        if user is None:
            return _resp()

        # Per-email rate limit: max 3 tokens/hour
        recent = await storage.count_recent_tokens_for_email(email)
        if recent >= 3:
            return _resp()

        # Create token and send email
        token = generate_token()
        await storage.create_invite_token(
            token, email, user["role"], user["id"], invite_expires_at()
        )
        public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
        login_url = f"{public_url}/login?token={token}"
        asyncio.ensure_future(send_login_link_email(user.get("name"), email, login_url))
        await _audit(request, "auth.request_link", detail=email)
        return _resp()

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        from http.cookies import SimpleCookie

        raw_cookie = request.headers.get("cookie", "")
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(raw_cookie)
        if "session" in cookie:
            await storage.delete_session(cookie["session"].value)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("session")
        return response

    # ------------------------------------------------------------------
    # HTML UI
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> Response:
        return _templates.TemplateResponse(
            request,
            "home.html",
            _tpl_ctx(
                request,
                "/",
                grafana_port=cfg.grafana_port,
                grafana_uid=cfg.grafana_uid,
                sk_port=cfg.sk_port,
            ),
        )

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    async def history_page(request: Request) -> Response:
        return _templates.TemplateResponse(
            request,
            "history.html",
            _tpl_ctx(
                request,
                "/history",
                grafana_port=cfg.grafana_port,
                grafana_uid=cfg.grafana_uid,
            ),
        )

    @app.get("/admin/boats", response_class=HTMLResponse, include_in_schema=False)
    async def admin_boats_page(request: Request) -> Response:
        return _templates.TemplateResponse(
            request, "admin/boats.html", _tpl_ctx(request, "/admin/boats")
        )

    @app.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
    async def admin_users_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        from logger.races import configured_tz

        tz = configured_tz()
        users = await storage.list_users()
        sessions = await storage.list_auth_sessions()
        await storage.delete_expired_sessions()

        def _local_ts(utc_str: str | None) -> str:
            if not utc_str:
                return "—"
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

        user_rows = "".join(
            f"<tr><td>{u['email']}</td><td>{u['name'] or '—'}</td>"
            f"<td>{_badge(u['role'])}</td>"
            f"<td>{_local_ts(u['last_seen'])}</td>"
            f'<td><button onclick="changeRole({u["id"]})" style="cursor:pointer;background:none;border:1px solid #2563eb;color:#7eb8f7;border-radius:4px;padding:2px 8px;font-size:.8rem">Change role</button></td>'  # noqa: E501
            f"</tr>"
            for u in users
        )
        sess_rows = "".join(
            f"<tr><td>{s.get('email', '')}</td><td>{s.get('role', '')}</td>"
            f"<td>{s.get('ip', '—')}</td>"
            f"<td>{_local_ts(s['created_at'])}</td>"
            f"<td>{_local_ts(s['expires_at'])}</td>"
            f'<td><button onclick="revokeSession(\'{s["session_id"]}\')" style="cursor:pointer;background:#7f1d1d;border:none;color:#fca5a5;border-radius:4px;padding:2px 8px;font-size:.8rem">Revoke</button></td>'  # noqa: E501
            f"</tr>"
            for s in sessions
        )
        return _templates.TemplateResponse(
            request,
            "admin/users.html",
            _tpl_ctx(
                request,
                "/admin/users",
                user_rows=user_rows,
                session_rows=sess_rows,
            ),
        )

    @app.post("/admin/users/invite", status_code=201, include_in_schema=False)
    @limiter.limit("5/minute")
    async def admin_invite_user(
        request: Request,
        email: str = Form(...),
        role: str = Form(...),
        name: str = Form(default=""),
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        if role not in ("admin", "crew", "viewer"):
            raise HTTPException(status_code=422, detail="Invalid role")
        email = email.strip().lower()
        if not email:
            raise HTTPException(status_code=422, detail="email must not be blank")
        clean_name = name.strip() or None
        token = generate_token()
        base = str(request.base_url).rstrip("/")
        await storage.create_invite_token(token, email, role, _user["id"], invite_expires_at())
        invite_url = f"{base}/login?token={token}"
        await _audit(request, "user.invite", detail=f"{email} as {role}", user=_user)

        from logger.email import send_welcome_email, smtp_configured

        email_sent = False
        if smtp_configured() and email:
            email_sent = await send_welcome_email(clean_name, email, role, invite_url)

        return JSONResponse(
            {"invite_url": invite_url, "token": token, "email_sent": email_sent},
            status_code=201,
        )

    @app.put("/admin/users/{user_id}/role", status_code=204, include_in_schema=False)
    async def admin_update_role(
        request: Request,
        user_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        role = (body.get("role") or "").strip()
        if role not in ("admin", "crew", "viewer"):
            raise HTTPException(status_code=422, detail="Invalid role")
        user = await storage.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        await storage.update_user_role(user_id, role)
        await _audit(request, "user.role", detail=f"user={user_id} role={role}", user=_user)

    @app.delete("/admin/sessions/{session_id}", status_code=204, include_in_schema=False)
    async def admin_revoke_session(
        request: Request,
        session_id: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        await storage.delete_session(session_id)
        await _audit(request, "session.revoke", detail=session_id[:16], user=_user)

    # ------------------------------------------------------------------
    # /admin/audit (#93)
    # ------------------------------------------------------------------

    @app.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
    async def admin_audit_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        from logger.races import configured_tz

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
        return _templates.TemplateResponse(
            request,
            "admin/audit.html",
            _tpl_ctx(request, "/admin/audit", audit_rows=audit_rows, has_entries=bool(entries)),
        )

    @app.get("/api/audit")
    async def api_audit_log(
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        entries = await storage.list_audit_log(limit=limit, offset=offset)
        return JSONResponse(entries)

    # ------------------------------------------------------------------
    # /admin/cameras (#98)
    # ------------------------------------------------------------------

    @app.get("/admin/cameras", response_class=HTMLResponse, include_in_schema=False)
    async def admin_cameras_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        return _templates.TemplateResponse(
            request, "admin/cameras.html", _tpl_ctx(request, "/admin/cameras")
        )

    @app.get("/admin/events", response_class=HTMLResponse, include_in_schema=False)
    async def admin_events_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        return _templates.TemplateResponse(
            request, "admin/events.html", _tpl_ctx(request, "/admin/events")
        )

    # ------------------------------------------------------------------
    # /admin/settings (#146)
    # ------------------------------------------------------------------

    @app.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
    async def admin_settings_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        return _templates.TemplateResponse(
            request, "admin/settings.html", _tpl_ctx(request, "/admin/settings")
        )

    @app.get("/api/settings")
    async def api_get_settings(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Return all curated settings with effective value and source."""

        db_settings = {r["key"]: r["value"] for r in await storage.list_settings()}
        result: list[dict[str, object]] = []
        for s in _SETTINGS_DEFS:
            db_val = db_settings.get(s.key)
            env_val = os.environ.get(s.key)
            if db_val is not None:
                source, effective = "db", db_val
            elif env_val is not None:
                source, effective = "env", env_val
            else:
                source, effective = "default", s.default
            display = "••••••••" if s.sensitive and effective else effective
            result.append(
                {
                    "key": s.key,
                    "label": s.label,
                    "input_type": s.input_type,
                    "default_value": s.default,
                    "help_text": s.help_text,
                    "options": list(s.options),
                    "sensitive": s.sensitive,
                    "effective_value": display,
                    "source": source,
                }
            )
        return JSONResponse({"settings": result})

    @app.put("/api/settings")
    async def api_put_settings(
        request: Request,
        body: dict[str, str],
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Upsert settings. Empty string deletes the override (reverts to env/default)."""
        changed: list[str] = []
        for key, value in body.items():
            defn = _SETTINGS_BY_KEY.get(key)
            if defn is None:
                raise HTTPException(status_code=422, detail=f"Unknown setting: {key}")
            value = str(value).strip()
            if value == "":
                # Delete override → revert to env/default
                await storage.delete_setting(key)
                # Remove from os.environ only if it came from our DB seeding
                # (don't remove actual shell env vars)
            else:
                await storage.set_setting(key, value)
                os.environ[key] = value
            changed.append(key)
        if changed:
            await _audit(
                request,
                "settings.update",
                detail=", ".join(changed),
                user=_user,
            )
        return JSONResponse({"updated": changed})

    @app.get("/api/cameras")
    async def api_list_cameras(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """List configured cameras with live status."""
        cams = await _load_cameras()
        if not cams:
            return JSONResponse([])

        import logger.cameras as cameras_mod

        statuses = await asyncio.gather(
            *(cameras_mod.get_status(cam) for cam in cams),
            return_exceptions=True,
        )
        result: list[dict[str, Any]] = []
        for cam, st in zip(cams, statuses, strict=True):
            if isinstance(st, BaseException):
                result.append(
                    {
                        "name": cam.name,
                        "ip": cam.ip,
                        "model": cam.model,
                        "wifi_ssid": cam.wifi_ssid,
                        "wifi_password": cam.wifi_password,
                        "recording": False,
                        "error": str(st),
                    }
                )
            else:
                result.append(
                    {
                        "name": st.name,
                        "ip": st.ip,
                        "model": cam.model,
                        "wifi_ssid": cam.wifi_ssid,
                        "wifi_password": cam.wifi_password,
                        "recording": st.recording,
                        "error": st.error,
                    }
                )
        return JSONResponse(result)

    @app.post("/api/cameras/{camera_name}/start")
    async def api_start_camera(
        camera_name: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Manually start recording on a single camera."""
        import logger.cameras as cameras_mod

        cams = await _load_cameras()
        cam = next((c for c in cams if c.name == camera_name), None)
        if cam is None:
            raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
        status = await cameras_mod.start_camera(cam)
        return JSONResponse(
            {
                "name": status.name,
                "ip": status.ip,
                "recording": status.recording,
                "error": status.error,
            }
        )

    @app.post("/api/cameras/{camera_name}/stop")
    async def api_stop_camera(
        camera_name: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Manually stop recording on a single camera."""
        import logger.cameras as cameras_mod

        cams = await _load_cameras()
        cam = next((c for c in cams if c.name == camera_name), None)
        if cam is None:
            raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
        status = await cameras_mod.stop_camera(cam)
        return JSONResponse(
            {
                "name": status.name,
                "ip": status.ip,
                "recording": status.recording,
                "error": status.error,
            }
        )

    @app.get("/api/cameras/sessions")
    async def api_camera_sessions_all(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """List recent camera sessions across all cameras."""
        db = storage._conn()
        cur = await db.execute(
            "SELECT cs.id, cs.session_id, cs.camera_name, cs.camera_ip,"
            " cs.recording_started_utc, cs.recording_stopped_utc,"
            " cs.sync_offset_ms, cs.error, r.name AS race_name"
            " FROM camera_sessions cs"
            " JOIN races r ON r.id = cs.session_id"
            " ORDER BY cs.id DESC LIMIT 50",
        )
        rows = await cur.fetchall()
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/sessions/{session_id}/cameras")
    async def api_session_cameras(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """List camera sessions for a specific race."""
        rows = await storage.list_camera_sessions(session_id)
        return JSONResponse(rows)

    # ------------------------------------------------------------------
    # Camera CRUD (#147)
    # ------------------------------------------------------------------

    @app.post("/api/cameras", status_code=201)
    async def api_add_camera(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Add a new camera configuration."""
        body = await request.json()
        name = str(body.get("name", "")).strip()
        ip = str(body.get("ip", "")).strip()
        model = str(body.get("model", "insta360-x4")).strip()
        wifi_ssid = str(body.get("wifi_ssid", "")).strip() or None
        wifi_password = str(body.get("wifi_password", "")).strip() or None
        if not name or not ip:
            raise HTTPException(400, detail="name and ip are required")
        try:
            cam_id = await storage.add_camera(name, ip, model, wifi_ssid, wifi_password)
        except Exception:  # noqa: BLE001
            raise HTTPException(409, detail=f"Camera {name!r} already exists") from None
        await _audit(request, "camera.add", detail=name, user=_user)
        return JSONResponse(
            {"id": cam_id, "name": name, "ip": ip, "model": model, "wifi_ssid": wifi_ssid},
            status_code=201,
        )

    @app.put("/api/cameras/{camera_name}")
    async def api_update_camera(
        request: Request,
        camera_name: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Update a camera's IP, model, name, or WiFi credentials."""
        body = await request.json()
        ip = str(body.get("ip", "")).strip()
        model = body.get("model")
        new_name = str(body.get("name", "")).strip()
        wifi_ssid = str(body.get("wifi_ssid", "")).strip() or None
        wifi_password = str(body.get("wifi_password", "")).strip() or None
        if not ip:
            raise HTTPException(400, detail="ip is required")
        if new_name and new_name != camera_name:
            ok = await storage.rename_camera(
                camera_name,
                new_name,
                ip,
                model=model if model else None,
                wifi_ssid=wifi_ssid,
                wifi_password=wifi_password,
            )
        else:
            ok = await storage.update_camera(
                camera_name,
                ip,
                model=model if model else None,
                wifi_ssid=wifi_ssid,
                wifi_password=wifi_password,
            )
        if not ok:
            raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
        await _audit(request, "camera.update", detail=camera_name, user=_user)
        return JSONResponse({"name": new_name or camera_name, "ip": ip, "wifi_ssid": wifi_ssid})

    @app.delete("/api/cameras/{camera_name}", status_code=204)
    async def api_delete_camera(
        request: Request,
        camera_name: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        """Delete a camera configuration."""
        ok = await storage.delete_camera(camera_name)
        if not ok:
            raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
        await _audit(request, "camera.delete", detail=camera_name, user=_user)

    # ------------------------------------------------------------------
    # /api/state
    # ------------------------------------------------------------------

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        from logger.races import Race as _Race
        from logger.races import configured_tz, default_event_for_date, local_today, local_weekday

        now = datetime.now(UTC)
        today = local_today()
        date_str = today.isoformat()
        weekday = local_weekday()

        rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
        default_event = default_event_for_date(today, rules)
        custom_event = await storage.get_daily_event(date_str)

        if default_event is not None:
            event: str | None = default_event
            event_is_default = True
        elif custom_event is not None:
            event = custom_event
            event_is_default = False
        else:
            event = None
            event_is_default = False

        current = await storage.get_current_race()
        today_races = await storage.list_races_for_date(date_str)

        next_race_num = await storage.count_sessions_for_date(date_str, "race") + 1
        next_practice_num = await storage.count_sessions_for_date(date_str, "practice") + 1

        async def _race_dict(r: _Race) -> dict[str, Any]:
            duration_s: float | None = None
            if r.end_utc is not None:
                duration_s = (r.end_utc - r.start_utc).total_seconds()
            else:
                elapsed = (now - r.start_utc).total_seconds()
                duration_s = elapsed
            crew = await storage.get_race_crew(r.id)
            results = await storage.list_race_results(r.id)
            sails = await storage.get_race_sails(r.id)
            cur = await storage._conn().execute(
                "SELECT id FROM audio_sessions"
                " WHERE race_id = ? AND session_type IN ('race', 'practice') LIMIT 1",
                (r.id,),
            )
            audio_row = await cur.fetchone()
            audio_session_id: int | None = audio_row["id"] if audio_row else None
            return {
                "id": r.id,
                "name": r.name,
                "event": r.event,
                "race_num": r.race_num,
                "date": r.date,
                "start_utc": r.start_utc.isoformat(),
                "end_utc": r.end_utc.isoformat() if r.end_utc else None,
                "duration_s": round(duration_s, 1) if duration_s is not None else None,
                "session_type": r.session_type,
                "crew": crew,
                "results": results,
                "sails": sails,
                "has_audio": audio_session_id is not None,
                "audio_session_id": audio_session_id,
            }

        current_dict = await _race_dict(current) if current else None
        today_race_dicts = [await _race_dict(r) for r in today_races]

        return JSONResponse(
            {
                "date": date_str,
                "weekday": weekday,
                "timezone": str(configured_tz()),
                "event": event,
                "event_is_default": event_is_default,
                "current_race": current_dict,
                "next_race_num": next_race_num,
                "next_practice_num": next_practice_num,
                "today_races": today_race_dicts,
                "has_recorder": recorder is not None,
                "current_debrief": {
                    "race_id": _debrief_race_id,
                    "race_name": _debrief_race_name,
                    "start_utc": _debrief_start_utc.isoformat(),
                }
                if _debrief_race_id is not None
                else None,
            }
        )

    # ------------------------------------------------------------------
    # /api/instruments
    # ------------------------------------------------------------------

    @app.get("/api/instruments")
    async def api_instruments() -> JSONResponse:
        data = await storage.latest_instruments()
        return JSONResponse(data)

    # ------------------------------------------------------------------
    # /api/polar/current
    # ------------------------------------------------------------------

    @app.get("/api/polar/current")
    async def api_polar_current() -> JSONResponse:
        import logger.polar as _polar

        inst = await storage.latest_instruments()
        tws = inst.get("tws_kts")
        twa = inst.get("twa_deg")
        bsp = inst.get("bsp_kts")
        point = None
        if tws is not None and twa is not None:
            point = await _polar.lookup_polar(storage, float(tws), float(twa))
        baseline_bsp = point["mean_bsp"] if point else None
        delta = (
            round(float(bsp) - float(baseline_bsp), 2)
            if (bsp is not None and baseline_bsp is not None)
            else None
        )
        return JSONResponse(
            {
                "bsp": bsp,
                "tws": tws,
                "twa": twa,
                "baseline_bsp": baseline_bsp,
                "baseline_p90": point["p90_bsp"] if point else None,
                "delta": delta,
                "sufficient_data": point is not None,
            }
        )

    # ------------------------------------------------------------------
    # /api/event
    # ------------------------------------------------------------------

    @app.post("/api/event", status_code=204)
    async def api_set_event(
        request: Request,
        body: EventRequest,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        event_name = body.event_name.strip()
        if not event_name:
            raise HTTPException(status_code=422, detail="event_name must not be blank")
        from logger.races import local_today

        date_str = local_today().isoformat()
        await storage.set_daily_event(date_str, event_name)
        await _audit(request, "event.set", detail=event_name, user=_user)

    # ------------------------------------------------------------------
    # /api/event-rules (day-of-week → event name)
    # ------------------------------------------------------------------

    _WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    @app.get("/api/event-rules")
    async def api_list_event_rules(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        rules = await storage.list_event_rules()
        for r in rules:
            r["weekday_name"] = _WEEKDAY_NAMES[r["weekday"]]
        return JSONResponse(rules)

    @app.post("/api/event-rules", status_code=201)
    async def api_set_event_rule(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        body = await request.json()
        weekday = body.get("weekday")
        event_name = str(body.get("event_name", "")).strip()
        if weekday is None or not isinstance(weekday, int) or not (0 <= weekday <= 6):
            raise HTTPException(400, detail="weekday must be an integer 0 (Mon) – 6 (Sun)")
        if not event_name:
            raise HTTPException(400, detail="event_name is required")
        await storage.set_event_rule(weekday, event_name)
        await _audit(
            request, "event_rule.set", detail=f"{_WEEKDAY_NAMES[weekday]}={event_name}", user=_user
        )
        return JSONResponse({"weekday": weekday, "event_name": event_name})

    @app.delete("/api/event-rules/{weekday}", status_code=204)
    async def api_delete_event_rule(
        weekday: int,
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        if not (0 <= weekday <= 6):
            raise HTTPException(400, detail="weekday must be 0–6")
        ok = await storage.delete_event_rule(weekday)
        if not ok:
            raise HTTPException(404, detail="No rule for that weekday")
        await _audit(request, "event_rule.delete", detail=_WEEKDAY_NAMES[weekday], user=_user)

    # ------------------------------------------------------------------
    # /api/races/start
    # ------------------------------------------------------------------

    @app.post("/api/races/start", status_code=201)
    async def api_start_race(
        request: Request,
        session_type: str = Query(default="race"),
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        nonlocal \
            _audio_session_id, \
            _debrief_audio_session_id, \
            _debrief_race_id, \
            _debrief_race_name, \
            _debrief_start_utc
        from logger.races import build_race_name, default_event_for_date

        if session_type not in ("race", "practice"):
            raise HTTPException(
                status_code=422,
                detail="session_type must be 'race' or 'practice'",
            )

        from logger.races import local_today

        now = datetime.now(UTC)
        today = local_today()
        date_str = today.isoformat()

        rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
        default_event = default_event_for_date(today, rules)
        custom_event = await storage.get_daily_event(date_str)
        event = custom_event or default_event
        if event is None:
            raise HTTPException(
                status_code=422,
                detail="No event set for today. POST /api/event first.",
            )

        # Auto-stop any active debrief before starting a new session
        if _debrief_audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
            logger.info("Debrief auto-stopped to start new {}", session_type)
            _debrief_audio_session_id = None
            _debrief_race_id = None
            _debrief_race_name = None
            _debrief_start_utc = None

        race_num = await storage.count_sessions_for_date(date_str, session_type) + 1
        name = build_race_name(event, today, race_num, session_type)

        race = await storage.start_race(event, now, date_str, race_num, name, session_type)

        # Copy crew from most recently closed session as defaults
        last_crew = await storage.get_last_session_crew()
        if last_crew:
            await storage.set_race_crew(race.id, last_crew)
            logger.info("Crew carried forward to {}: {} positions", race.name, len(last_crew))

        if recorder is not None and audio_config is not None:
            from logger.audio import AudioDeviceNotFoundError

            try:
                session = await recorder.start(audio_config, name=race.name)
                _audio_session_id = await storage.write_audio_session(
                    session,
                    race_id=race.id,
                    session_type=session_type,
                    name=race.name,
                )
                logger.info("Audio recording started: {}", session.file_path)
            except AudioDeviceNotFoundError as exc:
                logger.warning("Audio unavailable for race {}: {}", race.name, exc)

        async def _start_cameras(rid: int) -> None:
            cams = await _load_cameras()
            if not cams:
                return
            import logger.cameras as cameras_mod

            try:
                statuses = await cameras_mod.start_all(cams, rid, storage)
                for s in statuses:
                    if s.error:
                        logger.warning("Camera {} failed to start: {}", s.name, s.error)
                    else:
                        logger.info("Camera {} recording started", s.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera start_all failed: {}", exc)

        asyncio.ensure_future(_start_cameras(race.id))
        await _audit(request, "race.start", detail=race.name, user=_user)
        return JSONResponse(
            {
                "id": race.id,
                "name": race.name,
                "event": race.event,
                "race_num": race.race_num,
                "start_utc": race.start_utc.isoformat(),
                "session_type": race.session_type,
            },
            status_code=201,
        )

    # ------------------------------------------------------------------
    # /api/races/{id}/end
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/end", status_code=204)
    async def api_end_race(
        request: Request,
        race_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        nonlocal _audio_session_id
        now = datetime.now(UTC)
        await storage.end_race(race_id, now)
        await _audit(request, "race.end", detail=str(race_id), user=_user)

        async def _stop_cameras(rid: int) -> None:
            cams = await _load_cameras()
            if not cams:
                return
            import logger.cameras as cameras_mod

            try:
                statuses = await cameras_mod.stop_all(cams, rid, storage)
                for s in statuses:
                    if s.error:
                        logger.warning("Camera {} failed to stop: {}", s.name, s.error)
                    else:
                        logger.info("Camera {} recording stopped", s.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera stop_all failed: {}", exc)

        asyncio.ensure_future(_stop_cameras(race_id))

        if recorder is not None and _audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_audio_session_id, completed.end_utc)
            logger.info("Audio recording saved: {}", completed.file_path)
            _audio_session_id = None

    # ------------------------------------------------------------------
    # /api/races/{id}/debrief/start
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/debrief/start", status_code=201)
    async def api_start_debrief(
        request: Request,
        race_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        nonlocal \
            _audio_session_id, \
            _debrief_audio_session_id, \
            _debrief_race_id, \
            _debrief_race_name, \
            _debrief_start_utc

        if recorder is None or audio_config is None:
            raise HTTPException(status_code=409, detail="No audio recorder configured")

        cur = await storage._conn().execute(
            "SELECT id, name, end_utc FROM races WHERE id = ?", (race_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Race not found")

        # Defensive: if the race is still in progress, auto-end it first
        if row["end_utc"] is None:
            now_end = datetime.now(UTC)
            await storage.end_race(race_id, now_end)
            if _audio_session_id is not None:
                completed = await recorder.stop()
                assert completed.end_utc is not None
                await storage.update_audio_session_end(_audio_session_id, completed.end_utc)
                _audio_session_id = None
            logger.info("Race {} auto-ended to start debrief", race_id)

        if _debrief_audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
            _debrief_audio_session_id = None

        debrief_name = f"{row['name']}-debrief"
        now = datetime.now(UTC)
        session = await recorder.start(audio_config, name=debrief_name)
        _debrief_audio_session_id = await storage.write_audio_session(
            session,
            race_id=race_id,
            session_type="debrief",
            name=debrief_name,
        )
        _debrief_race_id = race_id
        _debrief_race_name = row["name"]
        _debrief_start_utc = now
        logger.info("Debrief recording started: {}", session.file_path)

        await _audit(request, "debrief.start", detail=row["name"], user=_user)
        return JSONResponse(
            {"race_id": race_id, "race_name": row["name"], "start_utc": now.isoformat()},
            status_code=201,
        )

    # ------------------------------------------------------------------
    # /api/debrief/stop
    # ------------------------------------------------------------------

    @app.post("/api/debrief/stop", status_code=204)
    async def api_stop_debrief(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        nonlocal _debrief_audio_session_id, _debrief_race_id, _debrief_race_name, _debrief_start_utc

        if _debrief_audio_session_id is None:
            raise HTTPException(status_code=409, detail="No debrief in progress")

        completed = await recorder.stop()
        assert completed.end_utc is not None
        await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
        logger.info("Debrief recording saved: {}", completed.file_path)

        await _audit(request, "debrief.stop", user=_user)
        _debrief_audio_session_id = None
        _debrief_race_id = None
        _debrief_race_name = None
        _debrief_start_utc = None

    # ------------------------------------------------------------------
    # /api/races/{id}/export.{fmt}
    # ------------------------------------------------------------------

    @app.get("/api/races/{race_id}/export.{fmt}")
    async def api_export_race(race_id: int, fmt: str) -> FileResponse:
        if fmt not in ("csv", "gpx", "json"):
            raise HTTPException(status_code=400, detail="fmt must be csv, gpx, or json")

        from logger.races import local_today

        races = await storage.list_races_for_date(local_today().isoformat())
        # Also search across all dates by fetching by id directly
        race = None
        for r in races:
            if r.id == race_id:
                race = r
                break

        if race is None:
            # Fallback: search all races (no date filter)
            cur = await storage._conn().execute(
                "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
                " FROM races WHERE id = ?",
                (race_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Race not found")
            from datetime import datetime as _dt

            from logger.races import Race

            race = Race(
                id=row["id"],
                name=row["name"],
                event=row["event"],
                race_num=row["race_num"],
                date=row["date"],
                start_utc=_dt.fromisoformat(row["start_utc"]),
                end_utc=_dt.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                session_type=row["session_type"],
            )

        if race.end_utc is None:
            raise HTTPException(status_code=409, detail="Race is still in progress")

        from logger.export import export_to_file

        suffix = f".{fmt}"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            out_path = f.name

        await export_to_file(storage, race.start_utc, race.end_utc, out_path)

        filename = f"{race.name}.{fmt}"
        media = {
            "csv": "text/csv",
            "gpx": "application/gpx+xml",
            "json": "application/json",
        }[fmt]
        return FileResponse(
            out_path,
            media_type=media,
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------
    # /api/sessions  (history browser)
    # ------------------------------------------------------------------

    @app.get("/api/sessions")
    async def api_sessions(
        q: str | None = None,
        type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> JSONResponse:
        if type is not None and type not in ("race", "practice", "debrief"):
            raise HTTPException(
                status_code=422,
                detail="type must be 'race', 'practice', or 'debrief'",
            )
        limit = max(1, min(limit, 200))
        total, sessions = await storage.list_sessions(
            q=q or None,
            session_type=type,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"total": total, "sessions": sessions})

    # ------------------------------------------------------------------
    # /api/races
    # ------------------------------------------------------------------

    @app.get("/api/races")
    async def api_list_races(date: str | None = None) -> JSONResponse:
        if date is None:
            from logger.races import local_today

            date = local_today().isoformat()
        races = await storage.list_races_for_date(date)
        result = []
        for r in races:
            duration_s: float | None = None
            if r.end_utc is not None:
                duration_s = (r.end_utc - r.start_utc).total_seconds()
            result.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "event": r.event,
                    "race_num": r.race_num,
                    "date": r.date,
                    "start_utc": r.start_utc.isoformat(),
                    "end_utc": r.end_utc.isoformat() if r.end_utc else None,
                    "duration_s": round(duration_s, 1) if duration_s is not None else None,
                }
            )
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # /api/races/{id}/crew
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/crew", status_code=204)
    async def api_set_crew(
        request: Request,
        race_id: int,
        body: list[CrewEntry],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Race not found")

        invalid = [e.position for e in body if e.position not in POSITIONS]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown position(s): {invalid}. Must be one of {list(POSITIONS)}",
            )

        crew = [{"position": e.position, "sailor": e.sailor} for e in body if e.sailor.strip()]
        await storage.set_race_crew(race_id, crew)
        await _audit(request, "crew.set", detail=str(race_id), user=_user)

    @app.get("/api/races/{race_id}/crew")
    async def api_get_crew(race_id: int) -> JSONResponse:
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Race not found")

        crew = await storage.get_race_crew(race_id)
        recent = await storage.get_recent_sailors()
        return JSONResponse({"crew": crew, "recent_sailors": recent})

    # ------------------------------------------------------------------
    # /api/sailors/recent
    # ------------------------------------------------------------------

    @app.get("/api/sailors/recent")
    async def api_recent_sailors() -> JSONResponse:
        sailors = await storage.get_recent_sailors()
        return JSONResponse({"sailors": sailors})

    # ------------------------------------------------------------------
    # /api/boats
    # ------------------------------------------------------------------

    @app.get("/api/boats")
    async def api_list_boats(
        q: str | None = None,
        exclude_race: int | None = None,
    ) -> JSONResponse:
        boats = await storage.list_boats(exclude_race_id=exclude_race, q=q or None)
        return JSONResponse(boats)

    @app.post("/api/boats", status_code=201)
    async def api_create_boat(
        request: Request,
        body: BoatCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        sail = body.sail_number.strip()
        if not sail:
            raise HTTPException(status_code=422, detail="sail_number must not be blank")
        boat_id = await storage.add_boat(sail, body.name, body.class_name)
        await _audit(request, "boat.add", detail=sail, user=_user)
        return JSONResponse({"id": boat_id}, status_code=201)

    @app.patch("/api/boats/{boat_id}", status_code=204)
    async def api_update_boat(
        request: Request,
        boat_id: int,
        body: BoatUpdate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute(
            "SELECT sail_number, name, class FROM boats WHERE id = ?", (boat_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Boat not found")
        sail = (body.sail_number or "").strip() or row["sail_number"]
        name = body.name if body.name is not None else row["name"]
        class_name = body.class_name if body.class_name is not None else row["class"]
        await storage.update_boat(boat_id, sail, name, class_name)
        await _audit(request, "boat.update", detail=str(boat_id), user=_user)

    @app.delete("/api/boats/{boat_id}", status_code=204)
    async def api_delete_boat(
        request: Request,
        boat_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute("SELECT id FROM boats WHERE id = ?", (boat_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Boat not found")
        await storage.delete_boat(boat_id)
        await _audit(request, "boat.delete", detail=str(boat_id), user=_user)

    # ------------------------------------------------------------------
    # /api/sessions/{race_id}/results
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{race_id}/results")
    async def api_get_results(race_id: int) -> JSONResponse:
        results = await storage.list_race_results(race_id)
        return JSONResponse(results)

    @app.post("/api/sessions/{race_id}/results", status_code=201)
    async def api_upsert_result(
        request: Request,
        race_id: int,
        body: RaceResultEntry,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Race not found")

        if body.place < 1:
            raise HTTPException(status_code=422, detail="place must be >= 1")

        if body.boat_id is not None:
            boat_id = body.boat_id
            # Verify boat exists
            cur2 = await storage._conn().execute("SELECT id FROM boats WHERE id = ?", (boat_id,))
            if await cur2.fetchone() is None:
                raise HTTPException(status_code=404, detail="Boat not found")
        elif body.sail_number:
            boat_id = await storage.find_or_create_boat(body.sail_number)
        else:
            raise HTTPException(status_code=422, detail="boat_id or sail_number is required")

        result_id = await storage.upsert_race_result(
            race_id,
            body.place,
            boat_id,
            finish_time=body.finish_time,
            dnf=body.dnf,
            dns=body.dns,
            notes=body.notes,
        )
        await _audit(
            request, "result.upsert", detail=f"race={race_id} place={body.place}", user=_user
        )
        return JSONResponse({"id": result_id}, status_code=201)

    # ------------------------------------------------------------------
    # /api/results/{result_id}
    # ------------------------------------------------------------------

    @app.delete("/api/results/{result_id}", status_code=204)
    async def api_delete_result(
        request: Request,
        result_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute(
            "SELECT id FROM race_results WHERE id = ?", (result_id,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Result not found")
        await storage.delete_race_result(result_id)
        await _audit(request, "result.delete", detail=str(result_id), user=_user)

    # ------------------------------------------------------------------
    # /api/sessions/{session_id}/notes  &  /api/notes/{note_id}
    # ------------------------------------------------------------------

    async def _resolve_session(session_id: int) -> tuple[int | None, int | None]:
        """Return (race_id, audio_session_id) for the given session_id, or raise 404."""
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is not None:
            return session_id, None
        cur2 = await storage._conn().execute(
            "SELECT id FROM audio_sessions WHERE id = ?", (session_id,)
        )
        if await cur2.fetchone() is not None:
            return None, session_id
        raise HTTPException(status_code=404, detail="Session not found")

    @app.post("/api/sessions/{session_id}/notes", status_code=201)
    async def api_create_note(
        request: Request,
        session_id: int,
        body: NoteCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        if body.note_type not in ("text", "settings"):
            raise HTTPException(status_code=422, detail="note_type must be 'text' or 'settings'")
        if body.note_type == "text" and (not body.body or not body.body.strip()):
            raise HTTPException(status_code=422, detail="body must not be blank for text notes")
        if body.note_type == "settings":
            if not body.body:
                raise HTTPException(
                    status_code=422, detail="body must not be blank for settings notes"
                )
            try:
                parsed = json.loads(body.body)
                if not isinstance(parsed, dict):
                    raise ValueError  # noqa: TRY301
            except (json.JSONDecodeError, ValueError):
                raise HTTPException(  # noqa: B904
                    status_code=422,
                    detail="body must be a JSON object for settings notes",
                )
        race_id, audio_session_id = await _resolve_session(session_id)
        ts = body.ts if body.ts else datetime.now(UTC).isoformat()
        note_id = await storage.create_note(
            ts,
            body.body,
            race_id=race_id,
            audio_session_id=audio_session_id,
            note_type=body.note_type,
            user_id=_user.get("id"),
        )
        from logger import influx

        await asyncio.to_thread(
            influx.write_note,
            ts_iso=ts,
            note_type=body.note_type,
            body=body.body,
            race_id=race_id,
            note_id=note_id,
        )
        await _audit(request, "note.add", detail=body.note_type, user=_user)
        return JSONResponse({"id": note_id, "ts": ts}, status_code=201)

    @app.post("/api/sessions/{session_id}/notes/photo", status_code=201)
    async def api_create_photo_note(
        request: Request,
        session_id: int,
        file: UploadFile,
        ts: str = Form(default=""),
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        race_id, audio_session_id = await _resolve_session(session_id)

        notes_dir = os.environ.get("NOTES_DIR", "data/notes")
        session_dir = Path(notes_dir) / str(session_id)
        await asyncio.to_thread(session_dir.mkdir, parents=True, exist_ok=True)

        now_str = datetime.now(UTC).isoformat()
        actual_ts = ts.strip() if ts.strip() else now_str
        safe_ts = actual_ts.replace(":", "-").replace("+", "")[:19]
        ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
        filename = f"{safe_ts}_{uuid.uuid4().hex[:8]}{ext}"
        dest = session_dir / filename

        data = await file.read()
        await asyncio.to_thread(dest.write_bytes, data)

        photo_path = f"{session_id}/{filename}"
        note_id = await storage.create_note(
            actual_ts,
            None,
            race_id=race_id,
            audio_session_id=audio_session_id,
            note_type="photo",
            photo_path=photo_path,
            user_id=_user.get("id"),
        )
        from logger import influx

        await asyncio.to_thread(
            influx.write_note,
            ts_iso=actual_ts,
            note_type="photo",
            body=f"/notes/{photo_path}",
            race_id=race_id,
            note_id=note_id,
        )
        await _audit(request, "note.photo", detail=photo_path, user=_user)
        return JSONResponse(
            {"id": note_id, "ts": actual_ts, "photo_path": photo_path}, status_code=201
        )

    @app.get("/notes/{path:path}")
    async def serve_note_photo(
        path: str,
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> Response:
        notes_dir = Path(os.environ.get("NOTES_DIR", "data/notes")).resolve()
        full_path = (notes_dir / path).resolve()
        if not str(full_path).startswith(str(notes_dir)):
            raise HTTPException(status_code=403, detail="Forbidden")
        if not full_path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        st = full_path.stat()
        etag = f'"{st.st_mtime_ns}-{st.st_size}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304)
        return FileResponse(
            full_path,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": etag,
            },
        )

    @app.get("/api/sessions/{session_id}/notes")
    async def api_list_notes(session_id: int) -> JSONResponse:
        race_id, audio_session_id = await _resolve_session(session_id)
        notes = await storage.list_notes(race_id=race_id, audio_session_id=audio_session_id)
        return JSONResponse(notes)

    @app.delete("/api/notes/{note_id}", status_code=204)
    async def api_delete_note(
        request: Request,
        note_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        found = await storage.delete_note(note_id)
        if not found:
            raise HTTPException(status_code=404, detail="Note not found")
        await _audit(request, "note.delete", detail=str(note_id), user=_user)

    @app.get("/api/notes/settings-keys")
    async def api_settings_keys() -> JSONResponse:
        """Return all distinct keys used in settings notes, sorted alphabetically.

        Used to populate the typeahead datalist on the settings note entry form.
        Returns: {"keys": ["backstay", "cunningham", ...]}
        """
        keys = await storage.list_settings_keys()
        return JSONResponse({"keys": keys})

    # ------------------------------------------------------------------
    # /api/sessions/{session_id}/videos  &  /api/videos/{video_id}
    # ------------------------------------------------------------------

    def _video_deep_link(row: dict[str, Any], at_utc: datetime | None = None) -> dict[str, Any]:
        """Augment a race_videos row with a computed YouTube deep-link.

        If *at_utc* is supplied the link jumps to that moment in the video.
        Otherwise the link just opens the video from the beginning.
        """
        from logger.video import VideoSession  # local import to avoid circular deps

        sync_utc = datetime.fromisoformat(row["sync_utc"])
        duration_s = row["duration_s"]

        out = dict(row)
        if at_utc is not None and duration_s is not None:
            vs = VideoSession(
                url=row["youtube_url"],
                video_id=row["video_id"],
                title=row["title"],
                duration_s=duration_s,
                sync_utc=sync_utc,
                sync_offset_s=row["sync_offset_s"],
            )
            out["deep_link"] = vs.url_at(at_utc)
        else:
            out["deep_link"] = None
        return out

    @app.get("/api/sessions/{session_id}/videos")
    async def api_list_videos(
        session_id: int,
        at: str | None = None,
    ) -> JSONResponse:
        """List videos linked to a session.

        Optional ``?at=<UTC ISO 8601>`` param computes a deep-link to that
        moment in each video.
        """
        # Videos are only supported on races (not audio sessions).
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Session not found")
        rows = await storage.list_race_videos(session_id)
        at_utc: datetime | None = None
        if at:
            try:
                at_utc = datetime.fromisoformat(at)
                if at_utc.tzinfo is None:
                    at_utc = at_utc.replace(tzinfo=UTC)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
        return JSONResponse([_video_deep_link(r, at_utc) for r in rows])

    @app.get("/api/sessions/{session_id}/videos/redirect")
    async def api_videos_redirect(
        session_id: int,
        at: str | None = None,
    ) -> RedirectResponse:
        """Redirect to the YouTube deep-link for a specific moment in the session's first video.

        Returns ``302 Location`` to the computed YouTube URL (with ``?t=<seconds>``).
        Returns ``404`` if the session doesn't exist or has no linked videos.
        Returns ``422`` if ``at`` is missing or cannot be parsed.
        """
        if not at:
            raise HTTPException(status_code=422, detail="'at' query parameter is required")
        try:
            at_utc = datetime.fromisoformat(at)
            if at_utc.tzinfo is None:
                at_utc = at_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Session not found")
        rows = await storage.list_race_videos(session_id)
        if not rows:
            raise HTTPException(status_code=404, detail="No videos linked to this session")
        # Use the first video by created_at (list_race_videos returns ASC order).
        row = rows[0]
        enriched = _video_deep_link(row, at_utc)
        url = enriched["deep_link"] or row["youtube_url"]
        return RedirectResponse(url=url, status_code=302)

    @app.get("/api/videos/redirect")
    async def api_videos_redirect_by_time(
        at: str | None = None,
    ) -> RedirectResponse:
        """Resolve the race active at ``at`` and redirect to its first video.

        Designed for Grafana Data Links — no session_id required.  Grafana
        passes ``${__value.time:date:iso}`` as the ``at`` parameter and this
        endpoint resolves the correct race automatically.

        Returns ``302 Location`` to the YouTube deep-link with ``?t=<seconds>``.
        Returns ``404`` if no race covers that timestamp or the race has no video.
        Returns ``422`` if ``at`` is missing or cannot be parsed.
        """
        if not at:
            raise HTTPException(status_code=422, detail="'at' query parameter is required")
        try:
            at_utc = datetime.fromisoformat(at)
            if at_utc.tzinfo is None:
                at_utc = at_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904

        at_iso = at_utc.isoformat()
        cur = await storage._conn().execute(
            """
            SELECT id FROM races
            WHERE start_utc <= ?
              AND (end_utc >= ? OR end_utc IS NULL)
            ORDER BY start_utc DESC
            LIMIT 1
            """,
            (at_iso, at_iso),
        )
        race_row = await cur.fetchone()
        if race_row is None:
            raise HTTPException(status_code=404, detail="No race found at this timestamp")

        session_id = race_row["id"]
        rows = await storage.list_race_videos(session_id)
        if not rows:
            raise HTTPException(status_code=404, detail="No videos linked to this session")

        row = rows[0]
        enriched = _video_deep_link(row, at_utc)
        url = enriched["deep_link"] or row["youtube_url"]
        return RedirectResponse(url=url, status_code=302)

    @app.post("/api/sessions/{session_id}/videos", status_code=201)
    async def api_add_video(
        request: Request,
        session_id: int,
        body: VideoCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Link a YouTube video to a race session.

        The caller supplies a sync point: a UTC wall-clock time and the
        corresponding video player position (seconds).  This pins the video
        timeline to logger time.
        """
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # Parse the sync UTC
        try:
            sync_utc = datetime.fromisoformat(body.sync_utc)
            if sync_utc.tzinfo is None:
                sync_utc = sync_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904

        # Extract YouTube video ID and fetch metadata via yt-dlp if available
        from logger.video import VideoLinker

        video_id = ""
        title = ""
        duration_s: float | None = None
        try:
            linker = VideoLinker()
            vs = await linker.create_session(body.youtube_url, sync_utc, body.sync_offset_s)
            video_id = vs.video_id
            title = vs.title
            duration_s = vs.duration_s
        except Exception:  # noqa: BLE001
            # yt-dlp unavailable or network error — store the URL as-is.
            # Extract video ID from URL heuristically.
            import re

            m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", body.youtube_url)
            video_id = m.group(1) if m else ""
            title = ""
            duration_s = None

        row_id = await storage.add_race_video(
            race_id=session_id,
            youtube_url=body.youtube_url,
            video_id=video_id,
            title=title,
            label=body.label,
            sync_utc=sync_utc,
            sync_offset_s=body.sync_offset_s,
            duration_s=duration_s,
            user_id=_user.get("id"),
        )
        rows = await storage.list_race_videos(session_id)
        row = next(r for r in rows if r["id"] == row_id)
        await _audit(request, "video.add", detail=body.youtube_url, user=_user)
        return JSONResponse(_video_deep_link(row), status_code=201)

    @app.patch("/api/videos/{video_id}", status_code=200)
    async def api_update_video(
        request: Request,
        video_id: int,
        body: VideoUpdate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Update label or sync calibration on an existing video link."""
        sync_utc: datetime | None = None
        if body.sync_utc is not None:
            try:
                sync_utc = datetime.fromisoformat(body.sync_utc)
                if sync_utc.tzinfo is None:
                    sync_utc = sync_utc.replace(tzinfo=UTC)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904
        found = await storage.update_race_video(
            video_id,
            label=body.label,
            sync_utc=sync_utc,
            sync_offset_s=body.sync_offset_s,
        )
        if not found:
            raise HTTPException(status_code=404, detail="Video not found")
        await _audit(request, "video.update", detail=str(video_id), user=_user)
        return JSONResponse({"id": video_id, "updated": True})

    @app.delete("/api/videos/{video_id}", status_code=204)
    async def api_delete_video(
        request: Request,
        video_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        """Remove a video link."""
        found = await storage.delete_race_video(video_id)
        if not found:
            raise HTTPException(status_code=404, detail="Video not found")
        await _audit(request, "video.delete", detail=str(video_id), user=_user)

    # ------------------------------------------------------------------
    # /api/grafana/annotations
    # ------------------------------------------------------------------

    @app.get("/api/grafana/annotations")
    async def api_grafana_annotations(
        from_: int | None = Query(default=None, alias="from"),
        to: int | None = None,
        sessionId: int | None = None,  # noqa: N803
    ) -> JSONResponse:
        """Grafana SimpleJSON annotation feed.

        Grafana passes epoch milliseconds as ``from`` and ``to``.
        Optional ``sessionId`` scopes results to a single race or practice.
        """
        if from_ is None or to is None:
            return JSONResponse([])
        start = datetime.fromtimestamp(from_ / 1000.0, tz=UTC)
        end = datetime.fromtimestamp(to / 1000.0, tz=UTC)
        race_id: int | None = None
        audio_session_id: int | None = None
        if sessionId is not None:
            race_id, audio_session_id = await _resolve_session(sessionId)
        notes = await storage.list_notes_range(
            start, end, race_id=race_id, audio_session_id=audio_session_id
        )
        result = []
        for n in notes:
            ts_ms = int(datetime.fromisoformat(n["ts"]).timestamp() * 1000)
            text = n["body"] or ""
            if n["note_type"] == "photo" and n.get("photo_path"):
                photo_url = f"/notes/{n['photo_path']}"
                text = f'<img src="{photo_url}" style="max-width:300px"/>'
                if n["body"]:
                    text = n["body"] + "<br/>" + text
            result.append(
                {
                    "time": ts_ms,
                    "timeEnd": ts_ms,
                    "title": n["note_type"].capitalize(),
                    "text": text,
                    "tags": [n["note_type"]],
                }
            )
        return JSONResponse(result, headers={"Access-Control-Allow-Origin": "*"})

    # ------------------------------------------------------------------
    # /api/sails  &  /api/sessions/{id}/sails
    # ------------------------------------------------------------------

    from logger.storage import _SAIL_TYPES  # noqa: PLC0415

    @app.get("/api/sails")
    async def api_list_sails() -> JSONResponse:
        """Return active sails grouped by type."""
        all_sails = await storage.list_sails(include_inactive=False)
        grouped: dict[str, list[dict[str, Any]]] = {t: [] for t in _SAIL_TYPES}
        for s in all_sails:
            if s["type"] in grouped:
                grouped[s["type"]].append(s)
        return JSONResponse(grouped)

    @app.post("/api/sails", status_code=201)
    async def api_add_sail(
        request: Request,
        body: SailCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Add a sail to the inventory."""
        if body.type not in _SAIL_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown sail type {body.type!r}. Must be one of {list(_SAIL_TYPES)}",
            )
        if not body.name.strip():
            raise HTTPException(status_code=422, detail="name must not be blank")
        try:
            sail_id = await storage.add_sail(body.type, body.name, body.notes)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Sail already exists: type={body.type!r} name={body.name!r}",
            ) from exc
        await _audit(request, "sail.add", detail=f"{body.type}/{body.name}", user=_user)
        return JSONResponse(
            {"id": sail_id, "type": body.type, "name": body.name.strip()}, status_code=201
        )

    @app.patch("/api/sails/{sail_id}", status_code=200)
    async def api_update_sail(
        request: Request,
        sail_id: int,
        body: SailUpdate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Update sail name/notes or retire it."""
        found = await storage.update_sail(
            sail_id, name=body.name, notes=body.notes, active=body.active
        )
        if not found:
            raise HTTPException(status_code=404, detail="Sail not found")
        await _audit(request, "sail.update", detail=str(sail_id), user=_user)
        return JSONResponse({"id": sail_id, "updated": True})

    @app.get("/api/sessions/{session_id}/sails")
    async def api_get_session_sails(session_id: int) -> JSONResponse:
        """Return the sail selection for a race/practice session."""
        race = await storage.get_race(session_id)
        if race is None:
            raise HTTPException(status_code=404, detail="Session not found")
        sails = await storage.get_race_sails(session_id)
        return JSONResponse(sails)

    @app.put("/api/sessions/{session_id}/sails", status_code=200)
    async def api_set_session_sails(
        request: Request,
        session_id: int,
        body: RaceSailsSet,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Set the sail selection for a race/practice session."""
        race = await storage.get_race(session_id)
        if race is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # Validate that each supplied sail_id references a sail of the correct type
        slot_map = {"main": body.main_id, "jib": body.jib_id, "spinnaker": body.spinnaker_id}
        for slot_type, sail_id in slot_map.items():
            if sail_id is None:
                continue
            all_sails = await storage.list_sails(include_inactive=True)
            matched = next((s for s in all_sails if s["id"] == sail_id), None)
            if matched is None:
                raise HTTPException(status_code=422, detail=f"Sail id={sail_id} not found")
            if matched["type"] != slot_type:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Sail id={sail_id} has type {matched['type']!r},"
                        f" expected {slot_type!r} for the {slot_type} slot"
                    ),
                )

        await storage.set_race_sails(
            session_id,
            main_id=body.main_id,
            jib_id=body.jib_id,
            spinnaker_id=body.spinnaker_id,
        )
        sails = await storage.get_race_sails(session_id)
        await _audit(request, "sails.set", detail=str(session_id), user=_user)
        return JSONResponse(sails)

    # ------------------------------------------------------------------
    # /api/audio/{session_id}/download  &  /api/audio/{session_id}/stream
    # ------------------------------------------------------------------

    @app.get("/api/audio/{session_id}/download")
    async def download_audio(session_id: int) -> FileResponse:
        """Download a WAV file as an attachment."""
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        path = Path(row["file_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found on disk")
        return FileResponse(
            path,
            media_type="audio/wav",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )

    @app.get("/api/audio/{session_id}/stream")
    async def stream_audio(session_id: int) -> FileResponse:
        """Stream a WAV file; Starlette handles Range headers for seekable playback."""
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        path = Path(row["file_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found on disk")
        return FileResponse(path, media_type="audio/wav")

    # ------------------------------------------------------------------
    # /api/system-health
    # ------------------------------------------------------------------

    @app.get("/api/system-health")
    async def api_system_health() -> JSONResponse:
        """Return current CPU, memory, and disk utilisation percentages."""
        import psutil  # type: ignore[import-untyped]

        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        payload: dict[str, float | None] = {
            "cpu_pct": cpu,
            "mem_pct": mem.percent,
            "disk_pct": disk.percent,
        }
        temp_c: float | None = None
        get_temps = getattr(psutil, "sensors_temperatures", None)
        if get_temps is not None:
            temps: dict[str, list[object]] = get_temps()
            for entries in temps.values():
                if entries:
                    current = getattr(entries[0], "current", None)
                    if current is not None:
                        temp_c = float(current)
                    break
        payload["cpu_temp_c"] = temp_c
        return JSONResponse(payload)

    # ------------------------------------------------------------------
    # /api/audio/{session_id}/transcribe  &  /api/audio/{session_id}/transcript
    # ------------------------------------------------------------------

    @app.post("/api/audio/{session_id}/transcribe", status_code=202)
    async def api_transcribe(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Trigger a transcription job for an audio session (202 Accepted).

        If a job already exists, returns 409 Conflict.
        """
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        model = os.environ.get("WHISPER_MODEL", "base")
        try:
            transcript_id = await storage.create_transcript_job(session_id, model)
        except ValueError:
            raise HTTPException(  # noqa: B904
                status_code=409, detail="Transcript job already exists for this session"
            )

        from logger.storage import get_effective_setting
        from logger.transcribe import transcribe_session

        t_url = await get_effective_setting(storage, "TRANSCRIBE_URL")
        diarize = bool(os.environ.get("HF_TOKEN"))
        asyncio.create_task(
            transcribe_session(
                storage,
                session_id,
                transcript_id,
                model_size=model,
                diarize=diarize,
                transcribe_url=t_url,
            )
        )
        await _audit(request, "transcribe.start", detail=str(session_id), user=_user)
        return JSONResponse({"status": "accepted", "transcript_id": transcript_id}, status_code=202)

    @app.get("/api/audio/{session_id}/transcript")
    async def api_get_transcript(session_id: int) -> JSONResponse:
        """Poll transcription status and retrieve the transcript text when done."""
        import json as _json

        t = await storage.get_transcript(session_id)
        if t is None:
            raise HTTPException(status_code=404, detail="No transcript job found for this session")
        if t.get("segments_json"):
            t["segments"] = _json.loads(t["segments_json"])
        del t["segments_json"]
        return JSONResponse(t)

    # ------------------------------------------------------------------
    # Tags (#99)
    # ------------------------------------------------------------------

    @app.get("/api/tags")
    async def api_list_tags() -> JSONResponse:
        tags = await storage.list_tags()
        return JSONResponse(tags)

    @app.post("/api/tags", status_code=201)
    async def api_create_tag(
        request: Request,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        name = (body.get("name") or "").strip().lower()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        color = body.get("color")
        tag = await storage.get_tag_by_name(name)
        if tag:
            raise HTTPException(status_code=409, detail="Tag already exists")
        tag_id = await storage.create_tag(name, color)
        await _audit(request, "tag.create", detail=name, user=_user)
        return JSONResponse({"id": tag_id, "name": name, "color": color}, status_code=201)

    @app.patch("/api/tags/{tag_id}", status_code=200)
    async def api_update_tag(
        request: Request,
        tag_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        found = await storage.update_tag(tag_id, name=body.get("name"), color=body.get("color"))
        if not found:
            raise HTTPException(status_code=404, detail="Tag not found")
        await _audit(request, "tag.update", detail=str(tag_id), user=_user)
        return JSONResponse({"id": tag_id, "updated": True})

    @app.delete("/api/tags/{tag_id}", status_code=204)
    async def api_delete_tag(
        request: Request,
        tag_id: int,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        found = await storage.delete_tag(tag_id)
        if not found:
            raise HTTPException(status_code=404, detail="Tag not found")
        await _audit(request, "tag.delete", detail=str(tag_id), user=_user)

    @app.post("/api/sessions/{session_id}/tags", status_code=201)
    async def api_add_session_tag(
        request: Request,
        session_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        tag_id = body.get("tag_id")
        tag_name = body.get("tag_name")
        if tag_id is None and not tag_name:
            raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
        if tag_id is None:
            assert isinstance(tag_name, str)
            tag_id = await storage.get_or_create_tag(tag_name)
        await storage.add_session_tag(session_id, tag_id)
        await _audit(
            request, "session.tag.add", detail=f"session={session_id} tag={tag_id}", user=_user
        )
        return JSONResponse({"session_id": session_id, "tag_id": tag_id}, status_code=201)

    @app.delete("/api/sessions/{session_id}/tags/{tag_id}", status_code=204)
    async def api_remove_session_tag(
        request: Request,
        session_id: int,
        tag_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        await storage.remove_session_tag(session_id, tag_id)
        await _audit(
            request, "session.tag.remove", detail=f"session={session_id} tag={tag_id}", user=_user
        )

    @app.get("/api/sessions/{session_id}/tags")
    async def api_get_session_tags(session_id: int) -> JSONResponse:
        tags = await storage.get_session_tags(session_id)
        return JSONResponse(tags)

    @app.post("/api/notes/{note_id}/tags", status_code=201)
    async def api_add_note_tag(
        request: Request,
        note_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        tag_id = body.get("tag_id")
        tag_name = body.get("tag_name")
        if tag_id is None and not tag_name:
            raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
        if tag_id is None:
            assert isinstance(tag_name, str)
            tag_id = await storage.get_or_create_tag(tag_name)
        await storage.add_note_tag(note_id, tag_id)
        await _audit(request, "note.tag.add", detail=f"note={note_id} tag={tag_id}", user=_user)
        return JSONResponse({"note_id": note_id, "tag_id": tag_id}, status_code=201)

    @app.delete("/api/notes/{note_id}/tags/{tag_id}", status_code=204)
    async def api_remove_note_tag(
        request: Request,
        note_id: int,
        tag_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        await storage.remove_note_tag(note_id, tag_id)
        await _audit(request, "note.tag.remove", detail=f"note={note_id} tag={tag_id}", user=_user)

    @app.get("/api/notes/{note_id}/tags")
    async def api_get_note_tags(note_id: int) -> JSONResponse:
        tags = await storage.get_note_tags(note_id)
        return JSONResponse(tags)

    # ------------------------------------------------------------------
    # Profile & Avatars (#100)
    # ------------------------------------------------------------------

    @app.get("/profile", response_class=HTMLResponse, include_in_schema=False)
    async def profile_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> Response:
        import time

        user_id = _user.get("id") or 0
        role = _user.get("role", "viewer")
        role_colors = {"admin": "#f59e0b", "crew": "#34d399", "viewer": "#60a5fa"}
        return _templates.TemplateResponse(
            request,
            "profile.html",
            _tpl_ctx(
                request,
                "/profile",
                name=_user.get("name") or _user.get("email") or "Unknown",
                email=_user.get("email") or "",
                role=role,
                role_color=role_colors.get(role, "#8892a4"),
                avatar_url=f"/avatars/{user_id}.jpg?v={int(time.time())}" if user_id else "",
            ),
        )

    @app.post("/profile/avatar", status_code=200, include_in_schema=False)
    async def upload_avatar(
        request: Request,
        file: UploadFile,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        user_id = _user.get("id")
        if user_id is None:
            raise HTTPException(status_code=400, detail="Cannot set avatar for mock user")

        # Validate content type
        ct = (file.content_type or "").lower()
        if ct not in ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"):
            raise HTTPException(status_code=422, detail="Unsupported image type")

        data = await file.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="File too large (max 10 MB)")

        avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
        await asyncio.to_thread(avatar_dir.mkdir, parents=True, exist_ok=True)
        dest = avatar_dir / f"{user_id}.jpg"

        try:
            import io

            from PIL import Image  # noqa: PLC0415

            opened = Image.open(io.BytesIO(data))
            rgb = opened.convert("RGB")
            # Centre-crop to square
            w, h = rgb.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            cropped = rgb.crop((left, top, left + side, top + side))
            resized = cropped.resize((256, 256), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=85)
            await asyncio.to_thread(dest.write_bytes, buf.getvalue())
        except ImportError:
            # Pillow not installed — save raw bytes as fallback
            await asyncio.to_thread(dest.write_bytes, data)

        rel_path = f"{user_id}.jpg"
        await storage.set_avatar_path(user_id, rel_path)
        await _audit(request, "avatar.upload", user=_user)
        return JSONResponse({"avatar_path": rel_path})

    @app.get("/avatars/{user_id}.jpg", include_in_schema=False)
    async def serve_avatar(user_id: int) -> Response:
        avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
        path = avatar_dir / f"{user_id}.jpg"
        if path.exists():
            mtime = int(path.stat().st_mtime)
            return FileResponse(
                path,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=60", "ETag": f'"{user_id}-{mtime}"'},
            )
        # Generate initials SVG fallback
        user = await storage.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        name = user.get("name") or user.get("email") or "?"
        parts = name.split()
        initials = (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()
        role = user.get("role", "viewer")
        colors = {"admin": "#2563eb", "crew": "#059669", "viewer": "#6b7280"}
        bg = colors.get(role, "#6b7280")
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">'
            f'<rect width="256" height="256" rx="128" fill="{bg}"/>'
            f'<text x="128" y="128" text-anchor="middle" dy=".35em"'
            f' font-family="system-ui,sans-serif" font-size="96" font-weight="700"'
            f' fill="white">{initials}</text></svg>'
        )
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-cache"},
        )

    return app
