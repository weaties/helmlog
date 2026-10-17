"""Admin management of coach grants (#848): create / list / edit / revoke."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio

from helmlog.auth import generate_token, session_expires_at
from helmlog.web import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from helmlog.storage import Storage

START = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
API = "/api/admin/coach-grants"


async def _login(storage: Storage, email: str, role: str) -> str:
    uid = await storage.create_user(email, None, role)
    sid = generate_token()
    await storage.create_session(sid, uid, session_expires_at())
    return sid


@pytest_asyncio.fixture
async def ctx(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, Storage, dict[str, Any]]]:
    monkeypatch.setenv("AUTH_DISABLED", "false")
    r1 = await storage.start_race("Evt", START, "2026-09-26", 1, "R1", "race")
    r2 = await storage.start_race("Evt", START + timedelta(hours=2), "2026-09-26", 2, "R2", "race")
    ids = {
        "r1": r1.id,
        "r2": r2.id,
        "admin": await _login(storage, "admin@x.com", "admin"),
        "crew": await _login(storage, "crew@x.com", "crew"),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(storage)), base_url="http://test"
    ) as c:
        c.cookies.set("session", ids["admin"])
        yield c, storage, ids


def _body(**kw: Any) -> dict[str, Any]:  # noqa: ANN401
    body: dict[str, Any] = {
        "label": "Coach Pat",
        "coach_email": "pat@coach.com",
        "all_sessions": False,
        "allow_video": False,
        "session_ids": [],
        "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    }
    body.update(kw)
    return body


@pytest.mark.asyncio
async def test_create_account_grant_invites_coach(ctx: Any) -> None:  # noqa: ANN401
    c, storage, ids = ctx
    r = await c.post(API, json=_body(session_ids=[ids["r1"]], allow_video=True))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invite_url"].startswith("http://test/auth/accept-invite?token=")
    g = await storage.get_coach_grant(body["id"])
    assert g["kind"] == "account" and g["coach_email"] == "pat@coach.com"
    assert g["session_ids"] == [ids["r1"]] and g["allow_video"] == 1
    # Coaches are never pre-created as (inactive) users — that would put
    # them in crew selectors.
    assert await storage.get_user_by_email("pat@coach.com") is None
    log = await storage.list_audit_log(limit=10)
    assert any(e["action"] == "coach.grant_create" for e in log)


@pytest.mark.asyncio
async def test_invited_coach_registers_with_coach_role(ctx: Any) -> None:  # noqa: ANN401
    c, storage, ids = ctx
    r = await c.post(API, json=_body(session_ids=[ids["r1"]]))
    token = r.json()["invite_url"].split("token=")[1]
    inv = await storage.get_invitation(token)
    assert inv is not None and inv["role"] == "coach"


@pytest.mark.asyncio
async def test_existing_coach_needs_no_invite(ctx: Any) -> None:  # noqa: ANN401
    c, storage, ids = ctx
    await storage.create_user("pat@coach.com", "Pat", "coach")
    r = await c.post(API, json=_body(session_ids=[ids["r1"]]))
    assert r.status_code == 201
    assert r.json()["invite_url"] is None


@pytest.mark.asyncio
async def test_email_of_staff_user_rejected(ctx: Any) -> None:  # noqa: ANN401
    c, _storage, ids = ctx
    r = await c.post(API, json=_body(coach_email="crew@x.com", session_ids=[ids["r1"]]))
    assert r.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"label": "  "}, "label"),
        ({"coach_email": ""}, "email"),
        ({"session_ids": []}, "race"),
        ({"session_ids": [99999]}, "race"),
        ({"expires_at": "2020-01-01T00:00:00+00:00"}, "future"),
        ({"expires_at": (datetime.now(UTC) + timedelta(days=400)).isoformat()}, "366"),
        ({"expires_at": "not a date"}, "expires_at"),
    ],
)
async def test_create_validation(ctx: Any, override: dict[str, Any], fragment: str) -> None:  # noqa: ANN401
    c, _storage, _ids = ctx
    r = await c.post(API, json=_body(**override))
    assert r.status_code == 422, r.text
    assert fragment in r.text


@pytest.mark.asyncio
async def test_all_sessions_grant_needs_no_list(ctx: Any) -> None:  # noqa: ANN401
    c, _storage, _ids = ctx
    r = await c.post(API, json=_body(all_sessions=True))
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_list_grants_with_state(ctx: Any) -> None:  # noqa: ANN401
    c, _storage, ids = ctx
    gid = (await c.post(API, json=_body(session_ids=[ids["r1"]]))).json()["id"]
    r = await c.get(API)
    assert r.status_code == 200
    (g,) = r.json()["grants"]
    assert g["id"] == gid and g["state"] == "active"
    assert g["session_ids"] == [ids["r1"]]
    assert "token_hash" not in g


@pytest.mark.asyncio
async def test_update_grant(ctx: Any) -> None:  # noqa: ANN401
    c, storage, ids = ctx
    gid = (await c.post(API, json=_body(session_ids=[ids["r1"]]))).json()["id"]
    upd = {
        "label": "Coach Pat (season)",
        "all_sessions": False,
        "allow_video": True,
        "session_ids": [ids["r1"], ids["r2"]],
        "expires_at": (datetime.now(UTC) + timedelta(days=60)).isoformat(),
    }
    r = await c.put(f"{API}/{gid}", json=upd)
    assert r.status_code == 204, r.text
    g = await storage.get_coach_grant(gid)
    assert g["label"] == "Coach Pat (season)" and g["allow_video"] == 1
    assert g["session_ids"] == sorted([ids["r1"], ids["r2"]])
    assert any(e["action"] == "coach.grant_update" for e in await storage.list_audit_log())


@pytest.mark.asyncio
async def test_revoke_grant_is_terminal(ctx: Any) -> None:  # noqa: ANN401
    c, storage, ids = ctx
    gid = (await c.post(API, json=_body(session_ids=[ids["r1"]]))).json()["id"]
    assert (await c.post(f"{API}/{gid}/revoke")).status_code == 204
    g = await storage.get_coach_grant(gid)
    assert g["revoked_at"] is not None
    upd = _body(session_ids=[ids["r1"]])
    upd.pop("coach_email")
    assert (await c.put(f"{API}/{gid}", json=upd)).status_code == 409
    assert any(e["action"] == "coach.grant_revoke" for e in await storage.list_audit_log())


@pytest.mark.asyncio
async def test_missing_grant(ctx: Any) -> None:  # noqa: ANN401
    c, _storage, _ids = ctx
    assert (await c.post(f"{API}/999/revoke")).status_code == 404


@pytest.mark.asyncio
async def test_non_admin_forbidden(ctx: Any) -> None:  # noqa: ANN401
    c, _storage, ids = ctx
    c.cookies.set("session", ids["crew"])
    assert (await c.get(API)).status_code == 403
    assert (await c.post(API, json=_body(all_sessions=True))).status_code == 403


@pytest.mark.asyncio
async def test_admin_page_renders(ctx: Any) -> None:  # noqa: ANN401
    c, _storage, _ids = ctx
    r = await c.get("/admin/coaches", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "Coach access" in r.text


@pytest.mark.asyncio
async def test_peer_race_cannot_be_granted(ctx: Any) -> None:  # noqa: ANN401
    c, storage, _ids = ctx
    peer = await storage.import_race(
        name="Peer-R1",
        event="Evt",
        race_num=1,
        date_str="2026-09-26",
        start_utc=START,
        end_utc=START + timedelta(hours=1),
        session_type="race",
        source="peer",
        source_id="peer-1",
        peer_fingerprint="abc123",
        peer_co_op_id="coop",
    )
    r = await c.post(API, json=_body(session_ids=[peer]))
    assert r.status_code == 422
    assert "another boat" in r.text
