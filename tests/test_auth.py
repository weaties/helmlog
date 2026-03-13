"""Tests for auth, invitations, credentials, and related auth routes (#268)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.auth import (
    generate_token,
    hash_password,
    invite_expires_at,
    reset_token_expires_at,
    session_expires_at,
    verify_password,
)
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_admin_user(storage: Storage) -> tuple[int, str]:
    """Create an admin user and return (user_id, session_id)."""
    user_id = await storage.create_user("admin@test.com", "Test Admin", "admin")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


async def _create_crew_user(storage: Storage) -> tuple[int, str]:
    user_id = await storage.create_user("crew@test.com", "Test Crew", "crew")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


async def _create_viewer_user(storage: Storage) -> tuple[int, str]:
    user_id = await storage.create_user("viewer@test.com", "Test Viewer", "viewer")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


async def _create_crew_developer(storage: Storage) -> tuple[int, str]:
    """Create a crew user with the developer flag set."""
    user_id = await storage.create_user("crewdev@test.com", "Crew Dev", "crew", is_developer=True)
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


async def _create_user_with_password(
    storage: Storage, email: str, name: str, role: str, password: str
) -> int:
    """Create a user with a password credential. Returns user_id."""
    user_id = await storage.create_user(email, name, role)
    pw_hash = hash_password(password)
    await storage.create_credential(user_id, "password", email, pw_hash)
    return user_id


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_and_verify_password() -> None:
    """hash_password / verify_password round-trip."""
    pw = "hunter2hunter2"
    h = hash_password(pw)
    assert verify_password(pw, h) is True
    assert verify_password("wrong", h) is False


# ---------------------------------------------------------------------------
# Storage auth CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_user(storage: Storage) -> None:
    """create_user / get_user_by_id / get_user_by_email round-trip."""
    user_id = await storage.create_user("alice@example.com", "Alice", "crew")
    assert isinstance(user_id, int)

    by_id = await storage.get_user_by_id(user_id)
    assert by_id is not None
    assert by_id["email"] == "alice@example.com"
    assert by_id["role"] == "crew"
    assert by_id["is_active"] == 1

    by_email = await storage.get_user_by_email("Alice@Example.COM")  # case-insensitive
    assert by_email is not None
    assert by_email["id"] == user_id


@pytest.mark.asyncio
async def test_update_user_role(storage: Storage) -> None:
    user_id = await storage.create_user("bob@example.com", None, "viewer")
    await storage.update_user_role(user_id, "admin")
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_list_users(storage: Storage) -> None:
    await storage.create_user("a@x.com", None, "viewer")
    await storage.create_user("b@x.com", None, "crew")
    users = await storage.list_users()
    assert len(users) == 2


# ---------------------------------------------------------------------------
# Invitation CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invitation_lifecycle(storage: Storage) -> None:
    """create_invitation / get_invitation / accept_invitation round-trip."""
    user_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    inv_id = await storage.create_invitation(
        token, "invitee@x.com", "crew", None, False, user_id, invite_expires_at()
    )
    assert isinstance(inv_id, int)

    row = await storage.get_invitation(token)
    assert row is not None
    assert row["accepted_at"] is None
    assert row["revoked_at"] is None
    assert row["role"] == "crew"

    await storage.accept_invitation(token)
    row2 = await storage.get_invitation(token)
    assert row2 is not None
    assert row2["accepted_at"] is not None


@pytest.mark.asyncio
async def test_revoke_invitation(storage: Storage) -> None:
    user_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    inv_id = await storage.create_invitation(
        token, "r@x.com", "viewer", None, False, user_id, invite_expires_at()
    )
    await storage.revoke_invitation(inv_id)
    row = await storage.get_invitation(token)
    assert row is not None
    assert row["revoked_at"] is not None


@pytest.mark.asyncio
async def test_list_pending_invitations(storage: Storage) -> None:
    user_id = await storage.create_user("admin@x.com", None, "admin")
    # Create a pending invitation
    t1 = generate_token()
    await storage.create_invitation(
        t1, "p1@x.com", "crew", None, False, user_id, invite_expires_at()
    )
    # Create an accepted invitation
    t2 = generate_token()
    await storage.create_invitation(
        t2, "p2@x.com", "crew", None, False, user_id, invite_expires_at()
    )
    await storage.accept_invitation(t2)
    # Create an expired invitation
    t3 = generate_token()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await storage.create_invitation(t3, "p3@x.com", "crew", None, False, user_id, past)

    pending = await storage.list_pending_invitations()
    assert len(pending) == 1
    assert pending[0]["email"] == "p1@x.com"


# ---------------------------------------------------------------------------
# User credential CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_lifecycle(storage: Storage) -> None:
    user_id = await storage.create_user("cred@x.com", None, "crew")
    cred_id = await storage.create_credential(user_id, "password", "cred@x.com", "hash123")
    assert isinstance(cred_id, int)

    cred = await storage.get_credential(user_id, "password")
    assert cred is not None
    assert cred["password_hash"] == "hash123"

    by_uid = await storage.get_credential_by_provider_uid("password", "cred@x.com")
    assert by_uid is not None
    assert by_uid["user_id"] == user_id


@pytest.mark.asyncio
async def test_update_password_hash(storage: Storage) -> None:
    user_id = await storage.create_user("upd@x.com", None, "crew")
    await storage.create_credential(user_id, "password", "upd@x.com", "old_hash")
    await storage.update_password_hash(user_id, "new_hash")
    cred = await storage.get_credential(user_id, "password")
    assert cred is not None
    assert cred["password_hash"] == "new_hash"


# ---------------------------------------------------------------------------
# Password reset token CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_reset_token_lifecycle(storage: Storage) -> None:
    user_id = await storage.create_user("reset@x.com", None, "crew")
    token = generate_token()
    await storage.create_password_reset_token(token, user_id, reset_token_expires_at())

    row = await storage.get_password_reset_token(token)
    assert row is not None
    assert row["used_at"] is None

    await storage.use_password_reset_token(token)
    row2 = await storage.get_password_reset_token(token)
    assert row2 is not None
    assert row2["used_at"] is not None


# ---------------------------------------------------------------------------
# User activation / deactivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deactivate_activate_user(storage: Storage) -> None:
    user_id = await storage.create_user("active@x.com", None, "crew")
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["is_active"] == 1

    await storage.deactivate_user(user_id)
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["is_active"] == 0

    await storage.activate_user(user_id)
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["is_active"] == 1


@pytest.mark.asyncio
async def test_deactivated_user_session_rejected(storage: Storage) -> None:
    """A deactivated user's session is rejected."""
    user_id = await storage.create_user("deact@x.com", None, "viewer")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    await storage.deactivate_user(user_id)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.get("/api/state", headers={"accept": "application/json"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_lifecycle(storage: Storage) -> None:
    user_id = await storage.create_user("sess@x.com", None, "viewer")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())

    sess = await storage.get_session(session_id)
    assert sess is not None
    assert sess["user_id"] == user_id

    await storage.delete_session(session_id)
    assert await storage.get_session(session_id) is None


@pytest.mark.asyncio
async def test_delete_expired_sessions(storage: Storage) -> None:
    user_id = await storage.create_user("exp@x.com", None, "viewer")
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    session_id = generate_token()
    await storage.create_session(session_id, user_id, expired_at)

    await storage.delete_expired_sessions()
    assert await storage.get_session(session_id) is None


# ---------------------------------------------------------------------------
# Auth middleware (AUTH_DISABLED=false)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_api_returns_401(storage: Storage) -> None:
    """With auth enabled, unauthenticated API requests get 401."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/state", headers={"accept": "application/json"})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_healthz_is_public(storage: Storage) -> None:
    """GET /healthz is accessible without auth."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_login_page_is_public(storage: Storage) -> None:
    """GET /login returns HTML without auth."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_authenticated_viewer_can_read(storage: Storage) -> None:
    """A viewer with a valid session cookie can access read routes."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        _, session_id = await _create_viewer_user(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.get("/api/state")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_viewer_blocked_from_crew_route(storage: Storage) -> None:
    """A viewer session cannot access crew-level write routes."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        _, session_id = await _create_viewer_user(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.post("/api/races/start")
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_crew_blocked_from_admin_route(storage: Storage) -> None:
    """A crew session cannot access admin-only routes."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        _, session_id = await _create_crew_user(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.get("/admin/users")
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_access_admin_route(storage: Storage) -> None:
    """An admin session can access /admin/users."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        _, session_id = await _create_admin_user(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.get("/admin/users")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Developer flag (orthogonal to role hierarchy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_developer_flag_stored(storage: Storage) -> None:
    """create_user with is_developer=True persists the flag."""
    user_id = await storage.create_user("dev@x.com", "Dev", "crew", is_developer=True)
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["is_developer"] == 1


@pytest.mark.asyncio
async def test_is_developer_default_false(storage: Storage) -> None:
    """create_user defaults is_developer to False."""
    user_id = await storage.create_user("nodev@x.com", "NoDev", "crew")
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["is_developer"] == 0


@pytest.mark.asyncio
async def test_update_user_developer(storage: Storage) -> None:
    """update_user_developer toggles the flag."""
    user_id = await storage.create_user("toggle@x.com", None, "viewer")
    await storage.update_user_developer(user_id, True)
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["is_developer"] == 1
    await storage.update_user_developer(user_id, False)
    user = await storage.get_user_by_id(user_id)
    assert user is not None
    assert user["is_developer"] == 0


@pytest.mark.asyncio
async def test_crew_without_developer_blocked_from_synthesize(storage: Storage) -> None:
    """A crew user without developer flag gets 403 on synthesize."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        _, session_id = await _create_crew_user(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.post(
                "/api/sessions/synthesize",
                json={},
                headers={"content-type": "application/json"},
            )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_crew_developer_can_access_synthesize(storage: Storage) -> None:
    """A crew user with developer flag passes auth on synthesize."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        _, session_id = await _create_crew_developer(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.post(
                "/api/sessions/synthesize",
                json={},
                headers={"content-type": "application/json"},
            )
        # Should not be 403 (will be 422 or 500 due to missing body fields)
        assert resp.status_code != 403


@pytest.mark.asyncio
async def test_api_me_includes_is_developer(storage: Storage) -> None:
    """/api/me response includes is_developer field."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        _, session_id = await _create_crew_developer(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.get("/api/me")
        assert resp.status_code == 200
        assert resp.json()["is_developer"] is True


@pytest.mark.asyncio
async def test_admin_toggle_developer(storage: Storage) -> None:
    """Admin can toggle developer flag via PUT /admin/users/{id}/developer."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        target_id = await storage.create_user("target@x.com", "Target", "crew")
        _, admin_session = await _create_admin_user(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": admin_session},
        ) as client:
            resp = await client.put(
                f"/admin/users/{target_id}/developer",
                json={"is_developer": True},
            )
        assert resp.status_code == 204
        user = await storage.get_user_by_id(target_id)
        assert user is not None
        assert user["is_developer"] == 1


@pytest.mark.asyncio
async def test_list_users_includes_is_developer(storage: Storage) -> None:
    """list_users includes the is_developer field."""
    await storage.create_user("dev@x.com", "Dev", "crew", is_developer=True)
    await storage.create_user("nodev@x.com", "NoDev", "viewer")
    users = await storage.list_users()
    devs = {u["email"]: u["is_developer"] for u in users}
    assert devs["dev@x.com"] == 1
    assert devs["nodev@x.com"] == 0


# ---------------------------------------------------------------------------
# Registration via invitation (POST /auth/register)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_via_invitation(storage: Storage) -> None:
    """Full flow: admin invites, user registers with password, session created."""
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    await storage.create_invitation(
        token, "new@x.com", "crew", "New Sailor", False, admin_id, invite_expires_at()
    )

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            # GET the registration page
            resp = await client.get(f"/auth/accept-invite?token={token}")
            assert resp.status_code == 200
            assert "Create Account" in resp.text

            # POST registration
            resp = await client.post(
                "/auth/register",
                data={
                    "token": token,
                    "email": "new@x.com",
                    "name": "New Sailor",
                    "password": "securepassword",
                    "password_confirm": "securepassword",
                },
            )
            assert resp.status_code == 303
            assert "session" in resp.cookies

    # User created
    user = await storage.get_user_by_email("new@x.com")
    assert user is not None
    assert user["role"] == "crew"

    # Invitation accepted
    inv = await storage.get_invitation(token)
    assert inv is not None
    assert inv["accepted_at"] is not None

    # Credential created
    cred = await storage.get_credential(user["id"], "password")
    assert cred is not None


@pytest.mark.asyncio
async def test_register_password_mismatch(storage: Storage) -> None:
    """Registration with mismatched passwords returns 400."""
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    await storage.create_invitation(
        token, "mismatch@x.com", "crew", None, False, admin_id, invite_expires_at()
    )

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/auth/register",
                data={
                    "token": token,
                    "email": "mismatch@x.com",
                    "password": "password123",
                    "password_confirm": "different123",
                },
            )
    assert resp.status_code == 400
    assert "do not match" in resp.text.lower()


@pytest.mark.asyncio
async def test_register_password_too_short(storage: Storage) -> None:
    """Registration with short password returns 400."""
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    await storage.create_invitation(
        token, "short@x.com", "crew", None, False, admin_id, invite_expires_at()
    )

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/auth/register",
                data={
                    "token": token,
                    "email": "short@x.com",
                    "password": "short",
                    "password_confirm": "short",
                },
            )
    assert resp.status_code == 400
    assert "8 characters" in resp.text.lower()


@pytest.mark.asyncio
async def test_register_with_developer_flag(storage: Storage) -> None:
    """Invitation with developer flag creates user with developer access."""
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    await storage.create_invitation(
        token, "devuser@x.com", "crew", None, True, admin_id, invite_expires_at()
    )

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.post(
                "/auth/register",
                data={
                    "token": token,
                    "email": "devuser@x.com",
                    "name": "Dev User",
                    "password": "securepassword",
                    "password_confirm": "securepassword",
                },
            )
        assert resp.status_code == 303

    user = await storage.get_user_by_email("devuser@x.com")
    assert user is not None
    assert user["is_developer"] == 1


# ---------------------------------------------------------------------------
# Login with email+password (POST /auth/login)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_with_password(storage: Storage) -> None:
    """POST /auth/login with correct password creates session."""
    await _create_user_with_password(storage, "user@x.com", "User", "crew", "mypassword1")

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.post(
                "/auth/login",
                data={"email": "user@x.com", "password": "mypassword1", "next": "/"},
            )
    assert resp.status_code == 303
    assert "session" in resp.cookies


@pytest.mark.asyncio
async def test_login_wrong_password(storage: Storage) -> None:
    """POST /auth/login with wrong password returns 400."""
    await _create_user_with_password(storage, "user@x.com", "User", "crew", "mypassword1")

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/auth/login",
                data={"email": "user@x.com", "password": "wrongpassword", "next": "/"},
            )
    assert resp.status_code == 400
    assert "invalid" in resp.text.lower()


@pytest.mark.asyncio
async def test_login_unknown_email(storage: Storage) -> None:
    """POST /auth/login with unknown email returns 400."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/auth/login",
                data={"email": "ghost@x.com", "password": "anything", "next": "/"},
            )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_login_deactivated_user(storage: Storage) -> None:
    """POST /auth/login for deactivated user returns 400."""
    user_id = await _create_user_with_password(
        storage, "deact@x.com", "Deact", "crew", "mypassword1"
    )
    await storage.deactivate_user(user_id)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/auth/login",
                data={"email": "deact@x.com", "password": "mypassword1", "next": "/"},
            )
    assert resp.status_code == 400
    assert "deactivated" in resp.text.lower()


@pytest.mark.asyncio
async def test_login_rate_limited(storage: Storage) -> None:
    """POST /auth/login is rate-limited to 5 requests per minute."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(5):
                await client.post(
                    "/auth/login",
                    data={"email": "x@x.com", "password": "bad", "next": "/"},
                )
            resp = await client.post(
                "/auth/login",
                data={"email": "x@x.com", "password": "bad", "next": "/"},
            )
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Forgot password flow
# ---------------------------------------------------------------------------

_SMTP_ENV = {"SMTP_HOST": "localhost", "SMTP_PORT": "587", "SMTP_FROM": "test@test.com"}


@pytest.mark.asyncio
async def test_forgot_password_sends_email(storage: Storage) -> None:
    """POST /auth/forgot-password for existing user sends reset email."""
    await _create_user_with_password(storage, "forgot@x.com", "Forgot", "crew", "oldpw123")

    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("helmlog.email._send_sync") as mock_smtp,
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/auth/forgot-password", data={"email": "forgot@x.com"})

    assert resp.status_code == 200
    assert "reset link has been sent" in resp.text.lower()
    mock_smtp.assert_called_once()


@pytest.mark.asyncio
async def test_forgot_password_unknown_email(storage: Storage) -> None:
    """POST /auth/forgot-password for unknown email returns generic response."""
    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("helmlog.email._send_sync") as mock_smtp,
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/auth/forgot-password", data={"email": "ghost@x.com"})

    assert resp.status_code == 200
    assert "reset link has been sent" in resp.text.lower()
    mock_smtp.assert_not_called()


@pytest.mark.asyncio
async def test_forgot_password_rate_limited(storage: Storage) -> None:
    """POST /auth/forgot-password is rate-limited to 3/minute."""
    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("helmlog.email._send_sync"),
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(3):
                await client.post("/auth/forgot-password", data={"email": "x@x.com"})
            resp = await client.post("/auth/forgot-password", data={"email": "x@x.com"})
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Reset password flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_password_flow(storage: Storage) -> None:
    """E2E: forgot password -> get reset token from DB -> reset password -> login."""
    user_id = await _create_user_with_password(
        storage, "reset@x.com", "Reset", "crew", "oldpassword"
    )
    token = generate_token()
    await storage.create_password_reset_token(token, user_id, reset_token_expires_at())

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            # GET reset page
            resp = await client.get(f"/auth/reset-password?token={token}")
            assert resp.status_code == 200
            assert "Reset Password" in resp.text

            # POST new password
            resp = await client.post(
                "/auth/reset-password",
                data={
                    "token": token,
                    "password": "newpassword1",
                    "password_confirm": "newpassword1",
                },
            )
            assert resp.status_code == 303

            # Token is now used
            row = await storage.get_password_reset_token(token)
            assert row is not None
            assert row["used_at"] is not None

            # Can login with new password
            resp = await client.post(
                "/auth/login",
                data={"email": "reset@x.com", "password": "newpassword1", "next": "/"},
            )
            assert resp.status_code == 303


@pytest.mark.asyncio
async def test_reset_password_mismatch(storage: Storage) -> None:
    """Reset with mismatched passwords returns 400."""
    user_id = await storage.create_user("mismatch@x.com", None, "crew")
    token = generate_token()
    await storage.create_password_reset_token(token, user_id, reset_token_expires_at())

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/auth/reset-password",
                data={
                    "token": token,
                    "password": "password123",
                    "password_confirm": "different123",
                },
            )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_expired_token(storage: Storage) -> None:
    """Reset with expired token returns 400."""
    user_id = await storage.create_user("expired@x.com", None, "crew")
    token = generate_token()
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await storage.create_password_reset_token(token, user_id, past)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/auth/reset-password?token={token}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Admin invite creates invitation (not old invite_token)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_invite_generates_url(storage: Storage) -> None:
    """POST /admin/users/invite returns an invite URL with accept-invite path."""
    _, session_id = await _create_admin_user(storage)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.post(
                "/admin/users/invite",
                data={"email": "newcrew@x.com", "role": "crew"},
            )

    assert resp.status_code == 201
    data = resp.json()
    assert "invite_url" in data
    assert "/auth/accept-invite?token=" in data["invite_url"]


@pytest.mark.asyncio
async def test_admin_invite_with_developer(storage: Storage) -> None:
    """Admin invite with is_developer creates invitation with flag."""
    _, session_id = await _create_admin_user(storage)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.post(
                "/admin/users/invite",
                data={"email": "newdev@x.com", "role": "crew", "is_developer": "1"},
            )
    assert resp.status_code == 201
    token = resp.json()["token"]
    inv = await storage.get_invitation(token)
    assert inv is not None
    assert inv["is_developer"] == 1


@pytest.mark.asyncio
async def test_admin_revoke_invitation(storage: Storage) -> None:
    """Admin can revoke a pending invitation."""
    admin_id, session_id = await _create_admin_user(storage)
    token = generate_token()
    inv_id = await storage.create_invitation(
        token, "revoke@x.com", "crew", None, False, admin_id, invite_expires_at()
    )

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.post(f"/admin/invitations/{inv_id}/revoke")
    assert resp.status_code == 204

    inv = await storage.get_invitation(token)
    assert inv is not None
    assert inv["revoked_at"] is not None


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_clears_session(storage: Storage) -> None:
    """POST /logout deletes the server-side session."""
    _, session_id = await _create_viewer_user(storage)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.post("/logout", cookies={"session": session_id})

    assert resp.status_code == 303
    assert await storage.get_session(session_id) is None


# ---------------------------------------------------------------------------
# Expired session rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_session_returns_401(storage: Storage) -> None:
    """An expired session cookie is rejected."""
    user_id = await storage.create_user("old@x.com", None, "viewer")
    session_id = generate_token()
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await storage.create_session(session_id, user_id, expired_at)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            resp = await client.get("/api/state", headers={"accept": "application/json"})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_notes_photo_requires_auth(storage: Storage) -> None:
    """/notes/ path requires auth when auth is enabled (regression for #109)."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(
                "/notes/1/some_photo.jpg", headers={"accept": "application/json"}
            )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Delete user cleans up new tables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_user_cleans_credentials(storage: Storage) -> None:
    """delete_user removes credentials and nullifies invitation references."""
    user_id = await _create_user_with_password(storage, "del@x.com", "Del", "crew", "password123")
    token = generate_token()
    await storage.create_invitation(
        token, "other@x.com", "viewer", None, False, user_id, invite_expires_at()
    )

    await storage.delete_user(user_id)

    cred = await storage.get_credential(user_id, "password")
    assert cred is None

    inv = await storage.get_invitation(token)
    assert inv is not None
    assert inv["invited_by"] is None


# ---------------------------------------------------------------------------
# Accept-invite page validates token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_invite_invalid_token(storage: Storage) -> None:
    """GET /auth/accept-invite with bad token returns 400."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/auth/accept-invite?token=badtoken")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_accept_invite_expired_token(storage: Storage) -> None:
    """GET /auth/accept-invite with expired invitation returns 400."""
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await storage.create_invitation(token, "exp@x.com", "crew", None, False, admin_id, past)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/auth/accept-invite?token={token}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Password reset email content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_password_reset_email() -> None:
    """send_password_reset_email calls send_email with correct subject."""
    with patch("helmlog.email.send_email", return_value=True) as mock_send:
        from helmlog.email import send_password_reset_email

        result = await send_password_reset_email(
            "Alice", "a@x.com", "http://test/auth/reset-password?token=abc"
        )
    assert result is True
    mock_send.assert_called_once()
    args = mock_send.call_args
    assert args[0][0] == "a@x.com"
    assert "reset" in args[0][1].lower()
    assert "http://test/auth/reset-password?token=abc" in args[0][2]
