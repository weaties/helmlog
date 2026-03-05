"""Tests for src/logger/auth.py and related auth-protected routes."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from logger.auth import (
    generate_token,
    invite_expires_at,
    session_expires_at,
)
from logger.web import create_app

if TYPE_CHECKING:
    from logger.storage import Storage

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


@pytest.mark.asyncio
async def test_invite_token_lifecycle(storage: Storage) -> None:
    user_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    await storage.create_invite_token(token, "invitee@x.com", "crew", user_id, invite_expires_at())

    row = await storage.get_invite_token(token)
    assert row is not None
    assert row["used_at"] is None
    assert row["role"] == "crew"

    await storage.redeem_invite_token(token)
    row2 = await storage.get_invite_token(token)
    assert row2 is not None
    assert row2["used_at"] is not None


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
# Invite token redemption via POST /login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_with_valid_token_creates_session(storage: Storage) -> None:
    """POST /login with a valid token sets a session cookie."""
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    await storage.create_invite_token(token, "new@x.com", "crew", admin_id, invite_expires_at())

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.post("/login", data={"token": token, "next": "/"})

    assert resp.status_code == 303
    assert "session" in resp.cookies

    # Token is marked used
    row = await storage.get_invite_token(token)
    assert row is not None
    assert row["used_at"] is not None

    # User was created
    user = await storage.get_user_by_email("new@x.com")
    assert user is not None
    assert user["role"] == "crew"


@pytest.mark.asyncio
async def test_login_with_used_token_returns_400(storage: Storage) -> None:
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    await storage.create_invite_token(token, "x@x.com", "viewer", admin_id, invite_expires_at())
    await storage.redeem_invite_token(token)  # already used

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/login", data={"token": token, "next": "/"})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_login_with_expired_token_returns_400(storage: Storage) -> None:
    admin_id = await storage.create_user("admin@x.com", None, "admin")
    token = generate_token()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await storage.create_invite_token(token, "y@x.com", "viewer", admin_id, past)

    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/login", data={"token": token, "next": "/"})

    assert resp.status_code == 400


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


# ---------------------------------------------------------------------------
# Admin invite endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_invite_generates_url(storage: Storage) -> None:
    """POST /admin/users/invite returns an invite URL."""
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
    assert "/login?token=" in data["invite_url"]


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


@pytest.mark.asyncio
async def test_login_rate_limited(storage: Storage) -> None:
    """POST /login is rate-limited to 10 requests per minute."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for _ in range(10):
                await client.post("/login", data={"token": "bad", "next": "/"})
            resp = await client.post("/login", data={"token": "bad", "next": "/"})
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Self-service login link (issue #148)
# ---------------------------------------------------------------------------

_SMTP_ENV = {"SMTP_HOST": "localhost", "SMTP_PORT": "587", "SMTP_FROM": "test@test.com"}


@pytest.mark.asyncio
async def test_count_recent_tokens_empty(storage: Storage) -> None:
    """count_recent_tokens_for_email returns 0 when no tokens exist."""
    count = await storage.count_recent_tokens_for_email("nobody@x.com")
    assert count == 0


@pytest.mark.asyncio
async def test_count_recent_tokens_counts(storage: Storage) -> None:
    """count_recent_tokens_for_email counts recently-created tokens."""
    user_id = await storage.create_user("counter@x.com", None, "crew")
    for _ in range(3):
        await storage.create_invite_token(
            generate_token(), "counter@x.com", "crew", user_id, invite_expires_at()
        )
    count = await storage.count_recent_tokens_for_email("counter@x.com")
    assert count == 3


@pytest.mark.asyncio
async def test_create_invite_token_null_created_by(storage: Storage) -> None:
    """create_invite_token accepts None for created_by."""
    token = generate_token()
    await storage.create_invite_token(token, "null@x.com", "viewer", None, invite_expires_at())
    row = await storage.get_invite_token(token)
    assert row is not None
    assert row["email"] == "null@x.com"


@pytest.mark.asyncio
async def test_send_login_link_email() -> None:
    """send_login_link_email calls send_email with correct subject."""
    with patch("logger.email.send_email", return_value=True) as mock_send:
        from logger.email import send_login_link_email

        result = await send_login_link_email("Alice", "a@x.com", "http://test/login?token=abc")
    assert result is True
    mock_send.assert_called_once()
    args = mock_send.call_args
    assert args[0][0] == "a@x.com"
    assert "login link" in args[0][1].lower()
    assert "http://test/login?token=abc" in args[0][2]
    assert "didn't request" in args[0][2].lower()


@pytest.mark.asyncio
async def test_request_link_sends_email(storage: Storage) -> None:
    """POST /auth/request-link for an existing user creates a token and sends email."""
    await storage.create_user("crew@boat.com", "Sailor", "crew")
    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("logger.email._send_sync") as mock_smtp,
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/auth/request-link", data={"email": "crew@boat.com"})

    assert resp.status_code == 200
    assert "login link has been sent" in resp.text.lower()
    mock_smtp.assert_called_once()

    # A token was created for this email
    cur = await storage._conn().execute(
        "SELECT COUNT(*) FROM invite_tokens WHERE email = ?", ("crew@boat.com",)
    )
    row = await cur.fetchone()
    assert row[0] == 1

    # Audit log entry exists
    entries = await storage.list_audit_log(limit=10)
    actions = [e["action"] for e in entries]
    assert "auth.request_link" in actions


@pytest.mark.asyncio
async def test_request_link_unknown_email(storage: Storage) -> None:
    """POST /auth/request-link for unknown email returns same HTML, no token."""
    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("logger.email._send_sync") as mock_smtp,
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/auth/request-link", data={"email": "ghost@x.com"})

    assert resp.status_code == 200
    assert "login link has been sent" in resp.text.lower()
    mock_smtp.assert_not_called()


@pytest.mark.asyncio
async def test_request_link_no_smtp(storage: Storage) -> None:
    """Without SMTP, POST /auth/request-link returns generic response."""
    await storage.create_user("nosend@x.com", None, "crew")
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/auth/request-link", data={"email": "nosend@x.com"})

    assert resp.status_code == 200
    assert "login link has been sent" in resp.text.lower()

    # No token was created
    cur = await storage._conn().execute(
        "SELECT COUNT(*) FROM invite_tokens WHERE email = ?", ("nosend@x.com",)
    )
    row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_request_link_rate_limited_by_ip(storage: Storage) -> None:
    """POST /auth/request-link is rate-limited by IP (5/minute)."""
    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("logger.email._send_sync"),
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(5):
                await client.post("/auth/request-link", data={"email": "x@x.com"})
            resp = await client.post("/auth/request-link", data={"email": "x@x.com"})
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_request_link_rate_limited_by_email(storage: Storage) -> None:
    """4th request for the same email in an hour creates no new token."""
    user_id = await storage.create_user("busy@x.com", "Busy", "crew")
    # Pre-create 3 tokens (simulating 3 recent requests)
    for _ in range(3):
        await storage.create_invite_token(
            generate_token(), "busy@x.com", "crew", user_id, invite_expires_at()
        )

    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("logger.email._send_sync") as mock_smtp,
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/auth/request-link", data={"email": "busy@x.com"})

    assert resp.status_code == 200  # Still generic response
    mock_smtp.assert_not_called()  # No email sent

    # Still only 3 tokens
    cur = await storage._conn().execute(
        "SELECT COUNT(*) FROM invite_tokens WHERE email = ?", ("busy@x.com",)
    )
    row = await cur.fetchone()
    assert row[0] == 3


@pytest.mark.asyncio
async def test_request_link_empty_email(storage: Storage) -> None:
    """POST /auth/request-link with empty email returns generic response."""
    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with patch.dict(os.environ, env):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/auth/request-link", data={"email": ""})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_page_shows_email_form_with_smtp(storage: Storage) -> None:
    """GET /login shows the email form when SMTP is configured."""
    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with patch.dict(os.environ, env):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/login")
    assert resp.status_code == 200
    assert 'action="/auth/request-link"' in resp.text


@pytest.mark.asyncio
async def test_login_page_hides_email_form_without_smtp(storage: Storage) -> None:
    """GET /login hides the email form when SMTP is not configured."""
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}, clear=False):
        # Ensure no SMTP vars
        os.environ.pop("SMTP_HOST", None)
        os.environ.pop("SMTP_PORT", None)
        os.environ.pop("SMTP_FROM", None)
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/login")
    assert resp.status_code == 200
    assert 'action="/auth/request-link"' not in resp.text


@pytest.mark.asyncio
async def test_request_link_token_redeemable(storage: Storage) -> None:
    """E2E: request link → extract token from DB → POST /login → session created."""
    await storage.create_user("e2e@x.com", "E2E", "crew")

    env = {"AUTH_DISABLED": "false", **_SMTP_ENV}
    with (
        patch.dict(os.environ, env),
        patch("logger.email._send_sync"),
    ):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            # Step 1: request a login link
            await client.post("/auth/request-link", data={"email": "e2e@x.com"})

            # Step 2: extract the token from the DB
            cur = await storage._conn().execute(
                "SELECT token FROM invite_tokens WHERE email = ? AND used_at IS NULL",
                ("e2e@x.com",),
            )
            row = await cur.fetchone()
            assert row is not None
            token = row[0]

            # Step 3: redeem the token via POST /login
            resp = await client.post("/login", data={"token": token, "next": "/"})

    assert resp.status_code == 303
    assert "session" in resp.cookies
