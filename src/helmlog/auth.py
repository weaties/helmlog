"""Authentication and authorisation helpers for the HelmLog web interface.

Magic-link / invite-token flow:
  1. Admin generates a single-use invite token via POST /admin/users/invite.
  2. Token is emailed or copied to the recipient as a URL: GET /login?token=<token>.
  3. Recipient's browser GETs that URL; the server redeems the token, creates a
     user (or finds the existing user by email), creates an auth_session, and
     sets a ``session`` cookie.
  4. Subsequent requests carry the cookie; ``require_auth`` validates it and
     attaches the user to the request state.

Auth can be disabled entirely (e.g. in tests) by setting the env var
``AUTH_DISABLED=true``.  In that mode every request is treated as an admin.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Cookie, HTTPException, Request, status

if TYPE_CHECKING:
    from collections.abc import Callable

    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Role ordering
# ---------------------------------------------------------------------------

_ROLE_RANK: dict[str, int] = {"viewer": 0, "crew": 1, "admin": 2}

SESSION_TTL_DAYS = int(os.getenv("AUTH_SESSION_TTL_DAYS", "90"))


def _is_auth_disabled() -> bool:
    return os.getenv("AUTH_DISABLED", "false").lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Mock admin user returned when auth is disabled
# ---------------------------------------------------------------------------

_MOCK_ADMIN: dict[str, Any] = {
    "id": None,
    "email": "admin@local",
    "name": "Local Admin",
    "role": "admin",
    "created_at": "1970-01-01T00:00:00+00:00",
    "last_seen": None,
}


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_storage(request: Request) -> Storage:
    return request.app.state.storage  # type: ignore[no-any-return]


async def _resolve_user(
    request: Request,
    session: Annotated[str | None, Cookie()] = None,
) -> dict[str, Any] | None:
    """Return the authenticated user dict, or None if not authenticated."""
    if _is_auth_disabled():
        return _MOCK_ADMIN

    if not session:
        return None

    storage: Storage = _get_storage(request)
    session_row = await storage.get_session(session)
    if not session_row:
        return None

    # Check expiry
    expires_at = datetime.fromisoformat(session_row["expires_at"])
    if datetime.now(UTC) > expires_at:
        await storage.delete_session(session)
        return None

    user = await storage.get_user_by_id(session_row["user_id"])
    if not user:
        return None

    # Fire-and-forget last_seen update (best effort)
    with contextlib.suppress(Exception):
        await storage.update_user_last_seen(user["id"])

    return user


def require_auth(min_role: str = "viewer") -> Callable[..., Any]:
    """FastAPI dependency factory. Returns a dependency that enforces *min_role*.

    Usage::

        @app.get("/admin/users")
        async def admin_users(user=Depends(require_auth("admin"))):
            ...
    """
    rank = _ROLE_RANK.get(min_role)
    if rank is None:
        raise ValueError(f"Unknown role: {min_role!r}")

    async def _dep(
        request: Request,
        session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, Any]:
        user = await _resolve_user(request, session)
        if user is None:
            # Redirect browsers to /login; return 401 for API clients
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                raise HTTPException(
                    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                    headers={"Location": f"/login?next={request.url.path}"},
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        user_rank = _ROLE_RANK.get(user["role"], -1)
        if user_rank < rank:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

        return user

    return _dep


# ---------------------------------------------------------------------------
# Token / session helpers (called from web.py routes)
# ---------------------------------------------------------------------------


def generate_token(nbytes: int = 32) -> str:
    """Return a URL-safe random token string."""
    return secrets.token_urlsafe(nbytes)


def session_expires_at() -> str:
    """Return the ISO-8601 expiry datetime for a new session."""
    return (datetime.now(UTC) + timedelta(days=SESSION_TTL_DAYS)).isoformat()


def invite_expires_at(days: int = 7) -> str:
    """Return the ISO-8601 expiry datetime for a new invite token (default 7 days)."""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()
