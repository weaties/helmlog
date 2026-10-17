"""Coach share links (#849): no-account, expiring links to a race or compare set.

Spec rows P6–P10 on https://github.com/weaties/helmlog/issues/848 (§2). The
link grant shares the coach scope / allowlist / filtering from #848, so these
tests focus on the link principal itself.
"""

from __future__ import annotations

import hashlib
import re
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


async def _race(storage: Storage, name: str, start: datetime) -> int:
    race = await storage.start_race("Evt", start, start.date().isoformat(), 1, name, "race")
    await storage.end_race(race.id, start + timedelta(hours=1))
    return race.id


async def _link(
    storage: Storage,
    session_ids: list[int],
    *,
    starts: datetime | None = None,
    expires: datetime | None = None,
    landing: str | None = None,
) -> tuple[int, str]:
    token = generate_token()
    now = datetime.now(UTC)
    gid = await storage.create_coach_grant(
        kind="link",
        label="Clinic",
        coach_email=None,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        all_sessions=False,
        allow_video=False,
        landing_path=landing,
        starts_at=(starts or now - timedelta(minutes=1)).isoformat(),
        expires_at=(expires or now + timedelta(days=14)).isoformat(),
        created_by=None,
        session_ids=session_ids,
    )
    return gid, token


@pytest_asyncio.fixture
async def env(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setenv("AUTH_DISABLED", "false")
    granted = await _race(storage, "R1", START)
    other = await _race(storage, "R2", START + timedelta(hours=2))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(storage)),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        yield {
            "c": c,
            "s": storage,
            "granted": granted,
            "other": other,
            "admin": await _login(storage, "admin@x.com", "admin"),
            "viewer": await _login(storage, "viewer@x.com", "viewer"),
        }


def _cookies(c: httpx.AsyncClient, **kw: str) -> None:
    c.cookies.clear()
    for k, v in kw.items():
        c.cookies.set(k, v)


JSON = {"accept": "application/json"}
HTML = {"accept": "text/html"}


# ---------------------------------------------------------------------------
# P9 / P10 — /share/<token>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p9_share_sets_cookie_and_redirects(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    landing = f"/session/{env['granted']}"
    gid, token = await _link(s, [env["granted"]], landing=landing)
    _cookies(c)
    r = await c.get(f"/share/{token}", headers=HTML)
    assert r.status_code == 303
    assert r.headers["location"] == landing
    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith("coach_link=")
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    max_age = int(set_cookie.lower().split("max-age=")[1].split(";")[0])
    assert 13 * 86400 < max_age <= 14 * 86400
    log = await s.list_audit_log(limit=10)
    opens = [e for e in log if e["action"] == "coach.link_open"]
    assert len(opens) == 1 and f'"grant_id": {gid}' in opens[0]["detail"]
    assert token not in opens[0]["detail"]


@pytest.mark.asyncio
async def test_p9_share_without_landing_goes_to_coach(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    _gid, token = await _link(s, [env["granted"]])
    _cookies(c)
    r = await c.get(f"/share/{token}", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/coach"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "expired", "revoked", "scheduled"])
async def test_p10_share_invalid(env: dict[str, Any], state: str) -> None:
    c, s = env["c"], env["s"]
    now = datetime.now(UTC)
    token = generate_token()
    if state == "expired":
        _gid, token = await _link(
            s, [env["granted"]], starts=now - timedelta(days=3), expires=now - timedelta(seconds=1)
        )
    elif state == "revoked":
        gid, token = await _link(s, [env["granted"]])
        await s.revoke_coach_grant(gid, now.isoformat())
    elif state == "scheduled":
        _gid, token = await _link(s, [env["granted"]], starts=now + timedelta(days=1))
    _cookies(c)
    r = await c.get(f"/share/{token}", headers=HTML)
    assert r.status_code == 404
    assert "invalid or has expired" in r.text
    assert "set-cookie" not in r.headers


@pytest.mark.asyncio
async def test_account_grant_token_space_is_separate(env: dict[str, Any]) -> None:
    # An account grant has no token, so /share/<anything> can't hit it.
    c = env["c"]
    _cookies(c)
    assert (await c.get("/share/", headers=HTML)).status_code in (404, 405)


# ---------------------------------------------------------------------------
# P6 / P7 / P8 — the coach_link cookie as a principal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_link_principal_scope(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    _gid, token = await _link(s, [env["granted"]])
    _cookies(c, coach_link=token)
    ok = await c.get(f"/api/sessions/{env['granted']}/detail", headers=JSON)
    assert ok.status_code == 200 and "has_audio" not in ok.json()
    assert (await c.get(f"/api/sessions/{env['other']}/detail", headers=JSON)).status_code == 404
    assert (await c.get("/api/sessions", headers=JSON)).status_code == 403
    assert (await c.get("/history", headers=HTML)).status_code == 303
    r = await c.post(f"/api/sessions/{env['granted']}/results", json={"place": 1})
    assert r.status_code == 403
    me = (await c.get("/api/me", headers=JSON)).json()
    assert me["role"] == "coach" and me["id"] is None


@pytest.mark.asyncio
async def test_p6_link_coach_landing(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    _gid, token = await _link(s, [env["granted"]])
    _cookies(c, coach_link=token)
    page = await c.get("/coach", headers=HTML)
    assert page.status_code == 200 and "R1" in page.text and "R2" not in page.text
    body = (await c.get("/api/coach/sessions", headers=JSON)).json()
    assert [r["id"] for r in body["sessions"]] == [env["granted"]]
    assert body["expires_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "expired", "revoked"])
async def test_p7_dead_link_cookie(env: dict[str, Any], state: str) -> None:
    c, s = env["c"], env["s"]
    now = datetime.now(UTC)
    token = generate_token()
    if state == "expired":
        _gid, token = await _link(
            s, [env["granted"]], starts=now - timedelta(days=3), expires=now - timedelta(seconds=1)
        )
    elif state == "revoked":
        gid, token = await _link(s, [env["granted"]])
        await s.revoke_coach_grant(gid, now.isoformat())
    _cookies(c, coach_link=token)
    api = await c.get(f"/api/sessions/{env['granted']}/detail", headers=JSON)
    assert api.status_code == 401
    assert 'coach_link=""' in api.headers["set-cookie"] or "Max-Age=0" in api.headers["set-cookie"]
    page = await c.get(f"/session/{env['granted']}", headers=HTML)
    assert page.status_code == 401
    assert "invalid or has expired" in page.text


@pytest.mark.asyncio
async def test_p7_revocation_is_immediate_for_links(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    gid, token = await _link(s, [env["granted"]])
    _cookies(c, coach_link=token)
    assert (await c.get(f"/api/sessions/{env['granted']}/detail", headers=JSON)).status_code == 200
    await s.revoke_coach_grant(gid, datetime.now(UTC).isoformat())
    assert (await c.get(f"/api/sessions/{env['granted']}/detail", headers=JSON)).status_code == 401


@pytest.mark.asyncio
async def test_p8_session_cookie_wins(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    _gid, token = await _link(s, [env["granted"]])
    _cookies(c, session=env["viewer"], coach_link=token)
    r = await c.get(f"/api/sessions/{env['other']}/detail", headers=JSON)
    assert r.status_code == 200 and "has_audio" in r.json()


@pytest.mark.asyncio
async def test_link_views_are_audited_as_link(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    gid, token = await _link(s, [env["granted"]])
    _cookies(c, coach_link=token)
    await c.get(f"/api/sessions/{env['granted']}/detail", headers=JSON)
    views = [e for e in await s.list_audit_log(limit=10) if e["action"] == "coach.session_view"]
    assert len(views) == 1
    assert '"via": "link"' in views[0]["detail"] and f"[{gid}]" in views[0]["detail"]
    assert views[0]["user_id"] is None


@pytest.mark.asyncio
async def test_logout_clears_link_cookie(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    _gid, token = await _link(s, [env["granted"]])
    _cookies(c, coach_link=token)
    r = await c.post("/logout")
    assert r.status_code == 303
    assert "coach_link=" in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Admin: creating links
# ---------------------------------------------------------------------------


def _link_body(**kw: Any) -> dict[str, Any]:  # noqa: ANN401
    body: dict[str, Any] = {
        "kind": "link",
        "label": "Clinic",
        "session_ids": [],
        "allow_video": False,
        "expires_at": (datetime.now(UTC) + timedelta(days=14)).isoformat(),
    }
    body.update(kw)
    return body


@pytest.mark.asyncio
async def test_admin_creates_link(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    _cookies(c, session=env["admin"])
    landing = f"/session/{env['granted']}/compare?ids=1,2"
    r = await c.post(API, json=_link_body(session_ids=[env["granted"]], landing_path=landing))
    assert r.status_code == 201, r.text
    url = r.json()["share_url"]
    assert url.startswith("http://test/share/")
    token = url.rsplit("/", 1)[1]
    g = await s.get_coach_grant(r.json()["id"])
    assert g["kind"] == "link" and g["coach_email"] is None
    assert g["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert g["landing_path"] == landing
    listed = (await c.get(API)).json()["grants"]
    assert all("token_hash" not in x for x in listed)
    log = [e for e in await s.list_audit_log(limit=10) if e["action"] == "coach.grant_create"]
    assert log and token not in log[0]["detail"]
    # The minted link works end to end.
    _cookies(c)
    r = await c.get(f"/share/{token}", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == landing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "landing", ["https://evil.example/", "//evil.example/x", "/history", "/admin/users", "nope"]
)
async def test_admin_rejects_bad_landing_path(env: dict[str, Any], landing: str) -> None:
    c = env["c"]
    _cookies(c, session=env["admin"])
    r = await c.post(API, json=_link_body(session_ids=[env["granted"]], landing_path=landing))
    assert r.status_code == 422
    assert "landing_path" in r.text


@pytest.mark.asyncio
async def test_admin_link_kind_validation(env: dict[str, Any]) -> None:
    c = env["c"]
    _cookies(c, session=env["admin"])
    r = await c.post(API, json=_link_body(kind="bogus", session_ids=[env["granted"]]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_share_button_admin_only(env: dict[str, Any]) -> None:
    c, s = env["c"], env["s"]
    slug = (await s.get_race(env["granted"])).slug  # type: ignore[union-attr]
    page = f"/session/{env['granted']}/{slug}"
    _cookies(c, session=env["admin"])
    admin = (await c.get(page, headers=HTML)).text
    assert 'id="coach-share-btn"' in admin and "/static/coach_share.js" in admin
    # The onclick must survive HTML attribute quoting (a JSON-quoted label
    # inside onclick="..." once truncated it).
    onclick = re.search(r'id="coach-share-btn".*?onclick="([^"]*)"', admin, re.S)
    assert onclick and onclick.group(1).rstrip().endswith("})")
    compare = (await c.get(f"/session/{env['granted']}/compare", headers=HTML)).text
    assert 'id="coach-share-btn"' in compare and "/static/coach_share.js" in compare
    _cookies(c, session=env["viewer"])
    viewer = (await c.get(page, headers=HTML)).text
    assert "coach-share-btn" not in viewer and "coach_share.js" not in viewer
