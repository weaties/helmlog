"""Route handlers for pages."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx
from helmlog.storage import RACE_SLUG_RETENTION_DAYS

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> JSONResponse:
    get_storage(request)
    return JSONResponse({"status": "ok"})


def _render_control_panel(request: Request, active_page: str) -> Response:
    """Render the home.html control panel (#635).

    Shared by ``/control`` and by ``/`` when no races exist yet.
    """
    return templates.TemplateResponse(
        request,
        "home.html",
        tpl_ctx(
            request,
            active_page,
            grafana_port=request.app.state.race_config.grafana_port,
            grafana_uid=request.app.state.race_config.grafana_uid,
            sk_port=request.app.state.race_config.sk_port,
        ),
    )


@router.get("/control", response_class=HTMLResponse, include_in_schema=False)
async def control_page(request: Request) -> Response:
    """Control panel — start/stop races, crew, sails, instruments (#635)."""
    get_storage(request)
    return _render_control_panel(request, "/control")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> Response:
    """Home page — latest-race session view, or control panel fallback (#635).

    * In-progress race (``end_utc IS NULL``) → live session view.
    * Else most recent completed race (max ``end_utc``) → session view.
    * Else (empty DB) → control panel, so a fresh install is usable.
    """
    storage = get_storage(request)
    current = await storage.get_current_race()
    if current is not None:
        return await _render_session_page(request, current, live=True, active_page="/")
    latest = await storage.get_latest_completed_race()
    if latest is not None:
        return await _render_session_page(request, latest, live=False, active_page="/")
    return _render_control_panel(request, "/")


@router.get("/history", response_class=HTMLResponse, include_in_schema=False)
async def history_page(request: Request) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request,
        "history.html",
        tpl_ctx(request, "/history"),
    )


def _canonical_session_url(race_id: int, slug: str | None) -> str:
    """Build the canonical ``/session/{id}/{slug}`` URL (#449).

    The integer id is the stable identity — it stays in the URL even when
    the session is renamed, so bookmarks survive any slug change. The slug
    is purely cosmetic for readability. When a row has no slug yet (pre-v58
    data), the URL collapses to ``/session/{id}``.
    """
    return f"/session/{race_id}/{slug}" if slug else f"/session/{race_id}"


async def _imported_session_redirect(request: Request, race: Any) -> RedirectResponse | None:  # noqa: ANN401
    """Redirect an imported results row to its linked live recording, or None.

    Imported races (Clubspot et al., ``source != 'live'``) are scoreboard rows
    pinned to a date with a placeholder ``start_utc == end_utc`` window and no
    instrument data of their own — so their track, overlays, and maneuvers
    panel all come up empty. When the import is matched to a recording it
    carries ``local_session_id``, and the live session page already folds the
    imported results back in (reverse ``local_session_id`` lookup), making it
    the complete view. Redirect there instead of rendering the data-less
    window. Unmatched imports (``local_session_id IS NULL``) render as-is.
    """
    storage = get_storage(request)
    db = storage._read_conn()  # noqa: SLF001
    cur = await db.execute("SELECT source, local_session_id FROM races WHERE id = ?", (race.id,))
    row = await cur.fetchone()
    if row is None:
        return None
    source = row["source"]
    local_sid = row["local_session_id"]
    if source in (None, "live") or local_sid is None or local_sid == race.id:
        return None
    live_race = await storage.get_race(local_sid)
    if live_race is None:
        return None
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(
        url=_canonical_session_url(live_race.id, live_race.slug) + qs, status_code=302
    )


async def _render_session_page(
    request: Request,
    race: Any,  # helmlog.races.Race  # noqa: ANN401
    *,
    live: bool = False,
    active_page: str = "/history",
) -> Response:
    """Render the session detail template for a resolved race (#449, #635).

    ``live=True`` marks the page as live-updating (used when the home page
    surfaces a race whose ``end_utc`` is still NULL). ``active_page``
    controls the nav highlight — ``/`` when the session is served as the
    home page, ``/history`` when it's the normal session detail URL.
    """
    from datetime import UTC, datetime, timedelta

    storage = get_storage(request)
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    user_role = user.get("role", "viewer") if user else "viewer"
    renamed_banner = None
    if race.renamed_at is not None:
        age = datetime.now(UTC) - race.renamed_at
        if age <= timedelta(days=RACE_SLUG_RETENTION_DAYS):
            db = storage._read_conn()  # noqa: SLF001
            hist_cur = await db.execute(
                "SELECT slug FROM race_slug_history WHERE race_id = ?"
                " ORDER BY retired_at DESC LIMIT 1",
                (race.id,),
            )
            hist_row = await hist_cur.fetchone()
            if hist_row is not None:
                renamed_banner = hist_row["slug"]
    return templates.TemplateResponse(
        request,
        "session.html",
        tpl_ctx(
            request,
            active_page,
            session_id=race.id,
            session_name=race.name,
            session_slug=race.slug,
            session_url=_canonical_session_url(race.id, race.slug),
            renamed_from=renamed_banner,
            clock_flag=race.clock_flag,
            grafana_port=request.app.state.race_config.grafana_port,
            grafana_uid=request.app.state.race_config.grafana_uid,
            user_role=user_role,
            live=live,
        ),
    )


@router.get(
    "/session/{session_id:int}/compare",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def maneuver_compare_page(request: Request, session_id: int) -> Response:
    """Maneuver comparison page — synced multi-video playback (#565)."""
    storage = get_storage(request)
    race = await storage.get_race(session_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Session not found")
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    user_role = user.get("role", "viewer") if user else "viewer"
    return templates.TemplateResponse(
        request,
        "compare.html",
        tpl_ctx(
            request,
            "/history",
            session_id=race.id,
            session_name=race.name,
            session_slug=race.slug,
            user_role=user_role,
        ),
    )


@router.get("/compare", response_class=HTMLResponse, include_in_schema=False)
async def cross_session_compare_page(request: Request) -> Response:
    """Cross-session maneuver compare page (#584).

    Unlike ``/session/{id}/compare`` this has no session context in the URL
    — the ``ids`` query param carries ``<session_id>:<maneuver_id>`` pairs
    and the page fetches everything it needs from
    ``/api/maneuvers/compare``.
    """
    get_storage(request)
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    user_role = user.get("role", "viewer") if user else "viewer"
    return templates.TemplateResponse(
        request,
        "compare.html",
        tpl_ctx(
            request,
            "/maneuvers",
            session_id=None,
            session_name="",
            session_slug="",
            user_role=user_role,
            cross_session=True,
        ),
    )


@router.get("/maneuvers", response_class=HTMLResponse, include_in_schema=False)
async def maneuvers_browser_page(request: Request) -> Response:
    """Cross-session maneuver browser (#584)."""
    get_storage(request)
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    user_role = user.get("role", "viewer") if user else "viewer"
    return templates.TemplateResponse(
        request,
        "maneuvers.html",
        tpl_ctx(request, "/maneuvers", user_role=user_role),
    )


@router.get(
    "/analysis",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def analysis_hub_page(request: Request) -> Response:
    """Coach-facing landing page listing cross-session analysis tools (#741).

    Distinct from /admin/analysis — that surface manages the AnalysisPlugin
    framework (install, default, co-op catalog). This page is the entry
    point for actually *running* coach analyses.
    """
    get_storage(request)
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    user_role = user.get("role", "viewer") if user else "viewer"
    return templates.TemplateResponse(
        request,
        "analysis_hub.html",
        tpl_ctx(request, "/analysis", user_role=user_role),
    )


@router.get(
    "/analysis/maneuver-compare",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def maneuver_best_vs_median_page(request: Request) -> Response:
    """Best vs median maneuver picker for coaching review (#741).

    Filter form → /api/analysis/maneuver-compare → links to /compare and
    /maneuvers/overlay so the existing video-grid and track-overlay pages
    do the heavy rendering.
    """
    get_storage(request)
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    user_role = user.get("role", "viewer") if user else "viewer"
    return templates.TemplateResponse(
        request,
        "maneuver_compare.html",
        tpl_ctx(request, "/analysis", user_role=user_role),
    )


@router.get("/maneuvers/overlay", response_class=HTMLResponse, include_in_schema=False)
async def maneuvers_overlay_page(request: Request) -> Response:
    """Time-aligned multi-maneuver overlay chart (#619).

    One page serves both entry points. The ``ids`` query parameter
    carries the ``<session_id>:<maneuver_id>`` pairs regardless of
    whether the user arrived from the session detail page or the
    cross-session maneuvers browser. A single session_id / name in
    the URL (via a ``session`` query hint) gets rendered as a
    breadcrumb so the user can return to the session page.
    """
    get_storage(request)
    session_id_raw = request.query_params.get("session", "")
    session_id: int | None = None
    session_name = ""
    session_slug = ""
    try:
        session_id = int(session_id_raw) if session_id_raw else None
    except ValueError:
        session_id = None
    if session_id is not None:
        storage = get_storage(request)
        race = await storage.get_race(session_id)
        if race is not None:
            session_name = race.name
            session_slug = race.slug or ""
    return templates.TemplateResponse(
        request,
        "overlay.html",
        tpl_ctx(
            request,
            "/maneuvers",
            session_id=session_id,
            session_name=session_name,
            session_slug=session_slug,
        ),
    )


@router.get(
    "/session/{session_id:int}/{slug}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def session_detail_page_canonical(request: Request, session_id: int, slug: str) -> Response:
    """Canonical session URL carrying both the stable id and the slug (#449).

    * If the id exists and the slug matches the current slug → render.
    * If the id exists but the slug is stale (renamed) → 302 to the new
      canonical URL so old bookmarks keep working indefinitely. 302 (not
      301) so browsers don't permanently cache the redirect — a re-rename
      back to the original slug should resolve correctly without a cache
      bust.
    * If the id doesn't exist → 404.
    """
    storage = get_storage(request)
    race = await storage.get_race(session_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if race.slug and slug != race.slug:
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(
            url=_canonical_session_url(race.id, race.slug) + qs, status_code=302
        )
    imported_redirect = await _imported_session_redirect(request, race)
    if imported_redirect is not None:
        return imported_redirect
    return await _render_session_page(request, race)


async def _render_debrief_page(request: Request, debrief: dict[str, Any]) -> Response:
    """Render the session template for a debrief (audio_sessions row) (#449)."""
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    user_role = user.get("role", "viewer") if user else "viewer"
    return templates.TemplateResponse(
        request,
        "session.html",
        tpl_ctx(
            request,
            "/history",
            session_id=debrief["id"],
            session_name=debrief["name"],
            session_slug="",
            session_url=f"/session/{debrief['id']}",
            renamed_from=None,
            grafana_port=request.app.state.race_config.grafana_port,
            grafana_uid=request.app.state.race_config.grafana_uid,
            user_role=user_role,
            live=False,
        ),
    )


@router.get(
    "/session/{session_ref}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def session_detail_page(request: Request, session_ref: str) -> Response:
    """Single-segment session URL — 301s to the canonical ``/session/{id}/{slug}``.

    Accepts an integer id (race or debrief) or a slug:

    * ``/session/{race_id}`` → 301 to ``/session/{id}/{slug}``. A race row
      with no slug (pre-v58 data whose backfill didn't complete) has one
      lazily allocated on first access.
    * ``/session/{audio_id}`` where that id matches a debrief row in
      ``audio_sessions`` → render inline (debriefs have no slug; the id is
      the stable key from the history list).
    * ``/session/{slug}`` (current) → 301 to the canonical URL.
    * ``/session/{slug}`` (retired, within retention window) → 301 to the
      current canonical URL.
    * Unknown id / slug → 404.
    """
    from datetime import UTC, datetime, timedelta

    storage = get_storage(request)

    # Preserve query string across the canonical-slug redirect so deep links
    # like /session/104?moment=38&comment=20 don't lose their params on the
    # 301 hop to /session/104/{slug}.
    qs = f"?{request.url.query}" if request.url.query else ""

    if session_ref.isdigit():
        numeric_id = int(session_ref)
        race = await storage.get_race(numeric_id)
        if race is not None:
            slug = race.slug or await storage.ensure_race_slug(race.id) or ""
            if slug:
                return RedirectResponse(
                    url=_canonical_session_url(race.id, slug) + qs, status_code=302
                )
            # Last-resort fallback — render inline rather than redirect-loop.
            imported_redirect = await _imported_session_redirect(request, race)
            if imported_redirect is not None:
                return imported_redirect
            return await _render_session_page(request, race)
        # Not a race — try the debrief (audio_sessions) id space so history
        # links to debrief sessions keep resolving.
        debrief = await storage.get_debrief_session(numeric_id)
        if debrief is not None:
            return await _render_debrief_page(request, debrief)
        raise HTTPException(status_code=404, detail="Session not found")

    race = await storage.get_race_by_slug(session_ref)
    if race is not None:
        return RedirectResponse(
            url=_canonical_session_url(race.id, race.slug) + qs, status_code=302
        )

    retired = await storage.lookup_retired_slug(session_ref)
    if retired is None:
        raise HTTPException(status_code=404, detail="Session not found")
    race_id, retired_at = retired
    if datetime.now(UTC) - retired_at > timedelta(days=RACE_SLUG_RETENTION_DAYS):
        raise HTTPException(status_code=404, detail="Session not found")
    current = await storage.get_race(race_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return RedirectResponse(
        url=_canonical_session_url(current.id, current.slug) + qs, status_code=302
    )


@router.get("/sails", response_class=HTMLResponse, include_in_schema=False)
async def sails_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(request, "sails.html", tpl_ctx(request, "/sails"))


@router.get("/profile", response_class=HTMLResponse, include_in_schema=False)
async def profile_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    import time

    from helmlog.themes import PRESET_ORDER, PRESETS

    user_id = _user.get("id") or 0
    role = _user.get("role", "viewer")
    role_colors = {"admin": "#f59e0b", "crew": "#34d399", "viewer": "#60a5fa"}
    consents = await storage.get_crew_consents(user_id) if user_id else []
    bio_consent = any(c["consent_type"] == "biometric" and c["granted"] for c in consents)
    pw_cred = await storage.get_credential(user_id, "password") if user_id else None
    has_password = pw_cred is not None
    preset_list = [{"id": pid, "name": PRESETS[pid].name} for pid in PRESET_ORDER if pid in PRESETS]
    custom_list = await storage.list_color_schemes()
    boat_default = await storage.get_setting("color_scheme_default") or ""
    current_scheme = _user.get("color_scheme") or ""
    return templates.TemplateResponse(
        request,
        "profile.html",
        tpl_ctx(
            request,
            "/profile",
            name=_user.get("name") or _user.get("email") or "Unknown",
            email=_user.get("email") or "",
            role=role,
            role_color=role_colors.get(role, "#8892a4"),
            avatar_url=f"/avatars/{user_id}.jpg?v={int(time.time())}" if user_id else "",
            weight_lbs=_user.get("weight_lbs"),
            bio_consent=bio_consent,
            user_id=user_id,
            preset_schemes=preset_list,
            custom_schemes=custom_list,
            boat_default=boat_default,
            current_scheme=current_scheme,
            has_password=has_password,
        ),
    )


@router.post("/profile/avatar", status_code=200, include_in_schema=False)
async def upload_avatar(
    request: Request,
    file: UploadFile,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
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
    await audit(request, "avatar.upload", user=_user)
    return JSONResponse({"avatar_path": rel_path})


@router.get("/avatars/{user_id}.jpg", include_in_schema=False)
async def serve_avatar(request: Request, user_id: int) -> Response:
    storage = get_storage(request)
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


@router.get("/attention", response_class=HTMLResponse)
async def attention_page(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> HTMLResponse:
    """Notification dashboard page."""
    get_storage(request)
    return templates.TemplateResponse(
        "attention.html",
        tpl_ctx(request, "/attention"),
    )
