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

import os
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from loguru import logger

if TYPE_CHECKING:
    from fastapi.responses import Response

    from helmlog.audio import AudioConfig, AudioRecorder, AudioRecorderGroup
    from helmlog.storage import Storage

# Re-export _get_git_info for backward compatibility (used by tests)
from helmlog.routes._helpers import (
    STARTUP_SHA,
    AppSessionState,
    _get_git_info,  # noqa: F401
)

_STATIC_DIR = __import__("pathlib").Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    storage: Storage,
    recorder: AudioRecorder | AudioRecorderGroup | None = None,
    audio_config: AudioConfig | None = None,
) -> FastAPI:
    """Create and return the FastAPI application bound to the given Storage.

    If *recorder* and *audio_config* are provided, recording starts when a race
    starts and stops when the race ends.  Cameras are managed in the database
    and loaded dynamically for each operation.
    """
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from helmlog.routes._helpers import limiter

    limiter.reset()
    app = FastAPI(title="HelmLog", docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # SessionMiddleware is required by Authlib's OAuth integration (request.session)
    from starlette.middleware.sessions import SessionMiddleware

    session_secret = os.getenv("SESSION_SECRET", "")
    if not session_secret:
        from helmlog.auth import generate_token

        session_secret = generate_token()
        logger.warning(
            "SESSION_SECRET not set — generated an ephemeral key (OAuth state will not survive restarts)"
        )
    app.add_middleware(SessionMiddleware, secret_key=session_secret)

    # Initialize OAuth providers
    from helmlog.oauth import init_oauth

    init_oauth()

    # -- Bind shared state --
    app.state.storage = storage
    app.state.recorder = recorder
    app.state.audio_config = audio_config
    app.state.session_state = AppSessionState()
    app.state.startup_sha = STARTUP_SHA
    app.state.ws_clients = set()  # WebSocket client connections

    # -- Web response cache (#594) --
    # Reuse a pre-bound cache if main.py already set one up (so background
    # tasks like ExternalFetcher and the web routes share a single cache).
    # Otherwise create a fresh one here — the test harness path.
    from helmlog.cache import WebCache

    existing = getattr(storage, "_race_cache", None)
    if isinstance(existing, WebCache):
        web_cache = existing
    else:
        web_cache = WebCache(storage)
        storage.bind_race_cache(web_cache)
    app.state.web_cache = web_cache

    from helmlog.races import RaceConfig

    app.state.race_config = RaceConfig()

    # -- Static files --
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # -- Peer API (federation endpoints for remote boats) --
    from helmlog.peer_api import _limiter as peer_limiter
    from helmlog.peer_api import router as peer_router

    app.state.peer_limiter = peer_limiter
    app.include_router(peer_router)

    # -- Theme middleware: injects resolved CSS variables into request.state --
    # NOTE: inject_theme_css is registered BEFORE auth_middleware.  In Starlette's
    # LIFO middleware stack, auth_middleware runs first (outermost), so
    # request.state.user is already populated when inject_theme_css executes.
    from helmlog.themes import resolve_theme, theme_to_css

    @app.middleware("http")
    async def inject_theme_css(request: Request, call_next: Any) -> Any:  # noqa: ANN401
        """Resolve the active color scheme and store the CSS in request.state."""
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            user: dict[str, Any] | None = getattr(request.state, "user", None)
            user_scheme: str | None = user.get("color_scheme") if user else None
            boat_default = await storage.get_setting("color_scheme_default")
            custom_schemes = await storage.list_color_schemes()
            theme = resolve_theme(user_scheme, boat_default, custom_schemes)
            request.state.theme_css = theme_to_css(theme)
        else:
            request.state.theme_css = ""
        return await call_next(request)

    from helmlog.bandwidth import bandwidth_middleware

    @app.middleware("http")
    async def track_bandwidth(request: Request, call_next: Any) -> Any:  # noqa: ANN401
        """Track per-request bandwidth attribution (#403)."""
        return await bandwidth_middleware(request, call_next)

    from helmlog.auth import _is_auth_disabled, _resolve_user, check_device_scope, resolve_device

    _PUBLIC_PATHS = {
        "/login",
        "/logout",
        "/healthz",
        "/avatars",
        "/auth/login",
        "/auth/accept-invite",
        "/auth/register",
        "/auth/oauth",
        "/auth/reset-password",
        "/auth/forgot-password",
        "/static",
    }

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
        if path in _PUBLIC_PATHS or path.startswith(("/static/", "/co-op/", "/auth/")):
            return await call_next(request)  # type: ignore[no-any-return]

        # Try device bearer token auth first (#423)
        device_user = await resolve_device(request)
        if device_user is not None:
            # Enforce scope restriction
            if not check_device_scope(device_user.get("device_scope"), request.method, path):
                from starlette.responses import JSONResponse as _JR

                return _JR({"detail": "Outside device scope"}, status_code=403)
            request.state.user = device_user
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
                from urllib.parse import quote

                from starlette.responses import RedirectResponse as _RR

                query = request.url.query
                next_target = f"{path}?{query}" if query else path
                return _RR(url=f"/login?next={quote(next_target, safe='')}", status_code=307)
            from starlette.responses import JSONResponse as _JR

            return _JR({"detail": "Not authenticated"}, status_code=401)
        request.state.user = user
        return await call_next(request)  # type: ignore[no-any-return]

    # -- Include domain routers --
    from helmlog.routes import (
        admin,
        analysis,
        aruco,
        audio,
        audio_channels,
        auth,
        boat_settings,
        cameras,
        comments,
        controls,
        crew,
        deployment,
        device_cameras,
        devices,
        federation,
        instruments,
        me,
        moments,
        network,
        notifications,
        pages,
        polar,
        race_start,
        race_start_sim,
        races,
        results,
        sails,
        sessions,
        settings,
        tags,
        videos,
        visualizations,
        ws,
    )

    for module in (
        pages,
        me,
        auth,
        admin,
        devices,
        instruments,
        polar,
        races,
        sessions,
        crew,
        sails,
        audio,
        audio_channels,
        cameras,
        device_cameras,
        aruco,
        controls,
        race_start,
        network,
        videos,
        boat_settings,
        moments,
        comments,
        tags,
        settings,
        deployment,
        federation,
        results,
        analysis,
        notifications,
        visualizations,
        ws,
    ):
        app.include_router(module.router)

    # Race-start simulator (#690). Routes are always mounted but gated on
    # the developer flag (users.is_developer = 1). RACE_START_SIMULATOR=false
    # is a hard kill switch that turns the routes into 404s for defense in
    # depth on production boats.
    app.include_router(race_start_sim.router)

    # -- Register results providers (#459) --
    from helmlog.results.base import register_provider
    from helmlog.results.clubspot import ClubspotProvider
    from helmlog.results.styc import StycProvider

    register_provider(ClubspotProvider())
    register_provider(StycProvider())

    # -- Wire WebSocket broadcast to storage live updates --
    import asyncio as _asyncio

    from helmlog.routes.ws import broadcast

    def _on_live_update(data: dict) -> None:  # type: ignore[type-arg]
        """Sync callback from Storage.update_live() → schedule async broadcast."""
        try:
            loop = _asyncio.get_running_loop()
            loop.create_task(broadcast(app.state.ws_clients, {"type": "instruments", "data": data}))
        except RuntimeError:
            pass  # no event loop running (e.g., during tests)

    storage.set_live_callback(_on_live_update)

    def _on_position_update(payload: dict) -> None:  # type: ignore[type-arg]
        """Sync callback from Storage.update_live() for PositionRecord → broadcast.

        Throttled to 1 Hz inside Storage so the wire stays predictable
        regardless of GPS fix rate. Frontend appends each fix to its track
        polyline so the live page extends in real time without polling.
        """
        try:
            loop = _asyncio.get_running_loop()
            loop.create_task(broadcast(app.state.ws_clients, {"type": "position", "data": payload}))
        except RuntimeError:
            pass

    storage.set_position_callback(_on_position_update)

    return app
