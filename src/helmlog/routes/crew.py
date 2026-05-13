"""Route handlers for crew."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import (
    BoatCreate,
    BoatUpdate,
    BulkCrewBackfill,
    CrewEntry,
    PositionEntry,
    RaceResultEntry,
    audit,
    get_storage,
)

router = APIRouter()


@router.post("/api/races/{race_id}/crew", status_code=204)
async def api_set_crew(
    request: Request,
    race_id: int,
    body: list[CrewEntry],
    gear_preset: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    """Write a verified per-race crew lineup (#761).

    Calling this endpoint is treated as confirmation: ``races.crew_assumed``
    flips to 0 and ``races.gear_preset`` is recorded. Pass ``?gear_preset=
    dry|wet|foulies`` to fill in missing per-entry gear_weight values from
    the preset.
    """
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Race not found")

    # Validate position_ids exist
    positions = await storage.get_crew_positions()
    valid_ids = {p["id"] for p in positions}
    invalid = [e.position_id for e in body if e.position_id not in valid_ids]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown position_id(s): {invalid}",
        )

    crew = [
        {
            "position_id": e.position_id,
            "user_id": e.user_id,
            "attributed": e.attributed,
            "body_weight": e.body_weight,
            "gear_weight": e.gear_weight,
        }
        for e in body
    ]
    try:
        await storage.confirm_race_crew(race_id, crew, gear_preset=gear_preset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    detail = f"race={race_id} verified n={len(crew)}"
    if gear_preset is not None:
        detail += f" gear={gear_preset}"
    await audit(request, "crew.set", detail=detail, user=_user)


@router.get("/api/races/{race_id}/crew")
async def api_get_crew(
    request: Request,
    race_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Race not found")

    crew = await storage.resolve_crew(race_id)
    summary = await storage.get_race_crew_summary(race_id)
    return JSONResponse(
        {
            "crew": crew,
            "crew_assumed": summary["crew_assumed"],
            "gear_preset": summary["gear_preset"],
            "total_body_weight_lb": summary["total_body_weight_lb"],
            "total_gear_weight_lb": summary["total_gear_weight_lb"],
            "total_crew_weight_lb": summary["total_crew_weight_lb"],
        }
    )


@router.post("/api/admin/crew-backfill", status_code=200)
async def api_crew_backfill(
    request: Request,
    body: BulkCrewBackfill,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Bulk-edit per-race crew + gear for historical races (#761 PR-C).

    Emits one ``crew.set`` audit_log row per race (data-licensing §11
    protest readiness — the trail must show who set crew weight per race,
    not a single batch row).
    """
    storage = get_storage(request)
    positions = await storage.get_crew_positions()
    valid_ids = {p["id"] for p in positions}

    updates: list[dict[str, Any]] = []
    for entry in body.updates:
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (entry.race_id,))
        if await cur.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail=f"Race {entry.race_id} not found",
            )
        invalid = [c.position_id for c in entry.crew if c.position_id not in valid_ids]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown position_id(s) in race {entry.race_id}: {invalid}",
            )
        updates.append(
            {
                "race_id": entry.race_id,
                "gear_preset": entry.gear_preset,
                "crew": [
                    {
                        "position_id": c.position_id,
                        "user_id": c.user_id,
                        "attributed": c.attributed,
                        "body_weight": c.body_weight,
                        "gear_weight": c.gear_weight,
                    }
                    for c in entry.crew
                ],
            }
        )

    try:
        applied = await storage.bulk_confirm_race_crew(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    for u in updates:
        detail = f"race={u['race_id']} verified n={len(u['crew'])} backfill"
        if u.get("gear_preset"):
            detail += f" gear={u['gear_preset']}"
        await audit(request, "crew.set", detail=detail, user=_user)
    return JSONResponse({"applied": applied})


@router.post("/api/crew/defaults", status_code=204)
async def api_set_crew_defaults(
    request: Request,
    body: list[CrewEntry],
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    """Set boat-level default crew roster."""
    storage = get_storage(request)
    crew = [
        {
            "position_id": e.position_id,
            "user_id": e.user_id,
            "attributed": e.attributed,
            "body_weight": e.body_weight,
            "gear_weight": e.gear_weight,
        }
        for e in body
    ]
    try:
        await storage.set_crew_defaults(None, crew)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit(request, "crew.defaults.set", user=_user)


@router.get("/api/crew/defaults")
async def api_get_crew_defaults(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Get boat-level default crew roster."""
    storage = get_storage(request)
    defaults = await storage.get_crew_defaults(None)
    return JSONResponse({"crew": defaults})


@router.get("/api/crew/positions")
async def api_get_crew_positions(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    positions = await storage.get_crew_positions()
    return JSONResponse({"positions": positions})


@router.post("/api/crew/positions", status_code=204)
async def api_set_crew_positions(
    request: Request,
    body: list[PositionEntry],
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Admin: set configured crew positions."""
    storage = get_storage(request)
    await storage.set_crew_positions([p.model_dump() for p in body])
    await audit(request, "crew.positions.set", user=_user)


@router.get("/api/crew/users")
async def api_crew_users(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List users for crew selector (crew→admin→viewer order).

    Includes invited-but-not-yet-accepted users with ``pending: true``.
    """
    storage = get_storage(request)
    users = await storage.list_users()
    pending_emails = await storage.list_pending_invitation_emails()
    for u in users:
        u["pending"] = u["email"] in pending_emails
    role_order = {"crew": 0, "admin": 1, "viewer": 2}
    users.sort(key=lambda u: (role_order.get(u["role"], 99), u.get("name") or ""))
    return JSONResponse({"users": users})


@router.post("/api/crew/placeholder", status_code=201)
async def api_create_placeholder(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Create a placeholder user for non-system crew."""
    storage = get_storage(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    uid = await storage.create_placeholder_user(name)
    await audit(request, "crew.placeholder", detail=name, user=_user)
    return JSONResponse({"id": uid, "name": name}, status_code=201)


@router.get("/api/boats")
async def api_list_boats(
    request: Request,
    q: str | None = None,
    exclude_race: int | None = None,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    boats = await storage.list_boats(exclude_race_id=exclude_race, q=q or None)
    return JSONResponse(boats)


@router.post("/api/boats", status_code=201)
async def api_create_boat(
    request: Request,
    body: BoatCreate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    sail = body.sail_number.strip()
    if not sail:
        raise HTTPException(status_code=422, detail="sail_number must not be blank")
    boat_id = await storage.add_boat(sail, body.name, body.class_name)
    await audit(request, "boat.add", detail=sail, user=_user)
    return JSONResponse({"id": boat_id}, status_code=201)


@router.patch("/api/boats/{boat_id}", status_code=204)
async def api_update_boat(
    request: Request,
    boat_id: int,
    body: BoatUpdate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
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
    await audit(request, "boat.update", detail=str(boat_id), user=_user)


@router.delete("/api/boats/{boat_id}", status_code=204)
async def api_delete_boat(
    request: Request,
    boat_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM boats WHERE id = ?", (boat_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Boat not found")
    await storage.delete_boat(boat_id)
    await audit(request, "boat.delete", detail=str(boat_id), user=_user)


@router.get("/api/sessions/{race_id}/results")
async def api_get_results(
    request: Request,
    race_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    results = await storage.list_race_results(race_id)
    return JSONResponse(results)


@router.post("/api/sessions/{race_id}/results", status_code=201)
async def api_upsert_result(
    request: Request,
    race_id: int,
    body: RaceResultEntry,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
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
    await audit(request, "result.upsert", detail=f"race={race_id} place={body.place}", user=_user)
    return JSONResponse({"id": result_id}, status_code=201)


@router.delete("/api/results/{result_id}", status_code=204)
async def api_delete_result(
    request: Request,
    result_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM race_results WHERE id = ?", (result_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Result not found")
    await storage.delete_race_result(result_id)
    await audit(request, "result.delete", detail=str(result_id), user=_user)


# ------------------------------------------------------------------
# /api/sessions/{session_id}/notes  &  /api/notes/{note_id}
# ------------------------------------------------------------------


async def _resolve_session(request: Request, session_id: int) -> tuple[int | None, int | None]:
    """Return (race_id, audio_session_id) for the given session_id, or raise 404."""
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is not None:
        return session_id, None
    cur2 = await storage._conn().execute(
        "SELECT id FROM audio_sessions WHERE id = ?", (session_id,)
    )
    if await cur2.fetchone() is not None:
        return None, session_id
    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/crew/consents")
async def api_list_consents(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List all crew consent records."""
    storage = get_storage(request)
    consents = await storage.list_crew_consents()
    return JSONResponse(consents)


@router.get("/api/crew/{user_id:int}/consents")
async def api_get_user_consents(
    request: Request,
    user_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Get consent records for a specific user."""
    storage = get_storage(request)
    consents = await storage.get_crew_consents(user_id)
    return JSONResponse(consents)


@router.put("/api/crew/{user_id:int}/consents", status_code=200)
async def api_set_consent(
    request: Request,
    user_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Set or revoke consent for a user."""
    storage = get_storage(request)
    body = await request.json()
    consent_type = (body.get("consent_type") or "").strip()
    valid = ("audio", "video", "name", "photo", "biometric", "voice_profile")
    if consent_type not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"consent_type must be one of: {', '.join(valid)}",
        )
    granted = bool(body.get("granted", True))
    # Revoking voice_profile consent triggers hard delete of biometric data
    if consent_type == "voice_profile" and not granted:
        await storage.revoke_voice_profile_consent(user_id)
    else:
        await storage.set_crew_consent(user_id, consent_type, granted)
    row_id = 0  # consent id not critical for response
    action = "consent.grant" if granted else "consent.revoke"
    await audit(request, action, detail=f"user={user_id}/{consent_type}", user=_user)
    return JSONResponse(
        {
            "id": row_id,
            "user_id": user_id,
            "consent_type": consent_type,
            "granted": granted,
        }
    )


@router.delete("/api/crew/{user_id:int}/voice-profile", status_code=204)
async def api_delete_voice_profile(
    request: Request,
    user_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    """Delete a crew member's voice profile."""
    storage = get_storage(request)
    deleted = await storage.delete_voice_profile(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    await audit(request, "voice_profile.delete", detail=f"user={user_id}", user=_user)


@router.post("/api/crew/{user_id:int}/anonymize", status_code=200)
async def api_anonymize_sailor(
    request: Request,
    user_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Anonymize a crew member's name across all sessions."""
    storage = get_storage(request)
    count = await storage.anonymize_sailor(user_id)
    await audit(request, "sailor.anonymize", detail=f"user={user_id}", user=_user)
    return JSONResponse({"user_id": user_id, "rows_updated": count})


@router.get("/api/users/names")
async def api_user_names(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return list of {id, name} for @mention autocomplete."""
    storage = get_storage(request)
    users = await storage.list_users()
    return JSONResponse(
        [
            {"id": u["id"], "name": u["name"] or u["email"]}
            for u in users
            if u.get("name") or u.get("email")
        ]
    )
