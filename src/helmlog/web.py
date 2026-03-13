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
    from helmlog.audio import AudioConfig, AudioRecorder
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Git version info — read once at import time
# ---------------------------------------------------------------------------


def _get_git_info() -> str:
    """Return 'branch @ shortsha · clean/dirty' from the current git repo."""
    import subprocess

    try:
        _repo = str(Path(__file__).resolve().parents[2])
        _git = ["git", "-c", f"safe.directory={_repo}", "--no-optional-locks"]

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

        import socket

        hostname = socket.gethostname()
        status = "dirty" if dirty else "clean"
        return f"{hostname} · {branch} @ {sha} · {status}"
    except Exception:  # noqa: BLE001
        return ""


_GIT_INFO: str = _get_git_info()
# SHA the running process was started with — used to detect restart-needed
_STARTUP_SHA: str = ""
try:
    import subprocess as _sp

    _repo_dir = str(Path(__file__).resolve().parents[2])
    _STARTUP_SHA = _sp.check_output(  # noqa: S603, S607
        ["git", "-c", f"safe.directory={_repo_dir}", "rev-parse", "HEAD"],
        cwd=_repo_dir,
        text=True,
        stderr=_sp.DEVNULL,
    ).strip()
    del _sp, _repo_dir
except Exception:  # noqa: BLE001
    pass

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
        help_text="Base URL for the HelmLog API (used by the video pipeline).",
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
        default="private",
        options=("private", "unlisted", "public"),
        help_text="Privacy status for auto-uploaded YouTube videos. Default is 'private' per data policy.",
    ),
    _SettingDef(
        key="EXTERNAL_DATA_ENABLED",
        label="External data fetching",
        input_type="select",
        default="true",
        options=("true", "false"),
        help_text="Enable weather/tide fetching (sends GPS position to Open-Meteo and NOAA).",
    ),
    _SettingDef(
        key="VIDEO_CLEANUP_AFTER_UPLOAD",
        label="Delete video after upload",
        input_type="select",
        default="false",
        options=("true", "false"),
        help_text="Delete stitched MP4 from Mac after successful YouTube upload.",
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
    _SettingDef(
        key="MONITOR_INTERVAL_S",
        label="Health monitor interval (seconds)",
        input_type="number",
        default="2",
        help_text="How often to collect Pi health metrics for the dashboard (1\u2013300 seconds).",
    ),
    _SettingDef(
        key="NETWORK_AUTO_SWITCH",
        label="Auto-switch WLAN for races",
        input_type="select",
        default="false",
        options=("true", "false"),
        help_text="Automatically switch WLAN to camera Wi-Fi on race start and revert on race end.",
    ),
    _SettingDef(
        key="NETWORK_DEFAULT_PROFILE",
        label="Default WLAN profile ID",
        input_type="text",
        default="",
        help_text="WLAN profile ID to revert to after a race ends (used with auto-switch).",
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
    app = FastAPI(title="HelmLog", docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.state.storage = storage
    _audio_session_id: int | None = None
    _debrief_audio_session_id: int | None = None
    _debrief_race_id: int | None = None
    _debrief_race_name: str | None = None
    _debrief_start_utc: datetime | None = None

    from helmlog.races import RaceConfig

    cfg = RaceConfig()

    # -- Static files + templates --
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # -- Peer API (federation endpoints for remote boats) --
    from helmlog.peer_api import _limiter as peer_limiter
    from helmlog.peer_api import router as peer_router

    app.state.peer_limiter = peer_limiter
    app.include_router(peer_router)

    def _tpl_ctx(request: Request, page: str, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
        return {"request": request, "active_page": page, "git_info": _GIT_INFO, **extra}

    from helmlog.auth import (
        _is_auth_disabled,
        _resolve_user,
        generate_token,
        invite_expires_at,
        require_auth,
        require_developer,
        session_expires_at,
    )

    _PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/avatars", "/auth/request-link", "/static"}

    async def _load_cameras() -> list[Any]:
        """Load cameras from the database and return Camera objects."""
        from helmlog.cameras import Camera

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
            from helmlog.auth import _MOCK_ADMIN

            request.state.user = _MOCK_ADMIN
            return await call_next(request)  # type: ignore[no-any-return]
        if path in _PUBLIC_PATHS or path.startswith(("/static/", "/co-op/")):
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
                "is_developer": bool(user.get("is_developer")),
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
        from helmlog.email import smtp_configured

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
            user_id = await storage.create_user(
                row["email"], None, row["role"], is_developer=bool(row.get("is_developer"))
            )
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
        from helmlog.email import send_device_alert, smtp_configured

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
        from helmlog.email import send_login_link_email, smtp_configured

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

    @app.get("/session/{session_id}", response_class=HTMLResponse, include_in_schema=False)
    async def session_detail_page(request: Request, session_id: int) -> Response:
        cur = await storage._conn().execute("SELECT name FROM races WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        session_name = row["name"] if row else f"Session {session_id}"
        return _templates.TemplateResponse(
            request,
            "session.html",
            _tpl_ctx(
                request,
                "/history",
                session_id=session_id,
                session_name=session_name,
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
        from helmlog.races import configured_tz

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
            f"<td>{'&#9989;' if u.get('is_developer') else '—'}</td>"
            f"<td>{_local_ts(u['last_seen'])}</td>"
            f'<td><button onclick="changeRole({u["id"]})" style="cursor:pointer;background:none;border:1px solid #2563eb;color:#7eb8f7;border-radius:4px;padding:2px 8px;font-size:.8rem">Change role</button>'  # noqa: E501
            f' <button onclick="toggleDev({u["id"]},{1 if not u.get("is_developer") else 0})" style="cursor:pointer;background:none;border:1px solid #d97706;color:#fbbf24;border-radius:4px;padding:2px 8px;font-size:.8rem">{"Remove dev" if u.get("is_developer") else "Make dev"}</button></td>'  # noqa: E501
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
        is_developer: str = Form(default=""),
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        if role not in ("admin", "crew", "viewer"):
            raise HTTPException(status_code=422, detail="Invalid role")
        email = email.strip().lower()
        if not email:
            raise HTTPException(status_code=422, detail="email must not be blank")
        clean_name = name.strip() or None
        dev_flag = is_developer in ("1", "on", "true")
        token = generate_token()
        base = str(request.base_url).rstrip("/")
        await storage.create_invite_token(
            token, email, role, _user["id"], invite_expires_at(), is_developer=dev_flag
        )
        invite_url = f"{base}/login?token={token}"
        dev_label = " +developer" if dev_flag else ""
        await _audit(request, "user.invite", detail=f"{email} as {role}{dev_label}", user=_user)

        from helmlog.email import send_welcome_email, smtp_configured

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

    @app.put("/admin/users/{user_id}/developer", status_code=204, include_in_schema=False)
    async def admin_update_developer(
        request: Request,
        user_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        user = await storage.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        is_dev = bool(body.get("is_developer"))
        await storage.update_user_developer(user_id, is_dev)
        await _audit(
            request, "user.developer", detail=f"user={user_id} is_developer={is_dev}", user=_user
        )

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

        import helmlog.cameras as cameras_mod

        statuses = await asyncio.gather(
            *(cameras_mod.get_status(cam) for cam in cams),
            return_exceptions=True,
        )
        result: list[dict[str, Any]] = []
        for cam, st in zip(cams, statuses, strict=True):
            # Mask WiFi passwords in API responses (#210)
            masked_pw = "••••••••" if cam.wifi_password else None
            if isinstance(st, BaseException):
                result.append(
                    {
                        "name": cam.name,
                        "ip": cam.ip,
                        "model": cam.model,
                        "wifi_ssid": cam.wifi_ssid,
                        "wifi_password": masked_pw,
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
                        "wifi_password": masked_pw,
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
        import helmlog.cameras as cameras_mod

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
        import helmlog.cameras as cameras_mod

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
    # /admin/network (#256)
    # ------------------------------------------------------------------

    @app.get("/admin/network", response_class=HTMLResponse, include_in_schema=False)
    async def admin_network_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        return _templates.TemplateResponse(
            request, "admin/network.html", _tpl_ctx(request, "/admin/network")
        )

    @app.get("/api/network/status")
    async def api_network_status(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Return WLAN status, interface list, and internet connectivity."""
        import helmlog.network as net_mod

        wlan_status, interfaces, internet = await asyncio.gather(
            net_mod.get_wlan_status(),
            net_mod.list_interfaces(),
            net_mod.check_internet(),
        )
        return JSONResponse(
            {
                "wlan": {
                    "connected": wlan_status.connected,
                    "ssid": wlan_status.ssid,
                    "ip_address": wlan_status.ip_address,
                    "signal_strength": wlan_status.signal_strength,
                },
                "interfaces": [
                    {"name": i.name, "state": i.state, "ip_address": i.ip_address}
                    for i in interfaces
                ],
                "internet": internet,
            }
        )

    @app.get("/api/network/profiles")
    async def api_list_network_profiles(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """List all WLAN profiles (camera + non-camera)."""
        # Camera networks from cameras table
        camera_rows = await storage.list_cameras()
        camera_profiles = [
            {
                "id": f"camera:{r['name']}",
                "name": f"{r['name']} — {r['wifi_ssid']}",
                "ssid": r["wifi_ssid"],
                "source": "camera",
                "is_default": False,
            }
            for r in camera_rows
            if r.get("wifi_ssid")
        ]
        # Non-camera profiles from wlan_profiles table
        wlan_rows = await storage.list_wlan_profiles()
        wlan_profiles = [
            {
                "id": f"profile:{r['id']}",
                "name": r["name"],
                "ssid": r["ssid"],
                "source": "saved",
                "is_default": bool(r["is_default"]),
            }
            for r in wlan_rows
        ]
        return JSONResponse(camera_profiles + wlan_profiles)

    @app.post("/api/network/profiles", status_code=201)
    async def api_add_network_profile(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Add a non-camera WLAN profile."""
        body = await request.json()
        name = body.get("name", "").strip()
        ssid = body.get("ssid", "").strip()
        password = body.get("password", "").strip() or None
        is_default = bool(body.get("is_default", False))
        if not name or not ssid:
            raise HTTPException(422, detail="name and ssid are required")
        pid = await storage.add_wlan_profile(name, ssid, password, is_default)
        await _audit(request, "network.profile.add", detail=name, user=_user)
        return JSONResponse({"id": pid, "name": name, "ssid": ssid}, status_code=201)

    @app.put("/api/network/profiles/{profile_id}")
    async def api_update_network_profile(
        request: Request,
        profile_id: int,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Update a non-camera WLAN profile."""
        body = await request.json()
        name = body.get("name", "").strip()
        ssid = body.get("ssid", "").strip()
        password = body.get("password", "").strip() or None
        is_default = bool(body.get("is_default", False))
        if not name or not ssid:
            raise HTTPException(422, detail="name and ssid are required")
        ok = await storage.update_wlan_profile(profile_id, name, ssid, password, is_default)
        if not ok:
            raise HTTPException(404, detail="Profile not found")
        await _audit(request, "network.profile.update", detail=name, user=_user)
        return JSONResponse({"id": profile_id, "name": name, "ssid": ssid})

    @app.delete("/api/network/profiles/{profile_id}", status_code=204)
    async def api_delete_network_profile(
        request: Request,
        profile_id: int,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        """Delete a non-camera WLAN profile."""
        ok = await storage.delete_wlan_profile(profile_id)
        if not ok:
            raise HTTPException(404, detail="Profile not found")
        await _audit(request, "network.profile.delete", detail=str(profile_id), user=_user)

    @app.post("/api/network/connect")
    async def api_network_connect(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Connect to a WLAN profile (camera or saved)."""
        import helmlog.network as net_mod

        body = await request.json()
        profile_id = body.get("profile_id", "")

        if str(profile_id).startswith("camera:"):
            camera_name = str(profile_id).removeprefix("camera:")
            cams = await storage.list_cameras()
            cam = next((c for c in cams if c["name"] == camera_name), None)
            if not cam or not cam.get("wifi_ssid"):
                raise HTTPException(404, detail="Camera network not found")
            result = await net_mod.connect_to_ssid(cam["wifi_ssid"], cam.get("wifi_password"))
        elif str(profile_id).startswith("profile:"):
            pid = int(str(profile_id).removeprefix("profile:"))
            profile = await storage.get_wlan_profile(pid)
            if not profile:
                raise HTTPException(404, detail="Profile not found")
            result = await net_mod.connect_to_ssid(profile["ssid"], profile.get("password"))
        else:
            raise HTTPException(422, detail="Invalid profile_id format")

        await _audit(request, "network.connect", detail=str(profile_id), user=_user)
        return JSONResponse(
            {
                "success": result.success,
                "ssid": result.ssid,
                "error": result.error,
            }
        )

    @app.post("/api/network/disconnect")
    async def api_network_disconnect(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Disconnect WLAN (Ethernet-only mode)."""
        import helmlog.network as net_mod

        result = await net_mod.disconnect_wlan()
        await _audit(request, "network.disconnect", user=_user)
        return JSONResponse({"success": result.success, "error": result.error})

    # ------------------------------------------------------------------
    # /api/state
    # ------------------------------------------------------------------

    @app.get("/api/state")
    async def api_state(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.races import Race as _Race
        from helmlog.races import configured_tz, default_event_for_date, local_today, local_weekday

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
    async def api_instruments(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        data = await storage.latest_instruments()
        return JSONResponse(data)

    # ------------------------------------------------------------------
    # /api/polar/current
    # ------------------------------------------------------------------

    @app.get("/api/polar/current")
    async def api_polar_current(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        import helmlog.polar as _polar

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
        from helmlog.races import local_today

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
        from helmlog.races import build_race_name, default_event_for_date

        if session_type not in ("race", "practice"):
            raise HTTPException(
                status_code=422,
                detail="session_type must be 'race' or 'practice'",
            )

        from helmlog.races import local_today

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
            from helmlog.audio import AudioDeviceNotFoundError

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
            import helmlog.cameras as cameras_mod

            try:
                statuses = await cameras_mod.start_all(cams, rid, storage)
                for s in statuses:
                    if s.error:
                        logger.warning("Camera {} failed to start: {}", s.name, s.error)
                    else:
                        logger.info("Camera {} recording started", s.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera start_all failed: {}", exc)

        async def _network_auto_switch_start() -> None:
            import helmlog.network as net_mod

            try:
                result = await net_mod.auto_switch_for_race_start(storage)
                if result and not result.success:
                    logger.warning("WLAN auto-switch failed: {}", result.error)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WLAN auto-switch error: {}", exc)

        asyncio.ensure_future(_start_cameras(race.id))
        asyncio.ensure_future(_network_auto_switch_start())
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
            import helmlog.cameras as cameras_mod

            try:
                statuses = await cameras_mod.stop_all(cams, rid, storage)
                for s in statuses:
                    if s.error:
                        logger.warning("Camera {} failed to stop: {}", s.name, s.error)
                    else:
                        logger.info("Camera {} recording stopped", s.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera stop_all failed: {}", exc)

        async def _network_auto_switch_end() -> None:
            import helmlog.network as net_mod

            try:
                result = await net_mod.auto_switch_for_race_end(storage)
                if result and not result.success:
                    logger.warning("WLAN auto-revert failed: {}", result.error)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WLAN auto-revert error: {}", exc)

        asyncio.ensure_future(_stop_cameras(race_id))
        asyncio.ensure_future(_network_auto_switch_end())

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
    @limiter.limit("20/minute")
    async def api_export_race(
        request: Request,
        race_id: int,
        fmt: str,
        gps_precision: int | None = Query(default=None, ge=0, le=8),
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> FileResponse:
        """Export race data. Optional gps_precision (0-8 decimal places) reduces GPS accuracy (#203)."""
        if fmt not in ("csv", "gpx", "json"):
            raise HTTPException(status_code=400, detail="fmt must be csv, gpx, or json")

        from helmlog.races import local_today

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

            from helmlog.races import Race

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

        from helmlog.export import export_to_file

        suffix = f".{fmt}"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            out_path = f.name

        await export_to_file(
            storage,
            race.start_utc,
            race.end_utc,
            out_path,
            gps_precision=gps_precision,
        )

        filename = f"{race.name}.{fmt}"
        media = {
            "csv": "text/csv",
            "gpx": "application/gpx+xml",
            "json": "application/json",
        }[fmt]
        await _audit(request, "export.download", detail=f"{race.name}.{fmt}", user=_user)
        return FileResponse(
            out_path,
            media_type=media,
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------
    # /api/courses/marks  (CYC marks + computed buoy marks for map)
    # ------------------------------------------------------------------

    @app.get("/api/courses/marks")
    async def api_course_marks(
        wind_dir: float = 0.0,
        start_lat: float = 47.63,
        start_lon: float = -122.40,
        leg_nm: float = 1.0,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.courses import CYC_MARKS, compute_buoy_marks

        buoy = compute_buoy_marks(start_lat, start_lon, wind_dir, leg_nm)
        buoy_json = {k: {"name": m.name, "lat": m.lat, "lon": m.lon} for k, m in buoy.items()}
        cyc_json = {k: {"name": m.name, "lat": m.lat, "lon": m.lon} for k, m in CYC_MARKS.items()}
        return JSONResponse({"buoy_marks": buoy_json, "cyc_marks": cyc_json})

    # ------------------------------------------------------------------
    # /api/sessions/synthesize
    # ------------------------------------------------------------------

    @app.post("/api/sessions/synthesize")
    async def api_synthesize_session(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
        _dev: dict[str, Any] = Depends(require_developer),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.courses import (
            CourseMark,
            build_custom_course,
            build_triangle_course,
            build_wl_course,
            validate_course_marks,
        )
        from helmlog.races import build_race_name, local_today
        from helmlog.synthesize import (
            CollisionAvoidanceConfig,
            HeaderResponseConfig,
            SynthConfig,
            simulate,
        )

        body = await request.json()
        course_type = body.get("course_type", "windward_leeward")
        wind_dir = float(body.get("wind_direction", 0.0))
        tws_low = float(body.get("wind_speed_low", 8.0))
        tws_high = float(body.get("wind_speed_high", 14.0))
        shift_mag_lo = float(body.get("shift_magnitude_low", 5.0))
        shift_mag_hi = float(body.get("shift_magnitude_high", 14.0))
        start_lat = float(body.get("start_lat", 47.63))
        start_lon = float(body.get("start_lon", -122.40))
        leg_nm = float(body.get("leg_distance_nm", 1.0))
        laps = int(body.get("laps", 2))
        seed = int(body.get("seed", 42))
        raw_wind_seed = body.get("wind_seed")
        wind_seed: int | None = int(raw_wind_seed) if raw_wind_seed is not None else None
        mark_sequence = body.get("mark_sequence", "")
        peer_fingerprint: str | None = body.get("peer_fingerprint") or None
        peer_co_op_id: str | None = body.get("peer_co_op_id") or None
        raw_start_utc: str | None = body.get("start_utc")  # imported source session start

        # Collision avoidance — other boats' tracks to avoid (#246)
        raw_other_tracks: list[list[dict[str, Any]]] | None = body.get("other_tracks")
        min_separation_m = float(body.get("min_separation_m", 30.0))
        collision_avoidance = CollisionAvoidanceConfig(min_separation_m=min_separation_m)

        # Header response model — probabilistic tacking on wind shifts (#247)
        hr_raw = body.get("header_response")
        if isinstance(hr_raw, dict):
            header_response = HeaderResponseConfig(
                reaction_probability=float(hr_raw.get("reaction_probability", 0.70)),
                min_shift_threshold=(
                    float(hr_raw.get("min_shift_threshold_low", 3.0)),
                    float(hr_raw.get("min_shift_threshold_high", 8.0)),
                ),
                reaction_delay=(
                    float(hr_raw.get("reaction_delay_low", 10.0)),
                    float(hr_raw.get("reaction_delay_high", 45.0)),
                ),
                fatigue_start_frac=float(hr_raw.get("fatigue_start_frac", 0.70)),
                fatigue_floor=float(hr_raw.get("fatigue_floor", 0.40)),
            )
        else:
            header_response = HeaderResponseConfig()

        # Parse optional mark position overrides from user-dragged map markers
        raw_overrides = body.get("mark_overrides")
        mark_overrides: dict[str, tuple[float, float]] | None = None
        if isinstance(raw_overrides, dict):
            mark_overrides = {
                k: (float(v["lat"]), float(v["lon"]))
                for k, v in raw_overrides.items()
                if isinstance(v, dict) and "lat" in v and "lon" in v
            }

        if course_type == "windward_leeward":
            legs = build_wl_course(start_lat, start_lon, wind_dir, leg_nm, laps, mark_overrides)
        elif course_type == "triangle":
            legs = build_triangle_course(start_lat, start_lon, wind_dir, leg_nm, mark_overrides)
        elif course_type == "custom":
            if not mark_sequence:
                raise HTTPException(
                    status_code=422, detail="mark_sequence required for custom course"
                )
            try:
                legs = build_custom_course(mark_sequence, start_lat, start_lon, wind_dir, leg_nm)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        else:
            raise HTTPException(status_code=422, detail=f"Unknown course_type: {course_type}")

        # Validate all course marks are in navigable water (>6 ft deep)
        # Build marks from legs only — they already have correct overridden positions
        # and only include marks actually used in the course (#264)
        all_marks: dict[str, CourseMark] = {}
        for leg in legs:
            key = leg.target.name.split()[-1][0]
            if key not in all_marks:
                all_marks[key] = leg.target
        mark_warnings = validate_course_marks(all_marks)

        if raw_start_utc:
            start_time = datetime.fromisoformat(raw_start_utc.replace("Z", "+00:00"))
        else:
            start_time = datetime.now(UTC)
        config = SynthConfig(
            start_lat=start_lat,
            start_lon=start_lon,
            base_twd=wind_dir,
            tws_low=tws_low,
            tws_high=tws_high,
            shift_interval=(600.0, 1200.0),
            shift_magnitude=(shift_mag_lo, shift_mag_hi),
            legs=legs,
            seed=seed,
            start_time=start_time,
            wind_seed=wind_seed,
            header_response=header_response,
            collision_avoidance=collision_avoidance,
        )

        rows = await asyncio.to_thread(simulate, config, raw_other_tracks)
        if not rows:
            raise HTTPException(status_code=500, detail="Simulation produced no data points")

        today = local_today()
        date_str = today.isoformat()
        race_num = await storage.count_sessions_for_date(date_str, "synthesized") + 1
        source_id = str(uuid.uuid4())

        rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
        from helmlog.races import default_event_for_date

        custom_event = await storage.get_daily_event(date_str)
        default_event = default_event_for_date(today, rules)
        event = custom_event or default_event or "Synthesized"

        name = build_race_name(event, today, race_num, "synthesized")

        start_utc = rows[0].ts
        end_utc = rows[-1].ts

        race_id = await storage.import_race(
            name=name,
            event=event,
            race_num=race_num,
            date_str=date_str,
            start_utc=start_utc,
            end_utc=end_utc,
            session_type="synthesized",
            source="synthesized",
            source_id=source_id,
            peer_fingerprint=peer_fingerprint,
            peer_co_op_id=peer_co_op_id,
        )
        await storage.import_synthesized_data(rows, race_id=race_id)

        duration_s = (end_utc - start_utc).total_seconds()

        # Persist wind field params and course marks for later visualization
        await storage.save_synth_wind_params(
            race_id,
            {
                "seed": wind_seed if wind_seed is not None else seed,
                "base_twd": wind_dir,
                "tws_low": tws_low,
                "tws_high": tws_high,
                "shift_interval_lo": 600.0,
                "shift_interval_hi": 1200.0,
                "shift_magnitude_lo": shift_mag_lo,
                "shift_magnitude_hi": shift_mag_hi,
                "ref_lat": start_lat,
                "ref_lon": start_lon,
                "duration_s": duration_s,
                "course_type": course_type,
                "leg_distance_nm": leg_nm,
                "laps": laps if course_type == "windward_leeward" else None,
                "mark_sequence": mark_sequence if course_type == "custom" else None,
            },
        )
        marks_to_save = [
            {"mark_key": k, "mark_name": m.name, "lat": m.lat, "lon": m.lon}
            for k, m in all_marks.items()
        ]
        await storage.save_synth_course_marks(race_id, marks_to_save)
        detail = name + (f" [peer={peer_fingerprint}]" if peer_fingerprint else "")
        await _audit(request, "session.synthesize", detail=detail, user=_user)

        resp: dict[str, Any] = {
            "id": race_id,
            "name": name,
            "points": len(rows),
            "duration_s": round(duration_s, 1),
        }
        if mark_warnings:
            resp["mark_warnings"] = mark_warnings
        return JSONResponse(resp, status_code=201)

    # ------------------------------------------------------------------
    # /api/sessions/{id}/track  (GeoJSON track for map display)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/track")
    @limiter.limit("30/minute")
    async def api_session_track(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return GPS track as GeoJSON for map display."""
        db = storage._conn()
        cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Race not found")
        start_utc = row["start_utc"]
        end_utc = row["end_utc"] or start_utc

        # Prefer race_id filter (exact match for synthesized sessions);
        # fall back to time-range query for real instrument data.
        rid_cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE race_id = ?", (session_id,)
        )
        rid_row = await rid_cur.fetchone()
        has_race_id = rid_row["cnt"] > 0 if rid_row else False

        if has_race_id:
            pos_cur = await db.execute(
                "SELECT latitude_deg, longitude_deg, ts FROM positions"
                " WHERE race_id = ? ORDER BY ts",
                (session_id,),
            )
        else:
            pos_cur = await db.execute(
                "SELECT latitude_deg, longitude_deg, ts FROM positions"
                " WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (start_utc, end_utc),
            )
        positions = await pos_cur.fetchall()
        if not positions:
            return JSONResponse({"type": "FeatureCollection", "features": []})

        coords = [[r["longitude_deg"], r["latitude_deg"]] for r in positions]
        timestamps = [
            t if "+" in t or t.endswith("Z") else t + "Z" for r in positions if (t := r["ts"])
        ]
        feature = {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "session_id": session_id,
                "points": len(coords),
                "timestamps": timestamps,
            },
        }
        return JSONResponse({"type": "FeatureCollection", "features": [feature]})

    # ------------------------------------------------------------------
    # /api/sessions/{id}  (single session detail)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/detail")
    async def api_session_detail(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return full metadata for a single session."""
        db = storage._conn()
        cur = await db.execute(
            "SELECT r.id, r.name, r.event, r.race_num, r.date,"
            " r.start_utc, r.end_utc, r.session_type,"
            " r.peer_fingerprint, r.peer_co_op_id,"
            " (SELECT COUNT(*) > 0 FROM positions p"
            "   WHERE p.ts >= r.start_utc AND p.ts <= COALESCE(r.end_utc, r.start_utc)"
            " ) AS has_track,"
            " (SELECT rv.youtube_url FROM race_videos rv"
            "   WHERE rv.race_id = r.id LIMIT 1) AS first_video_url"
            " FROM races r WHERE r.id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")

        start_utc = datetime.fromisoformat(row["start_utc"])
        end_utc = datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None
        duration_s = (end_utc - start_utc).total_seconds() if end_utc else None

        # Check for audio
        acur = await db.execute(
            "SELECT id FROM audio_sessions WHERE race_id = ? AND session_type IN ('race','practice')",
            (session_id,),
        )
        arow = await acur.fetchone()

        # Check for wind field params (synthesized sessions)
        wf_cur = await db.execute(
            "SELECT 1 FROM synth_wind_params WHERE session_id = ?",
            (session_id,),
        )
        has_wind_field = await wf_cur.fetchone() is not None

        return JSONResponse(
            {
                "id": row["id"],
                "type": row["session_type"],
                "name": row["name"],
                "event": row["event"],
                "race_num": row["race_num"],
                "date": row["date"],
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat() if end_utc else None,
                "duration_s": round(duration_s, 1) if duration_s is not None else None,
                "has_track": bool(row["has_track"]),
                "first_video_url": row["first_video_url"],
                "has_audio": arow is not None,
                "audio_session_id": arow["id"] if arow else None,
                "peer_fingerprint": row["peer_fingerprint"],
                "has_wind_field": has_wind_field,
            }
        )

    # ------------------------------------------------------------------
    # /api/sessions/{id}/wind-field  (spatial wind grid for visualization)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/wind-field")
    async def api_session_wind_field(
        session_id: int,
        elapsed_s: float = 0.0,
        grid_size: int = 20,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return a spatial grid of TWD/TWS values and course marks."""
        from helmlog.wind_field import WindField

        grid_size = min(max(grid_size, 5), 40)
        params = await storage.get_synth_wind_params(session_id)
        if params is None:
            raise HTTPException(status_code=404, detail="No wind field for this session")

        marks = await storage.get_synth_course_marks(session_id)
        elapsed_s = max(0.0, min(elapsed_s, params["duration_s"]))

        # Compute bounding box from marks + 0.5 nm padding on all sides
        import math

        pad_nm = 0.5
        if marks:
            mark_lats = [m["lat"] for m in marks]
            mark_lons = [m["lon"] for m in marks]
            center_lat = (min(mark_lats) + max(mark_lats)) / 2
            cos_ref = math.cos(math.radians(center_lat))
            lat_min = min(mark_lats) - pad_nm / 60.0
            lat_max = max(mark_lats) + pad_nm / 60.0
            lon_min = min(mark_lons) - pad_nm / 60.0 / cos_ref
            lon_max = max(mark_lons) + pad_nm / 60.0 / cos_ref
        else:
            cos_ref = math.cos(math.radians(params["ref_lat"]))
            lat_min = params["ref_lat"] - pad_nm / 60.0
            lat_max = params["ref_lat"] + pad_nm / 60.0
            lon_min = params["ref_lon"] - pad_nm / 60.0 / cos_ref
            lon_max = params["ref_lon"] + pad_nm / 60.0 / cos_ref

        # Capture bounds for the thread
        bounds = (lat_min, lat_max, lon_min, lon_max)

        def _compute() -> list[dict[str, float]]:
            wf = WindField(
                base_twd=params["base_twd"],
                tws_low=params["tws_low"],
                tws_high=params["tws_high"],
                duration_s=params["duration_s"],
                shift_interval=(params["shift_interval_lo"], params["shift_interval_hi"]),
                shift_magnitude=(params["shift_magnitude_lo"], params["shift_magnitude_hi"]),
                ref_lat=params["ref_lat"],
                ref_lon=params["ref_lon"],
                seed=params["seed"],
            )
            b_lat_min, b_lat_max, b_lon_min, b_lon_max = bounds

            cells: list[dict[str, float]] = []
            for r in range(grid_size):
                lat = b_lat_min + (b_lat_max - b_lat_min) * r / (grid_size - 1)
                for c in range(grid_size):
                    lon = b_lon_min + (b_lon_max - b_lon_min) * c / (grid_size - 1)
                    twd, tws = wf.at(elapsed_s, lat, lon)
                    cells.append(
                        {
                            "lat": round(lat, 6),
                            "lon": round(lon, 6),
                            "twd": round(twd, 1),
                            "tws": round(tws, 2),
                        }
                    )
            return cells

        cells = await asyncio.to_thread(_compute)

        return JSONResponse(
            {
                "elapsed_s": elapsed_s,
                "duration_s": params["duration_s"],
                "base_twd": params["base_twd"],
                "tws_low": params["tws_low"],
                "tws_high": params["tws_high"],
                "grid": {
                    "rows": grid_size,
                    "cols": grid_size,
                    "lat_min": round(lat_min, 6),
                    "lat_max": round(lat_max, 6),
                    "lon_min": round(lon_min, 6),
                    "lon_max": round(lon_max, 6),
                    "cells": cells,
                },
                "marks": marks,
            }
        )

    # ------------------------------------------------------------------
    # /api/sessions/{id}/wind-timeseries  (comparative wind time series)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/wind-timeseries")
    async def api_session_wind_timeseries(
        session_id: int,
        step_s: int = 10,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return TWD/TWS time series at port, center, and starboard positions."""
        from helmlog.wind_field import WindField

        step_s = min(max(step_s, 5), 60)
        params = await storage.get_synth_wind_params(session_id)
        if params is None:
            raise HTTPException(status_code=404, detail="No wind field for this session")

        def _compute() -> dict[str, Any]:
            import math

            wf = WindField(
                base_twd=params["base_twd"],
                tws_low=params["tws_low"],
                tws_high=params["tws_high"],
                duration_s=params["duration_s"],
                shift_interval=(params["shift_interval_lo"], params["shift_interval_hi"]),
                shift_magnitude=(params["shift_magnitude_lo"], params["shift_magnitude_hi"]),
                ref_lat=params["ref_lat"],
                ref_lon=params["ref_lon"],
                seed=params["seed"],
            )
            cos_ref = math.cos(math.radians(params["ref_lat"]))
            offset_lon = 0.3 / 60.0 / cos_ref  # 0.3 nm cross-course

            positions = [
                {
                    "label": "Port side",
                    "lat": params["ref_lat"],
                    "lon": round(params["ref_lon"] - offset_lon, 6),
                },
                {"label": "Center", "lat": params["ref_lat"], "lon": params["ref_lon"]},
                {
                    "label": "Starboard side",
                    "lat": params["ref_lat"],
                    "lon": round(params["ref_lon"] + offset_lon, 6),
                },
            ]

            series: list[dict[str, Any]] = []
            t = 0.0
            while t <= params["duration_s"]:
                twd_vals = []
                tws_vals = []
                for p in positions:
                    twd, tws = wf.at(t, p["lat"], p["lon"])
                    twd_vals.append(round(twd, 1))
                    tws_vals.append(round(tws, 2))
                series.append({"t": round(t, 1), "twd": twd_vals, "tws": tws_vals})
                t += step_s

            return {"positions": positions, "series": series}

        result = await asyncio.to_thread(_compute)

        return JSONResponse(
            {
                "duration_s": params["duration_s"],
                "step_s": step_s,
                "base_twd": params["base_twd"],
                "positions": result["positions"],
                "series": result["series"],
            }
        )

    # ------------------------------------------------------------------
    # /api/sessions/{id}/polar  (session polar comparison)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/polar")
    async def api_session_polar(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        import helmlog.polar as _polar

        data = await _polar.session_polar_comparison(storage, session_id)
        if data is None:
            return JSONResponse(
                {"cells": [], "tws_bins": [], "twa_bins": [], "session_sample_count": 0}
            )
        return JSONResponse(
            {
                "cells": [
                    {
                        "tws": c.tws_bin,
                        "twa": c.twa_bin,
                        "baseline_mean": c.baseline_mean_bsp,
                        "baseline_p90": c.baseline_p90_bsp,
                        "session_mean": c.session_mean_bsp,
                        "samples": c.session_sample_count,
                        "delta": c.delta,
                    }
                    for c in data.cells
                ],
                "tws_bins": data.tws_bins,
                "twa_bins": data.twa_bins,
                "session_sample_count": data.session_sample_count,
            }
        )

    # ------------------------------------------------------------------
    # /api/polar/rebuild
    # ------------------------------------------------------------------

    @app.post("/api/polar/rebuild", status_code=200)
    async def api_polar_rebuild(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        import helmlog.polar as _polar

        count = await _polar.build_polar_baseline(storage)
        await _audit(request, "polar.rebuild", detail=f"{count} bins", user=_user)
        return JSONResponse({"bins": count})

    # ------------------------------------------------------------------
    # /api/sessions/{id}/maneuvers  (maneuver detection)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/maneuvers")
    async def api_session_maneuvers(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return detected maneuvers for a session, with nearest GPS position."""
        import json as _json

        rows = await storage.get_session_maneuvers(session_id)

        # Enrich with nearest position so the front end can place map markers.
        # Find the closest position by checking both before and after the
        # maneuver timestamp and picking the one with the smallest time gap.
        # Scope queries to the session's time range so positions from other
        # sessions are never returned.
        db = storage._conn()
        race_cur = await db.execute(
            "SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,)
        )
        race_row = await race_cur.fetchone()
        session_start = str(race_row["start_utc"])[:19] if race_row else None
        session_end = str(race_row["end_utc"])[:19] if race_row else None

        enriched = []
        for row in rows:
            d = dict(row)
            ts_str = str(d["ts"])[:19]
            # Position just before or at the maneuver time (within session)
            before_cur = await db.execute(
                "SELECT latitude_deg, longitude_deg, ts FROM positions"
                " WHERE ts <= ? AND ts >= ? AND race_id = ?"
                " ORDER BY ts DESC LIMIT 1",
                (ts_str, session_start, session_id),
            )
            before = await before_cur.fetchone()
            # Position just after the maneuver time (within session)
            after_cur = await db.execute(
                "SELECT latitude_deg, longitude_deg, ts FROM positions"
                " WHERE ts > ? AND ts <= ? AND race_id = ?"
                " ORDER BY ts LIMIT 1",
                (ts_str, session_end, session_id),
            )
            after = await after_cur.fetchone()

            # Pick the closer one by time delta
            pos = None
            if before and after:
                from datetime import datetime as _dt

                t_man = _dt.fromisoformat(ts_str)
                t_before = _dt.fromisoformat(str(before["ts"])[:19])
                t_after = _dt.fromisoformat(str(after["ts"])[:19])
                pos = before if (t_man - t_before) <= (t_after - t_man) else after
            else:
                pos = before or after

            d["lat"] = float(pos["latitude_deg"]) if pos else None
            d["lon"] = float(pos["longitude_deg"]) if pos else None
            if d.get("details") and isinstance(d["details"], str):
                try:
                    d["details"] = _json.loads(d["details"])
                except Exception:
                    d["details"] = {}
            enriched.append(d)

        return JSONResponse(enriched)

    @app.post("/api/sessions/{session_id}/detect-maneuvers", status_code=202)
    async def api_detect_maneuvers(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Trigger maneuver detection (or re-detection) for a session.

        Returns immediately with the count of detected maneuvers.
        """
        from helmlog.maneuver_detector import detect_maneuvers

        # Verify session exists
        db = storage._conn()
        cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Session not found")

        maneuvers = await detect_maneuvers(storage, session_id)
        return JSONResponse(
            {
                "session_id": session_id,
                "detected": len(maneuvers),
                "tacks": sum(1 for m in maneuvers if m.type == "tack"),
                "gybes": sum(1 for m in maneuvers if m.type == "gybe"),
                "roundings": sum(1 for m in maneuvers if m.type == "rounding"),
            },
            status_code=202,
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
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        if type is not None and type not in ("race", "practice", "debrief", "synthesized"):
            raise HTTPException(
                status_code=422,
                detail="type must be 'race', 'practice', 'debrief', or 'synthesized'",
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
    async def api_list_races(
        date: str | None = None,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        if date is None:
            from helmlog.races import local_today

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
    async def api_get_crew(
        race_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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
    async def api_recent_sailors(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        sailors = await storage.get_recent_sailors()
        return JSONResponse({"sailors": sailors})

    # ------------------------------------------------------------------
    # /api/boats
    # ------------------------------------------------------------------

    @app.get("/api/boats")
    async def api_list_boats(
        q: str | None = None,
        exclude_race: int | None = None,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
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
    async def api_get_results(
        race_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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
        from helmlog import influx

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
        from helmlog import influx

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
    async def api_list_notes(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        race_id, audio_session_id = await _resolve_session(session_id)
        notes = await storage.list_notes(race_id=race_id, audio_session_id=audio_session_id)
        return JSONResponse(notes)

    @app.delete("/api/notes/{note_id}", status_code=204)
    async def api_delete_note(
        request: Request,
        note_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        found, photo_path = await storage.delete_note_with_file(note_id)
        if not found:
            raise HTTPException(status_code=404, detail="Note not found")
        if photo_path:
            # Clean up the physical photo file (#205)
            notes_dir = Path(os.environ.get("NOTES_DIR", "data/notes"))
            full_path = notes_dir / photo_path
            if full_path.exists():
                await asyncio.to_thread(full_path.unlink)
                logger.info("Deleted photo file: {}", full_path)
        await _audit(request, "note.delete", detail=str(note_id), user=_user)

    @app.get("/api/notes/settings-keys")
    async def api_settings_keys(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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
        from helmlog.video import VideoSession  # local import to avoid circular deps

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
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
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
        from helmlog.video import VideoLinker

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
    # /api/boat-settings
    # ------------------------------------------------------------------

    from helmlog.boat_settings import (  # noqa: E501, PLC0415
        CATEGORY_ORDER,
        PARAMETER_NAMES,
        WEIGHT_DISTRIBUTION_PRESETS,
        parameters_by_category,
    )

    @app.get("/api/boat-settings/parameters")
    async def api_boat_settings_parameters(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return the canonical parameter definitions grouped by category."""
        grouped = parameters_by_category()
        result = []
        for cat, label in CATEGORY_ORDER:
            params = [
                {"name": p.name, "label": p.label, "unit": p.unit, "input_type": p.input_type}
                for p in grouped[cat]
            ]
            result.append({"category": cat, "label": label, "parameters": params})
        return JSONResponse(
            {"categories": result, "weight_distribution_presets": list(WEIGHT_DISTRIBUTION_PRESETS)}
        )

    @app.post("/api/boat-settings", status_code=201)
    async def api_create_boat_settings(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Create one or more boat setting entries.

        Body: ``{"race_id": int|null, "source": str, "entries": [{"ts": str, "parameter": str, "value": str}, ...]}``
        """
        body = await request.json()
        race_id: int | None = body.get("race_id")
        source: str = body.get("source", "manual")
        extraction_run_id: int | None = body.get("extraction_run_id")
        entries: list[dict[str, str]] = body.get("entries", [])
        if not entries:
            raise HTTPException(status_code=400, detail="entries is required and must be non-empty")
        for e in entries:
            if not all(k in e for k in ("ts", "parameter", "value")):
                raise HTTPException(
                    status_code=400, detail="Each entry must have ts, parameter, and value"
                )
            if e["parameter"] not in PARAMETER_NAMES:
                raise HTTPException(
                    status_code=400, detail=f"Unknown parameter: {e['parameter']!r}"
                )
        try:
            ids = await storage.create_boat_settings(race_id, entries, source, extraction_run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _audit(request, "boat_settings.create", detail=f"{len(ids)} entries", user=_user)
        return JSONResponse({"ids": ids}, status_code=201)

    @app.get("/api/boat-settings")
    async def api_list_boat_settings(
        race_id: int | None = Query(None),
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """List all boat settings for a race, ordered by timestamp."""
        rows = await storage.list_boat_settings(race_id)
        return JSONResponse(rows)

    @app.get("/api/boat-settings/current")
    async def api_current_boat_settings(
        race_id: int | None = Query(None),
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return the latest value for each parameter in a race."""
        rows = await storage.current_boat_settings(race_id)
        return JSONResponse(rows)

    @app.delete("/api/boat-settings/extraction-run/{extraction_run_id}", status_code=200)
    async def api_delete_boat_settings_extraction_run(
        request: Request,
        extraction_run_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Delete all settings from a specific extraction run."""
        count = await storage.delete_boat_settings_extraction_run(extraction_run_id)
        await _audit(
            request,
            "boat_settings.delete_run",
            detail=f"run={extraction_run_id} deleted={count}",
            user=_user,
        )
        return JSONResponse({"deleted": count})

    # ------------------------------------------------------------------
    # /api/sails  &  /api/sessions/{id}/sails
    # ------------------------------------------------------------------

    from helmlog.storage import _SAIL_TYPES  # noqa: PLC0415

    @app.get("/api/sails")
    async def api_list_sails(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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
    async def api_get_session_sails(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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
    @limiter.limit("10/minute")
    async def download_audio(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> FileResponse:
        """Download a WAV file as an attachment."""
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        path = Path(row["file_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found on disk")
        await _audit(request, "audio.download", detail=str(session_id), user=_user)
        return FileResponse(
            path,
            media_type="audio/wav",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )

    @app.get("/api/audio/{session_id}/stream")
    @limiter.limit("30/minute")
    async def stream_audio(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> FileResponse:
        """Stream a WAV file; Starlette handles Range headers for seekable playback."""
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        path = Path(row["file_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found on disk")
        await _audit(request, "audio.stream", detail=str(session_id), user=_user)
        return FileResponse(path, media_type="audio/wav")

    # ------------------------------------------------------------------
    # /api/system-health
    # ------------------------------------------------------------------

    @app.get("/api/system-health")
    async def api_system_health(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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

        from helmlog.storage import get_effective_setting
        from helmlog.transcribe import transcribe_session

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
    async def api_get_transcript(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Poll transcription status and retrieve the transcript text when done.

        Applies speaker anonymization map if present (#197).
        """
        import json as _json

        t = await storage.get_transcript_with_anon(session_id)
        if t is None:
            raise HTTPException(status_code=404, detail="No transcript job found for this session")
        if t.get("segments_json"):
            t["segments"] = _json.loads(t["segments_json"])
        del t["segments_json"]
        # Remove internal anon map from response
        t.pop("speaker_anon_map", None)
        return JSONResponse(t)

    # ------------------------------------------------------------------
    # Tags (#99)
    # ------------------------------------------------------------------

    @app.get("/api/tags")
    async def api_list_tags(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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
    async def api_get_session_tags(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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
    async def api_get_note_tags(
        note_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
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

    # ------------------------------------------------------------------
    # DELETE /api/sessions/{id}  (#194 — session/data deletion)
    # ------------------------------------------------------------------

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def api_delete_session(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        """Delete a session and all related data (admin only)."""
        cur = await storage._conn().execute("SELECT name FROM races WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        files = await storage.delete_race_session(session_id)
        # Clean up physical files
        for f in files:
            p = Path(f)
            if p.exists():
                await asyncio.to_thread(p.unlink)
                logger.info("Deleted file: {}", p)
        await _audit(request, "session.delete", detail=row["name"], user=_user)

    # ------------------------------------------------------------------
    # DELETE /api/audio/{id}  (#196 — audio deletion)
    # ------------------------------------------------------------------

    @app.delete("/api/audio/{session_id}", status_code=204)
    async def api_delete_audio(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        """Delete an audio session and its WAV file."""
        file_path = await storage.delete_audio_session(session_id)
        if file_path is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        p = Path(file_path)
        if p.exists():
            await asyncio.to_thread(p.unlink)
            logger.info("Deleted audio file: {}", p)
        await _audit(request, "audio.delete", detail=str(session_id), user=_user)

    # ------------------------------------------------------------------
    # DELETE /api/users/{id}  (#195 — user deletion)
    # ------------------------------------------------------------------

    @app.delete("/api/users/{user_id}", status_code=204)
    async def api_delete_user(
        request: Request,
        user_id: int,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        """Anonymize and delete a user account (admin only)."""
        target = await storage.get_user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        # Delete avatar file
        avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
        avatar_file = avatar_dir / f"{user_id}.jpg"
        if avatar_file.exists():
            await asyncio.to_thread(avatar_file.unlink)
        await storage.delete_user(user_id)
        await _audit(request, "user.delete", detail=f"user_id={user_id}", user=_user)

    @app.delete("/api/me", status_code=204)
    async def api_delete_me(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> None:
        """Self-delete: anonymize and remove own account."""
        user_id = _user.get("id")
        if user_id is None:
            raise HTTPException(status_code=400, detail="Cannot delete mock user")
        avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
        avatar_file = avatar_dir / f"{user_id}.jpg"
        if avatar_file.exists():
            await asyncio.to_thread(avatar_file.unlink)
        await storage.delete_user(user_id)
        await _audit(request, "user.self_delete", detail=f"user_id={user_id}", user=_user)

    # ------------------------------------------------------------------
    # Speaker anonymization (#197)
    # ------------------------------------------------------------------

    @app.post("/api/audio/{session_id}/transcript/anonymize-speaker", status_code=200)
    async def api_anonymize_speaker(
        request: Request,
        session_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Anonymize a speaker label in a diarized transcript."""
        speaker_label = (body.get("speaker_label") or "").strip()
        if not speaker_label:
            raise HTTPException(status_code=422, detail="speaker_label is required")
        # Find the transcript for this audio session
        t = await storage.get_transcript(session_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Transcript not found")
        found = await storage.anonymize_speaker(t["id"], speaker_label)
        if not found:
            raise HTTPException(status_code=404, detail="Transcript not found")
        await _audit(
            request,
            "transcript.anonymize_speaker",
            detail=f"session={session_id} speaker={speaker_label}",
            user=_user,
        )
        return JSONResponse({"anonymized": speaker_label})

    # ------------------------------------------------------------------
    # Crew consent (#202)
    # ------------------------------------------------------------------

    @app.get("/api/crew/consents")
    async def api_list_consents(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """List all crew consent records."""
        consents = await storage.list_crew_consents()
        return JSONResponse(consents)

    @app.get("/api/crew/{sailor_name}/consents")
    async def api_get_sailor_consents(
        sailor_name: str,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Get consent records for a specific sailor."""
        consents = await storage.get_crew_consents(sailor_name)
        return JSONResponse(consents)

    @app.put("/api/crew/{sailor_name}/consents", status_code=200)
    async def api_set_consent(
        request: Request,
        sailor_name: str,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Set or revoke consent for a sailor."""
        consent_type = (body.get("consent_type") or "").strip()
        if consent_type not in ("audio", "video", "name", "photo"):
            raise HTTPException(
                status_code=422, detail="consent_type must be audio/video/name/photo"
            )
        granted = bool(body.get("granted", True))
        row_id = await storage.set_crew_consent(sailor_name, consent_type, granted)
        action = "consent.grant" if granted else "consent.revoke"
        await _audit(request, action, detail=f"{sailor_name}/{consent_type}", user=_user)
        return JSONResponse(
            {
                "id": row_id,
                "sailor_name": sailor_name,
                "consent_type": consent_type,
                "granted": granted,
            }
        )

    @app.post("/api/crew/{sailor_name}/anonymize", status_code=200)
    async def api_anonymize_sailor(
        request: Request,
        sailor_name: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Anonymize a sailor's name across all sessions."""
        count = await storage.anonymize_sailor(sailor_name)
        await _audit(request, "sailor.anonymize", detail=sailor_name, user=_user)
        return JSONResponse({"anonymized": sailor_name, "rows_updated": count})

    # ------------------------------------------------------------------
    # Camera status for crew (#207)
    # ------------------------------------------------------------------

    @app.get("/api/cameras/status")
    async def api_camera_status_crew(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Return camera recording status (available to all authenticated users)."""
        cams = await _load_cameras()
        if not cams:
            return JSONResponse({"recording": False, "cameras": []})
        import helmlog.cameras as cameras_mod

        statuses = await asyncio.gather(
            *(cameras_mod.get_status(cam) for cam in cams),
            return_exceptions=True,
        )
        recording_cams: list[str] = []
        for cam, st in zip(cams, statuses, strict=True):
            if not isinstance(st, BaseException) and st.recording:
                recording_cams.append(cam.name)
        return JSONResponse(
            {
                "recording": bool(recording_cams),
                "cameras": recording_cams,
            }
        )

    # ------------------------------------------------------------------
    # Deployment management (#125)
    # ------------------------------------------------------------------

    @app.get("/admin/deployment", response_class=HTMLResponse, include_in_schema=False)
    async def admin_deployment_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        return _templates.TemplateResponse(
            request, "admin/deployment.html", _tpl_ctx(request, "/admin/deployment")
        )

    @app.get("/api/deployment/status")
    @limiter.limit("30/minute")
    async def api_deployment_status(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.deploy import DeployConfig, commits_behind, fetch_latest, get_running_version

        config = await DeployConfig.from_storage(storage)
        running = get_running_version()
        await fetch_latest(config)  # update origin refs before comparing
        behind = commits_behind(config)
        last = await storage.last_deployment()
        # Detect if on-disk code differs from what the running process loaded
        restart_needed = bool(_STARTUP_SHA and running["sha"] and running["sha"] != _STARTUP_SHA)
        # Detect if checked-out branch differs from tracked branch
        branch_mismatch = bool(running["branch"] and running["branch"] != config.branch)
        return JSONResponse(
            {
                "running": {**running, "startup_sha": _STARTUP_SHA},
                "branch": config.branch,
                "mode": config.mode,
                "poll_interval": config.poll_interval,
                "deploy_window": {
                    "start": config.window_start,
                    "end": config.window_end,
                },
                "commits_behind": behind,
                "update_available": behind > 0 or restart_needed or branch_mismatch,
                "restart_needed": restart_needed,
                "branch_mismatch": branch_mismatch,
                "last_deploy": last,
            }
        )

    @app.get("/api/deployment/changelog")
    @limiter.limit("10/minute")
    async def api_deployment_changelog(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.deploy import DeployConfig, get_changelog

        config = await DeployConfig.from_storage(storage)
        commits = await get_changelog(config)
        return JSONResponse({"commits": commits, "count": len(commits)})

    @app.post("/api/deployment/deploy")
    @limiter.limit("3/minute")
    async def api_deployment_deploy(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.deploy import DeployConfig, execute_deploy

        config = await DeployConfig.from_storage(storage)
        result = await execute_deploy(config)
        await storage.log_deployment(
            from_sha=result.get("from_sha", ""),
            to_sha=result.get("to_sha", ""),
            trigger="manual",
            status=result["status"],
            error=result.get("error"),
            user_id=_user.get("id"),
        )
        await _audit(
            request,
            "deployment.manual",
            detail=f"{result.get('from_sha', '')[:7]}→{result.get('to_sha', '')[:7]}",
            user=_user,
        )
        if result["status"] == "failed":
            raise HTTPException(status_code=500, detail=result.get("error", "Deploy failed"))
        return JSONResponse(result)

    @app.get("/api/deployment/branches")
    @limiter.limit("10/minute")
    async def api_deployment_branches(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.deploy import list_remote_branches

        branches = await list_remote_branches()
        return JSONResponse({"branches": branches})

    @app.put("/api/deployment/config")
    @limiter.limit("10/minute")
    async def api_deployment_config(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        body = await request.json()
        changed: list[str] = []
        if "mode" in body:
            mode = body["mode"]
            if mode not in ("explicit", "evergreen"):
                raise HTTPException(
                    status_code=400, detail="Mode must be 'explicit' or 'evergreen'"
                )
            await storage.set_setting("DEPLOY_MODE", mode)
            changed.append(f"mode={mode}")
        if "branch" in body:
            branch = str(body["branch"]).strip()
            if not branch:
                raise HTTPException(status_code=400, detail="Branch cannot be empty")
            await storage.set_setting("DEPLOY_BRANCH", branch)
            changed.append(f"branch={branch}")
        if "poll_interval" in body:
            poll = int(body["poll_interval"])
            if poll < 60:
                raise HTTPException(status_code=400, detail="Poll interval must be >= 60 seconds")
            await storage.set_setting("DEPLOY_POLL_INTERVAL", str(poll))
            changed.append(f"poll_interval={poll}")
        if "window_start" in body:
            val = body["window_start"]
            if val is None or val == "":
                await storage.delete_setting("DEPLOY_WINDOW_START")
            else:
                await storage.set_setting("DEPLOY_WINDOW_START", str(int(val)))
            changed.append(f"window_start={val}")
        if "window_end" in body:
            val = body["window_end"]
            if val is None or val == "":
                await storage.delete_setting("DEPLOY_WINDOW_END")
            else:
                await storage.set_setting("DEPLOY_WINDOW_END", str(int(val)))
            changed.append(f"window_end={val}")
        await _audit(request, "deployment.config", detail=", ".join(changed), user=_user)
        return JSONResponse({"status": "ok", "changed": changed})

    @app.get("/api/deployment/history")
    @limiter.limit("30/minute")
    async def api_deployment_history(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        deployments = await storage.list_deployments()
        return JSONResponse({"deployments": deployments})

    @app.get("/api/deployment/pipeline")
    @limiter.limit("10/minute")
    async def api_deployment_pipeline(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.deploy import get_pipeline_status

        pipeline = await get_pipeline_status()
        return JSONResponse(pipeline)

    @app.get("/api/deployment/promotions")
    @limiter.limit("10/minute")
    async def api_deployment_promotions(
        request: Request,
        tier: str | None = None,
        limit: int = 20,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.deploy import get_promotion_history

        promotions = await get_promotion_history(tier=tier, limit=limit)
        return JSONResponse({"promotions": promotions})

    @app.get("/api/deployment/pending")
    @limiter.limit("10/minute")
    async def api_deployment_pending(
        request: Request,
        from_tier: str = "stage",
        to_tier: str = "main",
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.deploy import get_pending_changes

        valid_tiers = {"main", "stage", "live"}
        if from_tier not in valid_tiers or to_tier not in valid_tiers:
            raise HTTPException(
                status_code=400, detail="Invalid tier — must be main, stage, or live"
            )
        commits = await get_pending_changes(from_tier=from_tier, to_tier=to_tier)
        return JSONResponse({"commits": commits, "count": len(commits)})

    # ------------------------------------------------------------------
    # Admin: Federation
    # ------------------------------------------------------------------

    @app.get("/admin/federation", response_class=HTMLResponse, include_in_schema=False)
    async def admin_federation_page(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> Response:
        return _templates.TemplateResponse(
            request,
            "admin/federation.html",
            _tpl_ctx(request, "/admin/federation"),
        )

    @app.get("/api/federation/identity")
    async def api_federation_identity(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        identity = await storage.get_boat_identity()
        boat_card_json: str | None = None
        if identity:
            try:
                from helmlog.federation import load_identity

                _, card = load_identity()
                boat_card_json = card.to_json()
                identity["owner_email"] = card.owner_email
            except FileNotFoundError:
                pass
        return JSONResponse(
            {
                "identity": identity,
                "boat_card_json": boat_card_json,
            }
        )

    @app.post("/api/federation/identity", status_code=201)
    async def api_federation_identity_init(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        body = await request.json()
        sail = body.get("sail_number", "").strip()
        name = body.get("boat_name", "").strip()
        email = body.get("owner_email") or None
        if not sail or not name:
            raise HTTPException(422, "sail_number and boat_name are required")

        from helmlog.federation import identity_exists, init_identity

        if identity_exists():
            raise HTTPException(409, "Identity already exists")

        card = init_identity(
            sail_number=sail,
            boat_name=name,
            owner_email=email,
        )
        await storage.save_boat_identity(
            pub_key=card.pub_key,
            fingerprint=card.fingerprint,
            sail_number=card.sail_number,
            boat_name=card.boat_name,
        )
        await _audit(
            request,
            "federation.identity.init",
            detail=f"{card.boat_name} ({card.fingerprint})",
            user=_user,
        )
        return JSONResponse(
            {
                "pub_key": card.pub_key,
                "fingerprint": card.fingerprint,
                "sail_number": card.sail_number,
                "boat_name": card.boat_name,
            },
            status_code=201,
        )

    @app.get("/api/federation/co-ops")
    async def api_federation_coops(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        memberships = await storage.list_co_op_memberships()
        result = []
        for m in memberships:
            peers = await storage.list_co_op_peers(m["co_op_id"])
            result.append({**m, "peers": peers})
        return JSONResponse({"co_ops": result})

    @app.post("/api/federation/co-ops", status_code=201)
    async def api_federation_coop_create(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.federation import create_co_op, load_identity

        try:
            private_key, card = load_identity()
        except FileNotFoundError:
            raise HTTPException(409, "Initialize identity first")  # noqa: B904

        if not card.owner_email:
            raise HTTPException(
                422,
                "Co-op requires an owner email. Re-initialize identity with an email address.",
            )

        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            raise HTTPException(422, "Co-op name is required")
        areas = body.get("areas") or []

        charter = create_co_op(private_key, card, name=name, areas=areas)

        from helmlog.federation import list_co_op_members

        members = list_co_op_members(charter.co_op_id)
        if members:
            await storage.save_co_op_membership(
                co_op_id=charter.co_op_id,
                co_op_name=charter.name,
                co_op_pub=card.pub_key,
                membership_json=members[0].to_json(),
                role="admin",
                joined_at=members[0].joined_at,
            )
            # Also save the creating boat as a peer so it appears in the member list
            await storage.save_co_op_peer(
                co_op_id=charter.co_op_id,
                boat_pub=card.pub_key,
                fingerprint=card.fingerprint,
                membership_json=members[0].to_json(),
                sail_number=card.sail_number,
                boat_name=card.boat_name,
            )
        await _audit(
            request,
            "federation.co_op.create",
            detail=f"{charter.name} ({charter.co_op_id})",
            user=_user,
        )
        return JSONResponse(charter.to_dict(), status_code=201)

    @app.post("/api/federation/co-ops/{co_op_id}/invite", status_code=201)
    async def api_federation_invite(
        request: Request,
        co_op_id: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        from helmlog.federation import BoatCard, load_identity, sign_membership

        try:
            private_key, admin_card = load_identity()
        except FileNotFoundError:
            raise HTTPException(409, "Initialize identity first")  # noqa: B904

        membership = await storage.get_co_op_membership(co_op_id)
        if not membership or membership["role"] != "admin":
            raise HTTPException(403, "You are not admin of this co-op")

        body = await request.json()
        required = ["pub", "fingerprint", "sail_number", "name"]
        missing = [f for f in required if not body.get(f)]
        if missing:
            raise HTTPException(
                422,
                f"Boat card missing required fields: {', '.join(missing)}",
            )

        invitee = BoatCard(
            pub_key=body["pub"],
            fingerprint=body["fingerprint"],
            sail_number=body["sail_number"],
            boat_name=body["name"],
            owner_email=body.get("owner_email"),
        )

        record = sign_membership(
            private_key,
            co_op_id=co_op_id,
            boat_card=invitee,
        )

        # Persist to filesystem
        from pathlib import Path

        identity_dir = Path.home() / ".helmlog" / "identity"
        members_dir = identity_dir.parent / "co-ops" / co_op_id / "members"
        members_dir.mkdir(parents=True, exist_ok=True)
        member_file = members_dir / f"{invitee.fingerprint}.json"
        member_file.write_text(record.to_json())

        # Persist to SQLite as peer
        await storage.save_co_op_peer(
            co_op_id=co_op_id,
            boat_pub=invitee.pub_key,
            fingerprint=invitee.fingerprint,
            membership_json=record.to_json(),
            sail_number=invitee.sail_number,
            boat_name=invitee.boat_name,
            tailscale_ip=body.get("tailscale_ip"),
        )
        await _audit(
            request,
            "federation.invite",
            detail=f"{invitee.boat_name} ({invitee.fingerprint}) → {co_op_id}",
            user=_user,
        )
        # Build invite bundle — the invitee imports this to join
        membership = await storage.get_co_op_membership(co_op_id)
        invite_bundle = {
            "co_op_id": co_op_id,
            "co_op_name": membership["co_op_name"] if membership else "",
            "admin_pub": admin_card.pub_key,
            "admin_fingerprint": admin_card.fingerprint,
            "admin_boat_name": admin_card.boat_name,
            "admin_sail_number": admin_card.sail_number,
            "admin_tailscale_ip": admin_card.tailscale_ip or "",
            "membership": record.to_dict(),
        }
        return JSONResponse(
            {
                "boat_name": invitee.boat_name,
                "fingerprint": invitee.fingerprint,
                "membership": record.to_dict(),
                "invite_bundle": invite_bundle,
            },
            status_code=201,
        )

    @app.post("/api/federation/join", status_code=201)
    async def api_federation_join(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Join a co-op using an invite bundle from the admin boat."""
        body = await request.json()
        co_op_id = body.get("co_op_id", "").strip()
        co_op_name = body.get("co_op_name", "").strip()
        admin_pub = body.get("admin_pub", "").strip()
        admin_fingerprint = body.get("admin_fingerprint", "").strip()
        membership_json = body.get("membership")

        if not all([co_op_id, co_op_name, admin_pub]):
            raise HTTPException(422, "Missing required fields in invite bundle")

        # Verify the membership signature before accepting the bundle
        if isinstance(membership_json, dict) and membership_json.get("admin_sig"):
            from helmlog.federation import MembershipRecord, verify_membership

            try:
                m = membership_json
                record = MembershipRecord(
                    co_op_id=m.get("co_op_id", ""),
                    boat_pub=m.get("boat_pub", ""),
                    sail_number=m.get("sail_number", ""),
                    boat_name=m.get("boat_name", ""),
                    role=m.get("role", "member"),
                    joined_at=m.get("joined_at", ""),
                    owner_email=m.get("owner_email"),
                    admin_sig=m.get("admin_sig", ""),
                )
                if not verify_membership(admin_pub, record):
                    raise HTTPException(
                        422,
                        "Invite bundle has invalid signature — bundle may be tampered",
                    )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(422, f"Invalid invite bundle: {exc}") from exc

        import json as _json

        membership_str = (
            _json.dumps(membership_json)
            if isinstance(membership_json, dict)
            else str(membership_json or "{}")
        )

        # Save co-op membership (this boat is a member, not admin)
        await storage.save_co_op_membership(
            co_op_id=co_op_id,
            co_op_name=co_op_name,
            co_op_pub=admin_pub,
            membership_json=membership_str,
            role="member",
        )

        # Save ourselves as a peer (so we show in the members list)
        try:
            from helmlog.federation import load_identity

            _, my_card = load_identity()
            await storage.save_co_op_peer(
                co_op_id=co_op_id,
                boat_pub=my_card.pub_key,
                fingerprint=my_card.fingerprint,
                membership_json=membership_str,
                sail_number=my_card.sail_number,
                boat_name=my_card.boat_name,
                tailscale_ip=my_card.tailscale_ip,
            )
        except FileNotFoundError:
            pass

        # Save the admin as a peer so we can query them
        admin_tailscale_ip = body.get("admin_tailscale_ip", "").strip() or None
        admin_boat_name = body.get("admin_boat_name", "").strip()
        admin_sail_number = body.get("admin_sail_number", "").strip()
        await storage.save_co_op_peer(
            co_op_id=co_op_id,
            boat_pub=admin_pub,
            fingerprint=admin_fingerprint,
            membership_json="{}",
            sail_number=admin_sail_number,
            boat_name=admin_boat_name,
            tailscale_ip=admin_tailscale_ip,
        )

        await _audit(
            request,
            "federation.join",
            detail=f"Joined {co_op_name} ({co_op_id})",
            user=_user,
        )
        return JSONResponse(
            {"status": "joined", "co_op_id": co_op_id, "co_op_name": co_op_name},
            status_code=201,
        )

    # ── Session sharing ──────────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/sharing")
    async def api_session_sharing(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        memberships = await storage.list_co_op_memberships()
        sharing = await storage.get_session_sharing(session_id)
        shared_ids = {s["co_op_id"] for s in sharing}
        return JSONResponse(
            {
                "sharing": sharing,
                "co_ops": [
                    {
                        "co_op_id": m["co_op_id"],
                        "co_op_name": m["co_op_name"],
                        "shared": m["co_op_id"] in shared_ids,
                    }
                    for m in memberships
                    if m["status"] == "active"
                ],
            }
        )

    @app.post("/api/sessions/{session_id}/share", status_code=201)
    async def api_session_share(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        body = await request.json()
        co_op_id = body.get("co_op_id", "").strip()
        if not co_op_id:
            raise HTTPException(422, "co_op_id is required")
        membership = await storage.get_co_op_membership(co_op_id)
        if not membership:
            raise HTTPException(404, "Not a member of this co-op")
        embargo_until = body.get("embargo_until") or None
        await storage.share_session(
            session_id,
            co_op_id,
            user_id=_user.get("id"),
            embargo_until=embargo_until,
        )
        await _audit(
            request,
            "federation.session.share",
            detail=f"session {session_id} → {membership['co_op_name']}",
            user=_user,
        )
        return JSONResponse({"status": "shared", "co_op_id": co_op_id}, status_code=201)

    @app.delete("/api/sessions/{session_id}/share/{co_op_id}")
    async def api_session_unshare(
        request: Request,
        session_id: int,
        co_op_id: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        removed = await storage.unshare_session(session_id, co_op_id)
        if not removed:
            raise HTTPException(404, "Session was not shared with this co-op")
        await _audit(
            request,
            "federation.session.unshare",
            detail=f"session {session_id} ✕ {co_op_id}",
            user=_user,
        )
        return JSONResponse({"status": "unshared", "co_op_id": co_op_id})

    # ── Peer data proxies (local UI → remote peers) ────────────────────

    @app.get("/api/federation/co-ops/{co_op_id}/peer-sessions")
    @limiter.limit("10/minute")
    async def api_peer_sessions(
        request: Request,
        co_op_id: str,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Query all online peers in a co-op for their shared sessions."""
        from helmlog.federation import load_identity
        from helmlog.peer_client import fetch_all_peer_sessions

        try:
            private_key, card = load_identity()
        except FileNotFoundError:
            raise HTTPException(409, "Initialize identity first")  # noqa: B904

        peers = await fetch_all_peer_sessions(
            storage,
            co_op_id,
            private_key,
            card.fingerprint,
        )
        await _audit(
            request,
            "coop.proxy.peer_sessions",
            detail=f"co_op={co_op_id} peers={len(peers)}",
            user=_user,
        )
        return JSONResponse({"peers": peers})

    @app.get(
        "/api/federation/co-ops/{co_op_id}/peers/{fingerprint}/sessions/{session_id}/track",
    )
    @limiter.limit("10/minute")
    async def api_peer_session_track(
        request: Request,
        co_op_id: str,
        fingerprint: str,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Proxy track data from a specific remote peer."""
        from helmlog.federation import load_identity
        from helmlog.peer_client import fetch_session_track

        try:
            private_key, card = load_identity()
        except FileNotFoundError:
            raise HTTPException(409, "Initialize identity first")  # noqa: B904

        # Look up peer's Tailscale IP
        peer = await storage.get_co_op_peer(co_op_id, fingerprint)
        if not peer or not peer.get("tailscale_ip"):
            raise HTTPException(404, "Peer not found or no Tailscale IP")

        track = await fetch_session_track(
            peer["tailscale_ip"],
            co_op_id,
            session_id,
            private_key,
            card.fingerprint,
        )
        await _audit(
            request,
            "coop.proxy.peer_track",
            detail=f"co_op={co_op_id} peer={fingerprint} session={session_id} points={len(track)}",
            user=_user,
        )
        return JSONResponse({"track": track, "count": len(track)})

    @app.get(
        "/api/federation/co-ops/{co_op_id}/peers/{fingerprint}/sessions/{session_id}/wind-field",
    )
    @limiter.limit("10/minute")
    async def api_peer_session_wind_field(
        request: Request,
        co_op_id: str,
        fingerprint: str,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        """Proxy wind-field data from a specific remote peer (#246)."""
        from helmlog.federation import load_identity
        from helmlog.peer_client import fetch_session_wind_field

        try:
            private_key, card = load_identity()
        except FileNotFoundError:
            raise HTTPException(409, "Initialize identity first")  # noqa: B904

        peer = await storage.get_co_op_peer(co_op_id, fingerprint)
        if not peer or not peer.get("tailscale_ip"):
            raise HTTPException(404, "Peer not found or no Tailscale IP")

        data = await fetch_session_wind_field(
            peer["tailscale_ip"],
            co_op_id,
            session_id,
            private_key,
            card.fingerprint,
        )
        if data is None:
            raise HTTPException(502, "Failed to fetch wind-field from peer")

        await _audit(
            request,
            "coop.proxy.peer_wind_field",
            detail=f"co_op={co_op_id} peer={fingerprint} session={session_id}",
            user=_user,
        )
        return JSONResponse(data)

    return app
