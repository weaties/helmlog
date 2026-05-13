"""Route handlers for me."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import hash_password, require_auth, verify_password
from helmlog.routes._helpers import PasswordChange, WeightUpdate, audit, get_storage

router = APIRouter()


@router.get("/api/me")
async def api_me(request: Request) -> JSONResponse:
    """Return the current user's identity and role."""
    get_storage(request)
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
            "weight_lbs": user.get("weight_lbs"),
        }
    )


@router.patch("/api/me/weight", status_code=204)
async def api_update_my_weight(
    request: Request,
    body: WeightUpdate,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    """Update the current user's body weight.

    Weight is biometric data — requires biometric consent per data licensing policy.
    """
    storage = get_storage(request)
    weight = body.weight_lbs
    if weight is not None:
        consents = await storage.get_crew_consents(_user["id"])
        bio_consent = next(
            (c for c in consents if c["consent_type"] == "biometric" and c["granted"]),
            None,
        )
        if not bio_consent:
            raise HTTPException(
                status_code=403,
                detail="Biometric consent required before storing weight data",
            )
    # Only touch fields the caller explicitly sent — avoids clearing one
    # value when the other is being updated.
    fields_set = body.model_fields_set
    if "weight_lbs" in fields_set:
        await storage.update_user_weight(_user["id"], weight)
    # Gear weight is a clothing/equipment preference, not biometric — no
    # consent gate.
    if "gear_default_lbs" in fields_set:
        await storage.update_user_gear_default(_user["id"], body.gear_default_lbs)


@router.patch("/api/me/password", status_code=204)
async def api_change_password(
    request: Request,
    body: PasswordChange,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    """Change the current user's password."""
    storage = get_storage(request)
    user_id = _user["id"]

    cred = await storage.get_credential(user_id, "password")
    if cred is None:
        raise HTTPException(status_code=422, detail="No password credential")

    if not verify_password(body.current_password, cred["password_hash"]):
        raise HTTPException(status_code=403, detail="Current password is incorrect")

    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match")

    new_hash = hash_password(body.new_password)
    await storage.update_password_hash(user_id, new_hash)
    await audit(request, "password.change", user=_user)


@router.patch("/api/me/name", status_code=204)
async def api_update_my_name(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    """Update the current user's display name."""
    storage = get_storage(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name must not be blank")
    await storage.update_user_profile(_user["id"], name, None)


def _login_ctx(next_url: str, error_html: str = "") -> dict[str, Any]:
    from helmlog.oauth import enabled_providers

    return {
        "next_url": next_url,
        "error_html": error_html,
        "oauth_providers": enabled_providers(),
    }


@router.delete("/api/users/{user_id}", status_code=204)
async def api_delete_user(
    request: Request,
    user_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Anonymize and delete a user account (admin only)."""
    storage = get_storage(request)
    target = await storage.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Delete avatar file
    avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
    avatar_file = avatar_dir / f"{user_id}.jpg"
    if avatar_file.exists():
        await asyncio.to_thread(avatar_file.unlink)
    await storage.delete_user(user_id)
    await audit(request, "user.delete", detail=f"user_id={user_id}", user=_user)


@router.delete("/api/me", status_code=204)
async def api_delete_me(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> None:
    """Self-delete: anonymize and remove own account."""
    storage = get_storage(request)
    user_id = _user.get("id")
    if user_id is None:
        raise HTTPException(status_code=400, detail="Cannot delete mock user")
    avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
    avatar_file = avatar_dir / f"{user_id}.jpg"
    if avatar_file.exists():
        await asyncio.to_thread(avatar_file.unlink)
    await storage.delete_user(user_id)
    await audit(request, "user.self_delete", detail=f"user_id={user_id}", user=_user)
