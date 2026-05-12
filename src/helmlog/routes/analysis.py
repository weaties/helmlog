"""Route handlers for analysis."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()

# Allowed metric names (smaller-is-better) for the maneuver-compare endpoint.
# Each maps to a key on the enriched-maneuver dict produced by
# ``analysis.maneuvers.enrich_maneuvers_for_ids``.
_COMPARE_METRICS: set[str] = {
    "distance_loss_m",
    "time_to_recover_s",
    "time_to_head_to_wind_s",
    "loss_kts",
}
# Allowed maneuver-type values for the compare endpoint. Roundings are
# split into weather / leeward via the ``details.rounding_kind`` discriminator.
_COMPARE_TYPES: set[str] = {
    "tack",
    "gybe",
    "weather_rounding",
    "leeward_rounding",
}


@router.get("/api/analysis/models")
async def api_analysis_models(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List available analysis plugins."""
    get_storage(request)
    from helmlog.analysis.discovery import discover_plugins  # noqa: PLC0415

    plugins = discover_plugins()
    result = []
    for _name, plugin in plugins.items():
        meta = plugin.meta()
        result.append(
            {
                "name": meta.name,
                "display_name": meta.display_name,
                "description": meta.description,
                "version": meta.version,
            }
        )
    return JSONResponse(result)


@router.post("/api/analysis/run/{session_id}")
async def api_analysis_run(
    request: Request,
    session_id: int,
    model: str | None = None,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Run an analysis plugin on a session."""
    storage = get_storage(request)
    from helmlog.analysis.cache import AnalysisCache, _compute_data_hash  # noqa: PLC0415
    from helmlog.analysis.discovery import discover_plugins, load_session_data  # noqa: PLC0415
    from helmlog.analysis.preferences import resolve_preference  # noqa: PLC0415
    from helmlog.analysis.protocol import AnalysisContext  # noqa: PLC0415

    # Determine which plugin to use
    plugin_name = model
    if not plugin_name:
        plugin_name = await resolve_preference(storage, user["id"])
    if not plugin_name:
        # Default to first available plugin
        plugins = discover_plugins()
        if not plugins:
            raise HTTPException(404, "No analysis plugins available")
        plugin_name = next(iter(plugins))

    plugins = discover_plugins()
    plugin = plugins.get(plugin_name)
    if plugin is None:
        raise HTTPException(404, f"Plugin {plugin_name!r} not found")

    # Mark stale any cache rows where plugin version has changed (#285)
    await storage.mark_plugin_cache_stale(plugin_name, plugin.meta().version)

    session_data = await load_session_data(storage, session_id)
    if session_data is None:
        raise HTTPException(404, "Session not found or not completed")

    # Check co-op data status
    db = storage._conn()
    race_cur = await db.execute(
        "SELECT source, peer_fingerprint FROM races WHERE id = ?", (session_id,)
    )
    race_row = await race_cur.fetchone()
    is_co_op = bool(race_row and race_row["peer_fingerprint"])

    ctx = AnalysisContext(
        user_id=user["id"],
        is_co_op_data=is_co_op,
    )

    # Check cache
    cache = AnalysisCache(storage)
    data_hash = _compute_data_hash(
        {
            "speeds": len(session_data.speeds),
            "winds": len(session_data.winds),
            "session_id": session_id,
        }
    )
    cached = await cache.get(session_id, plugin_name, data_hash=data_hash)
    if cached is not None:
        if is_co_op:
            cached.pop("raw", None)
        return JSONResponse(cached)

    result = await plugin.analyze(session_data, ctx)
    result_dict = result.to_dict(include_raw=True)

    await cache.put(session_id, plugin_name, result.plugin_version, data_hash, result_dict)

    if is_co_op:
        await audit(
            request,
            "analysis.run_coop",
            detail=f"session={session_id} plugin={plugin_name}",
            user=user,
        )
        result_dict.pop("raw", None)
    else:
        await audit(
            request,
            "analysis.run",
            detail=f"session={session_id} plugin={plugin_name}",
            user=user,
        )

    return JSONResponse(result_dict)


@router.get("/api/analysis/results/{session_id}")
async def api_analysis_results(
    request: Request,
    session_id: int,
    model: str | None = None,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return cached analysis result for a session."""
    storage = get_storage(request)
    from helmlog.analysis.discovery import discover_plugins  # noqa: PLC0415
    from helmlog.analysis.preferences import resolve_preference  # noqa: PLC0415

    plugin_name = model
    if not plugin_name:
        plugin_name = await resolve_preference(storage, user["id"])
    if not plugin_name:
        plugins = discover_plugins()
        if not plugins:
            raise HTTPException(404, "No analysis plugins available")
        plugin_name = next(iter(plugins))

    cached = await storage.get_analysis_cache(session_id, plugin_name)
    if cached is None:
        raise HTTPException(404, "No cached result")

    import json as _json  # noqa: PLC0415

    result = _json.loads(cached["result_json"])

    # Strip raw from co-op data
    db = storage._conn()
    race_cur = await db.execute("SELECT peer_fingerprint FROM races WHERE id = ?", (session_id,))
    race_row = await race_cur.fetchone()
    if race_row and race_row["peer_fingerprint"]:
        result.pop("raw", None)

    # Include stale_reason so UI can display "model updated — re-run?" (#285)
    stale_reason = cached.get("stale_reason")
    if stale_reason is not None:
        result["stale_reason"] = stale_reason

    return JSONResponse(result)


@router.get("/api/analysis/preferences")
async def api_analysis_preferences(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the resolved analysis preference for the current user."""
    storage = get_storage(request)
    from helmlog.analysis.preferences import resolve_preference  # noqa: PLC0415

    model = await resolve_preference(storage, user["id"])
    return JSONResponse({"model_name": model})


@router.put("/api/analysis/preferences")
async def api_set_analysis_preference(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Set analysis preference at a scope."""
    storage = get_storage(request)
    from helmlog.analysis.preferences import set_preference  # noqa: PLC0415

    body = await request.json()
    scope: str = body.get("scope", "user")
    scope_id: str | None = body.get("scope_id")
    model_name: str = body.get("model_name", "")
    if not model_name:
        raise HTTPException(422, "model_name is required")

    # Only admin can set platform/co_op/boat scope
    if scope != "user" and user.get("role") != "admin":
        raise HTTPException(403, "Only admin can set non-user preferences")

    if scope == "user":
        scope_id = str(user["id"])

    await set_preference(storage, scope, scope_id, model_name)
    await audit(
        request, "analysis.preference", detail=f"scope={scope} model={model_name}", user=user
    )
    return JSONResponse({"ok": True})


# /api/analysis/catalog — Phase 2 co-op promotion (#285)
# ------------------------------------------------------------------


@router.get("/api/analysis/catalog")
async def api_analysis_catalog_list(
    request: Request,
    co_op_id: str,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List analysis catalog entries for a co-op."""
    storage = get_storage(request)
    entries = await storage.list_catalog_entries(co_op_id)
    return JSONResponse([dict(e) for e in entries])


@router.post("/api/analysis/catalog/propose")
async def api_analysis_catalog_propose(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Propose a plugin for co-op promotion (boat owner action)."""
    from helmlog.analysis.catalog import CatalogError, propose_to_co_op  # noqa: PLC0415

    storage = get_storage(request)
    body = await request.json()
    plugin_name: str = body.get("plugin_name", "").strip()
    co_op_id: str = body.get("co_op_id", "").strip()

    if not plugin_name or not co_op_id:
        raise HTTPException(422, "plugin_name and co_op_id are required")

    membership = await storage.get_co_op_membership(co_op_id)
    if not membership or membership.get("status") == "revoked":
        raise HTTPException(403, "This boat is not an active member of the co-op")

    from helmlog.analysis.discovery import get_plugin  # noqa: PLC0415

    plugin = get_plugin(plugin_name)
    if plugin is None:
        raise HTTPException(404, f"Plugin {plugin_name!r} not found")

    identity = await storage.get_boat_identity()
    proposing_boat = identity["fingerprint"] if identity else "unknown"

    meta = plugin.meta()
    try:
        entry = await propose_to_co_op(
            storage,
            plugin_name,
            co_op_id,
            proposing_boat=proposing_boat,
            version=meta.version,
            author=meta.author,
            changelog=meta.changelog,
        )
    except CatalogError as exc:
        raise HTTPException(409, str(exc)) from exc  # noqa: B904

    await audit(
        request,
        "analysis.catalog.propose",
        detail=f"plugin={plugin_name} co_op={co_op_id}",
        user=user,
    )
    return JSONResponse(entry.to_dict(), status_code=201)


@router.post("/api/analysis/catalog/{plugin_name}/approve")
async def api_analysis_catalog_approve(
    request: Request,
    plugin_name: str,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Approve a proposed plugin (co-op moderator action)."""
    from helmlog.analysis.catalog import CatalogError, approve  # noqa: PLC0415

    storage = get_storage(request)
    body = await request.json()
    co_op_id: str = body.get("co_op_id", "").strip()
    if not co_op_id:
        raise HTTPException(422, "co_op_id is required")

    membership = await storage.get_co_op_membership(co_op_id)
    if not membership or membership.get("role") != "admin":
        raise HTTPException(403, "Only co-op moderators can approve proposals")

    from helmlog.analysis.discovery import get_plugin  # noqa: PLC0415

    plugin = get_plugin(plugin_name)
    if plugin is None:
        raise HTTPException(404, f"Plugin {plugin_name!r} not found")

    result_sample: dict[str, Any] = body.get("result_sample", {})

    try:
        entry = await approve(storage, plugin_name, co_op_id, result_sample=result_sample)
    except CatalogError as exc:
        raise HTTPException(422, str(exc)) from exc  # noqa: B904

    await audit(
        request,
        "analysis.catalog.approve",
        detail=f"plugin={plugin_name} co_op={co_op_id}",
        user=user,
    )
    return JSONResponse(entry.to_dict())


@router.post("/api/analysis/catalog/{plugin_name}/reject")
async def api_analysis_catalog_reject(
    request: Request,
    plugin_name: str,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Reject a proposed plugin (co-op moderator action)."""
    from helmlog.analysis.catalog import CatalogError, reject  # noqa: PLC0415

    storage = get_storage(request)
    body = await request.json()
    co_op_id: str = body.get("co_op_id", "").strip()
    reason: str = body.get("reason", "").strip()
    if not co_op_id or not reason:
        raise HTTPException(422, "co_op_id and reason are required")

    membership = await storage.get_co_op_membership(co_op_id)
    if not membership or membership.get("role") != "admin":
        raise HTTPException(403, "Only co-op moderators can reject proposals")

    try:
        entry = await reject(storage, plugin_name, co_op_id, reason=reason)
    except CatalogError as exc:
        raise HTTPException(422, str(exc)) from exc  # noqa: B904

    await audit(
        request,
        "analysis.catalog.reject",
        detail=f"plugin={plugin_name} co_op={co_op_id} reason={reason}",
        user=user,
    )
    return JSONResponse(entry.to_dict())


@router.post("/api/analysis/catalog/{plugin_name}/deprecate")
async def api_analysis_catalog_deprecate(
    request: Request,
    plugin_name: str,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Deprecate a co-op plugin (moderator or plugin author)."""
    from helmlog.analysis.catalog import CatalogError, deprecate  # noqa: PLC0415

    storage = get_storage(request)
    body = await request.json()
    co_op_id: str = body.get("co_op_id", "").strip()
    if not co_op_id:
        raise HTTPException(422, "co_op_id is required")

    membership = await storage.get_co_op_membership(co_op_id)
    if not membership:
        raise HTTPException(403, "This boat is not a member of the co-op")

    entry_row = await storage.get_catalog_entry(plugin_name, co_op_id)
    if entry_row is None:
        raise HTTPException(404, f"Plugin {plugin_name!r} not found in co-op catalog")

    identity = await storage.get_boat_identity()
    this_fingerprint = identity["fingerprint"] if identity else None
    is_moderator = membership.get("role") == "admin"
    is_author = this_fingerprint and entry_row.get("proposing_boat") == this_fingerprint

    if not is_moderator and not is_author:
        raise HTTPException(403, "Only the co-op moderator or plugin author can deprecate")

    try:
        entry = await deprecate(storage, plugin_name, co_op_id)
    except CatalogError as exc:
        raise HTTPException(422, str(exc)) from exc  # noqa: B904

    await audit(
        request,
        "analysis.catalog.deprecate",
        detail=f"plugin={plugin_name} co_op={co_op_id}",
        user=user,
    )
    return JSONResponse(entry.to_dict())


@router.post("/api/analysis/catalog/{plugin_name}/set-default")
async def api_analysis_catalog_set_default(
    request: Request,
    plugin_name: str,
    user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Set a co_op_active plugin as the co-op default (co-op moderator action)."""
    from helmlog.analysis.catalog import CatalogError, set_co_op_default  # noqa: PLC0415

    storage = get_storage(request)
    body = await request.json()
    co_op_id: str = body.get("co_op_id", "").strip()
    if not co_op_id:
        raise HTTPException(422, "co_op_id is required")

    membership = await storage.get_co_op_membership(co_op_id)
    if not membership or membership.get("role") != "admin":
        raise HTTPException(403, "Only co-op moderators can set the default model")

    try:
        entry = await set_co_op_default(storage, plugin_name, co_op_id)
    except CatalogError as exc:
        raise HTTPException(422, str(exc)) from exc  # noqa: B904

    await audit(
        request,
        "analysis.catalog.set_default",
        detail=f"plugin={plugin_name} co_op={co_op_id}",
        user=user,
    )
    return JSONResponse(entry.to_dict())


@router.post("/api/analysis/ab-compare/{session_id}")
async def api_analysis_ab_compare(
    request: Request,
    session_id: int,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Run two or more analysis models on the same session and return side-by-side results.

    Request body: {"models": ["polar_baseline", "sail_vmg"]}
    """
    from helmlog.analysis.cache import AnalysisCache, _compute_data_hash  # noqa: PLC0415
    from helmlog.analysis.discovery import discover_plugins, load_session_data  # noqa: PLC0415
    from helmlog.analysis.protocol import AnalysisContext  # noqa: PLC0415

    storage = get_storage(request)
    body = await request.json()
    model_names: list[str] = body.get("models", [])
    if len(model_names) < 2:
        raise HTTPException(422, "At least two model names are required for A/B comparison")
    if len(model_names) > 5:
        raise HTTPException(422, "At most 5 models can be compared at once")

    session_data = await load_session_data(storage, session_id)
    if session_data is None:
        raise HTTPException(404, "Session not found or not completed")

    db = storage._conn()
    race_cur = await db.execute("SELECT peer_fingerprint FROM races WHERE id = ?", (session_id,))
    race_row = await race_cur.fetchone()
    is_co_op = bool(race_row and race_row["peer_fingerprint"])

    ctx = AnalysisContext(user_id=user["id"], is_co_op_data=is_co_op)
    plugins = discover_plugins()
    cache = AnalysisCache(storage)
    data_hash = _compute_data_hash(
        {
            "speeds": len(session_data.speeds),
            "winds": len(session_data.winds),
            "session_id": session_id,
        }
    )

    panels: list[dict[str, Any]] = []
    for name in model_names:
        plugin = plugins.get(name)
        if plugin is None:
            panels.append({"plugin_name": name, "error": "Plugin not found"})
            continue

        meta = plugin.meta()

        stale_count = await storage.mark_plugin_cache_stale(name, meta.version)
        if stale_count:
            from loguru import logger  # noqa: PLC0415

            logger.debug(
                "Marked {} stale cache rows for {} after version change", stale_count, name
            )

        cached_row = await storage.get_analysis_cache(session_id, name)
        panel: dict[str, Any] = {}

        if (
            cached_row is not None
            and cached_row["data_hash"] == data_hash
            and cached_row.get("stale_reason") is None
        ):
            import json as _json  # noqa: PLC0415

            result_dict = _json.loads(cached_row["result_json"])
        else:
            result = await plugin.analyze(session_data, ctx)
            result_dict = result.to_dict(include_raw=True)
            await cache.put(session_id, name, meta.version, data_hash, result_dict)
            cached_row = await storage.get_analysis_cache(session_id, name)

        if is_co_op:
            result_dict.pop("raw", None)

        stale_reason = cached_row.get("stale_reason") if cached_row else None
        panel = {
            **result_dict,
            "label": f"{meta.display_name} v{meta.version}",
            "stale_reason": stale_reason,
        }
        panels.append(panel)

    await audit(
        request,
        "analysis.ab_compare",
        detail=f"session={session_id} models={model_names}",
        user=user,
    )
    return JSONResponse({"session_id": session_id, "panels": panels})


@router.get("/api/analysis/maneuver-compare")
async def api_maneuver_compare(
    request: Request,
    maneuver_type: str = Query(..., alias="type"),
    metric: str = Query("distance_loss_m"),
    tws_min: float | None = Query(None, ge=0),
    tws_max: float | None = Query(None, ge=0),
    direction: str | None = Query(None),
    post_start: bool = Query(False),
    n: int = Query(3, ge=1, le=10),
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Coaching tool: 3 best + 3 median maneuvers across all sessions (#741).

    Filters all stored maneuvers by ``type``, entry-TWS range, tack/gybe
    ``direction`` (PS|SP), and an optional pre-start cut (``post_start=1``
    drops maneuvers that happened before the session's race gun, computed
    the same way as ``GET /api/maneuvers``). Ranks the pool by ``metric``
    (smaller is better) and returns the best ``n`` plus the ``n`` closest
    to the median for side-by-side video/track review.
    """
    if maneuver_type not in _COMPARE_TYPES:
        raise HTTPException(422, f"type must be one of {sorted(_COMPARE_TYPES)}")
    if metric not in _COMPARE_METRICS:
        raise HTTPException(422, f"metric must be one of {sorted(_COMPARE_METRICS)}")
    if tws_min is not None and tws_max is not None and tws_min > tws_max:
        raise HTTPException(422, "tws_min must be <= tws_max")
    if direction is not None and direction not in ("PS", "SP"):
        raise HTTPException(422, "direction must be PS or SP")

    storage = get_storage(request)

    from helmlog.analysis.compare import (  # noqa: PLC0415
        assemble_compare_set,
        filter_pool,
        list_maneuver_pairs,
        load_gun_times,
    )
    from helmlog.analysis.maneuvers import enrich_maneuvers_for_ids  # noqa: PLC0415

    pairs = await list_maneuver_pairs(storage, maneuver_type=maneuver_type)
    enriched, video_sync = await enrich_maneuvers_for_ids(storage, pairs)

    gun_by_session: dict[int, str] | None = None
    if post_start:
        session_ids = sorted({sid for sid, _ in pairs})
        gun_by_session = await load_gun_times(storage, session_ids)

    pool = filter_pool(
        enriched,
        maneuver_type=maneuver_type,
        tws_min=tws_min,
        tws_max=tws_max,
        direction=direction,
        gun_by_session=gun_by_session,
    )
    result = assemble_compare_set(pool, metric=metric, n=n)

    await audit(
        request,
        "analysis.maneuver_compare",
        detail=(
            f"type={maneuver_type} metric={metric}"
            f" tws=[{tws_min},{tws_max}] dir={direction}"
            f" post_start={int(post_start)} pool={result['pool_size']}"
        ),
        user=user,
    )
    return JSONResponse(
        {
            "filters": {
                "type": maneuver_type,
                "metric": metric,
                "tws_min": tws_min,
                "tws_max": tws_max,
                "direction": direction,
                "post_start": post_start,
                "n": n,
            },
            **result,
            "video_sync_by_session": {str(sid): vs for sid, vs in video_sync.items()},
        }
    )
