"""Coach access end-to-end through the real middleware + routes (#848).

Spec: https://github.com/weaties/helmlog/issues/848. Section numbers below
refer to it: §2 principal resolution, §3 route access, §4 field filtering,
§5 audit. Auth is enabled (AUTH_DISABLED=false) for every test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from starlette.routing import Route, WebSocketRoute

from helmlog.auth import generate_token, session_expires_at
from helmlog.coach import coach_path_allowed
from helmlog.maneuver_detector import Maneuver
from helmlog.web import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from helmlog.storage import Storage

START = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)


@dataclass
class World:
    app: Any
    storage: Storage
    granted: int  # race in the coach's grant, with video
    granted_novideo: int  # race in a grant without video
    other: int  # race not granted
    admin: str  # session cookies
    viewer: str
    coach: str
    granted_maneuver: int
    other_maneuver: int


async def _login(storage: Storage, email: str, role: str) -> str:
    uid = await storage.create_user(email, email.split("@")[0], role)
    sid = generate_token()
    await storage.create_session(sid, uid, session_expires_at())
    return sid


async def _race(storage: Storage, name: str, start: datetime) -> int:
    race = await storage.start_race("Jfest", start, start.date().isoformat(), 1, name, "race")
    await storage.end_race(race.id, start + timedelta(hours=1))
    return race.id


async def _maneuver(storage: Storage, race_id: int, start: datetime) -> int:
    ts = start + timedelta(minutes=10)
    await storage.write_maneuvers(
        race_id,
        [Maneuver("tack", ts, ts + timedelta(seconds=8), 8.0, 0.5, None, 12, 45)],
    )
    (m,) = await storage.get_session_maneuvers(race_id)
    return int(m["id"])


async def grant(
    storage: Storage,
    *,
    email: str = "coach@x.com",
    session_ids: list[int] | None = None,
    all_sessions: bool = False,
    allow_video: bool = False,
    starts: datetime | None = None,
    expires: datetime | None = None,
) -> int:
    now = datetime.now(UTC)
    return await storage.create_coach_grant(
        kind="account",
        label="Coach",
        coach_email=email,
        token_hash=None,
        all_sessions=all_sessions,
        allow_video=allow_video,
        landing_path=None,
        starts_at=(starts or now - timedelta(days=1)).isoformat(),
        expires_at=(expires or now + timedelta(days=30)).isoformat(),
        created_by=None,
        session_ids=session_ids or [],
    )


@pytest_asyncio.fixture
async def world(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("AUTH_DISABLED", "false")
    granted = await _race(storage, "Jfest-R1", START)
    granted_novideo = await _race(storage, "Jfest-R2", START + timedelta(hours=2))
    other = await _race(storage, "Jfest-R3", START + timedelta(hours=4))
    for rid, vid in ((granted, "vidA"), (granted_novideo, "vidB"), (other, "vidC")):
        await storage.add_race_video(
            rid,
            f"https://youtu.be/{vid}",
            vid,
            "t",
            "Bow",
            START,
            0.0,
            duration_s=8 * 3600.0,
        )
    boat = await storage.find_or_create_boat("USA 1")
    for rid in (granted, other):
        await storage.upsert_race_result(rid, 1, boat, notes="private crew note")
    gm = await _maneuver(storage, granted, START)
    om = await _maneuver(storage, other, START + timedelta(hours=4))
    w = World(
        app=create_app(storage),
        storage=storage,
        granted=granted,
        granted_novideo=granted_novideo,
        other=other,
        admin=await _login(storage, "admin@x.com", "admin"),
        viewer=await _login(storage, "viewer@x.com", "viewer"),
        coach=await _login(storage, "coach@x.com", "coach"),
        granted_maneuver=gm,
        other_maneuver=om,
    )
    await grant(storage, session_ids=[granted], allow_video=True)
    await grant(storage, session_ids=[granted_novideo])
    return w


@pytest_asyncio.fixture
async def http(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        yield c


async def get(
    http: httpx.AsyncClient, cookie: str | None, path: str, *, html: bool = False
) -> httpx.Response:
    http.cookies.clear()
    if cookie:
        http.cookies.set("session", cookie)
    headers = {"accept": "text/html"} if html else {"accept": "application/json"}
    return await http.get(path, headers=headers)


# ---------------------------------------------------------------------------
# §2 Principal resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p3_staff_unchanged(world: World, http: httpx.AsyncClient) -> None:
    r = await get(http, world.viewer, f"/api/sessions/{world.other}/detail")
    assert r.status_code == 200
    assert "has_audio" in r.json()


@pytest.mark.asyncio
async def test_p4_coach_with_active_grant(world: World, http: httpx.AsyncClient) -> None:
    r = await get(http, world.coach, f"/api/sessions/{world.granted}/detail")
    assert r.status_code == 200
    assert r.json()["id"] == world.granted


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["none", "scheduled", "expired", "revoked"])
async def test_p5_coach_without_active_grant(
    world: World, http: httpx.AsyncClient, state: str
) -> None:
    s = world.storage
    sid = await _login(s, "lonely@x.com", "coach")
    now = datetime.now(UTC)
    if state == "scheduled":
        await grant(
            s, email="lonely@x.com", session_ids=[world.granted], starts=now + timedelta(days=1)
        )
    elif state == "expired":
        await grant(
            s,
            email="lonely@x.com",
            session_ids=[world.granted],
            starts=now - timedelta(days=10),
            expires=now - timedelta(seconds=1),
        )
    elif state == "revoked":
        gid = await grant(s, email="lonely@x.com", session_ids=[world.granted])
        await s.revoke_coach_grant(gid, now.isoformat())
    r = await get(http, sid, f"/api/sessions/{world.granted}/detail")
    assert r.status_code == 404
    landing = await get(http, sid, "/coach", html=True)
    assert landing.status_code == 200
    assert "no active access" in landing.text.lower()


@pytest.mark.asyncio
async def test_p5_revocation_takes_effect_on_next_request(
    world: World, http: httpx.AsyncClient
) -> None:
    s = world.storage
    sid = await _login(s, "pat@x.com", "coach")
    gid = await grant(s, email="pat@x.com", session_ids=[world.granted])
    assert (await get(http, sid, f"/api/sessions/{world.granted}/detail")).status_code == 200
    await s.revoke_coach_grant(gid, datetime.now(UTC).isoformat())
    assert (await get(http, sid, f"/api/sessions/{world.granted}/detail")).status_code == 404


@pytest.mark.asyncio
async def test_staff_grant_for_their_email_is_ignored(
    world: World, http: httpx.AsyncClient
) -> None:
    # A grant naming a viewer's email doesn't change what the viewer sees.
    await grant(world.storage, email="viewer@x.com", session_ids=[world.granted])
    r = await get(http, world.viewer, f"/api/sessions/{world.other}/detail")
    assert r.status_code == 200 and "has_audio" in r.json()


@pytest.mark.asyncio
async def test_p11_anonymous(world: World, http: httpx.AsyncClient) -> None:
    assert (await get(http, None, f"/api/sessions/{world.granted}/detail")).status_code == 401


# ---------------------------------------------------------------------------
# §3 Route access — classes A/B/C/D/E/F
# ---------------------------------------------------------------------------

_CLASS_A_LEAVES = [
    "detail",
    "track",
    "replay",
    "polar",
    "wind-field",
    "wind-timeseries",
    "course-overlay",
    "race-start-overlay",
    "vakaros-overlay",
    "maneuvers",
    "results",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", _CLASS_A_LEAVES)
async def test_r2_r3_class_a(world: World, http: httpx.AsyncClient, leaf: str) -> None:
    staff = await get(http, world.admin, f"/api/sessions/{world.granted}/{leaf}")
    coach = await get(http, world.coach, f"/api/sessions/{world.granted}/{leaf}")
    assert coach.status_code == staff.status_code, coach.text  # R2
    denied = await get(http, world.coach, f"/api/sessions/{world.other}/{leaf}")
    assert denied.status_code == 404  # R3


@pytest.mark.asyncio
async def test_analysis_results_denied(world: World, http: httpx.AsyncClient) -> None:
    # Tightened from the spec: plugin output includes sail_vmg (sail
    # selection), which the policy keeps from coaches.
    r = await get(http, world.coach, f"/api/analysis/results/{world.granted}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_r2_r3_session_maneuver_compare(world: World, http: httpx.AsyncClient) -> None:
    ok = await get(
        http,
        world.coach,
        f"/api/sessions/{world.granted}/maneuvers/compare?ids={world.granted_maneuver}",
    )
    assert ok.status_code == 200
    assert [m["id"] for m in ok.json()["maneuvers"]] == [world.granted_maneuver]
    denied = await get(
        http,
        world.coach,
        f"/api/sessions/{world.other}/maneuvers/compare?ids={world.other_maneuver}",
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_r15_maneuver_from_other_session_does_not_leak(
    world: World, http: httpx.AsyncClient
) -> None:
    r = await get(
        http,
        world.coach,
        f"/api/sessions/{world.granted}/maneuvers/compare?ids={world.other_maneuver}",
    )
    assert r.status_code == 200
    assert r.json()["maneuvers"] == []
    r = await get(
        http,
        world.coach,
        f"/api/maneuvers/compare?ids={world.granted}:{world.other_maneuver}",
    )
    assert r.status_code == 200
    assert r.json()["maneuvers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/maneuvers/compare", "/api/maneuvers/overlay"])
async def test_r4_r5_multi_session(world: World, http: httpx.AsyncClient, endpoint: str) -> None:
    ok = await get(http, world.coach, f"{endpoint}?ids={world.granted}:{world.granted_maneuver}")
    assert ok.status_code == 200  # R4
    mixed = await get(
        http,
        world.coach,
        f"{endpoint}?ids={world.granted}:{world.granted_maneuver},{world.other}:{world.other_maneuver}",
    )
    assert mixed.status_code == 404  # R5
    start_token = await get(http, world.coach, f"{endpoint}?ids={world.other}:S")
    assert start_token.status_code == 404


@pytest.mark.asyncio
async def test_r6_r7_r8_videos(world: World, http: httpx.AsyncClient) -> None:
    assert (
        await get(http, world.coach, f"/api/sessions/{world.granted}/videos")
    ).status_code == 200
    r7 = await get(http, world.coach, f"/api/sessions/{world.granted_novideo}/videos")
    assert r7.status_code == 404
    assert (await get(http, world.coach, f"/api/sessions/{world.other}/videos")).status_code == 404


@pytest.mark.asyncio
async def test_r9_coach_session_list(world: World, http: httpx.AsyncClient) -> None:
    r = await get(http, world.coach, "/api/coach/sessions")
    assert r.status_code == 200
    rows = {s["id"]: s for s in r.json()["sessions"]}
    assert set(rows) == {world.granted, world.granted_novideo}
    assert rows[world.granted]["first_video_url"] == "https://youtu.be/vidA"
    assert "first_video_url" not in rows[world.granted_novideo]
    assert r.json()["expires_at"]


@pytest.mark.asyncio
async def test_r9_all_sessions_grant(world: World, http: httpx.AsyncClient) -> None:
    sid = await _login(world.storage, "season@x.com", "coach")
    await grant(world.storage, email="season@x.com", all_sessions=True)
    r = await get(http, sid, "/api/coach/sessions")
    assert {s["id"] for s in r.json()["sessions"]} == {
        world.granted,
        world.granted_novideo,
        world.other,
    }
    assert (await get(http, sid, f"/api/sessions/{world.other}/detail")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/me", "/api/coach/timezone"])
async def test_r10_identity_and_static(world: World, http: httpx.AsyncClient, path: str) -> None:
    assert (await get(http, world.coach, path)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/history", "/maneuvers", "/admin/users", "/sails"])
async def test_r11_denied_html_redirects_to_coach(
    world: World, http: httpx.AsyncClient, path: str
) -> None:
    r = await get(http, world.coach, path, html=True)
    assert r.status_code == 303
    assert r.headers["location"] == "/coach"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions",
        "/api/races/{g}/crew",
        "/api/races/{g}/export.csv",
        "/api/sessions/{g}/maneuvers.csv",
        "/api/sessions/{g}/summary",
        "/api/sessions/{g}/moments",
        "/api/crew/users",
        "/api/state",
        "/api/sessions/{g}/videos/redirect",
    ],
)
async def test_r12_denied_api(world: World, http: httpx.AsyncClient, path: str) -> None:
    r = await get(http, world.coach, path.format(g=world.granted))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_r13_writes_denied(world: World, http: httpx.AsyncClient) -> None:
    http.cookies.clear()
    http.cookies.set("session", world.coach)
    r = await http.post(f"/api/sessions/{world.granted}/results", json={"place": 1})
    assert r.status_code == 403
    r = await http.delete(f"/api/sessions/{world.granted}")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# §3 guard: walk every registered route. A coach must be stopped by the
# middleware unless the concrete path is on the allowlist, and every
# allowlisted route that takes a session id must 404 for an ungranted one.
# ---------------------------------------------------------------------------


def _concrete(path: str, session_id: int) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1).split(":")[0]
        if name in ("session_id", "race_id", "session_ref"):
            return str(session_id)
        return "1"

    return re.sub(r"\{([^}]+)\}", sub, path)


@pytest.mark.asyncio
async def test_route_walk_guard(world: World, http: httpx.AsyncClient) -> None:
    checked = 0
    for route in world.app.routes:
        if isinstance(route, WebSocketRoute):
            assert not coach_path_allowed("GET", _concrete(route.path, world.other))
            continue
        if not isinstance(route, Route) or not route.methods:
            continue
        path = _concrete(route.path, world.other)
        # Public routes (no principal needed). /share/ is the coach link
        # entry point (#849), public by design. /api/internal/timer-event is
        # the Signal K timer plugin hook, gated by its own bearer token.
        if path.startswith(("/static", "/auth/", "/co-op/", "/share/")) or path in (
            "/login",
            "/logout",
            "/healthz",
            "/api/internal/timer-event",
        ):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            http.cookies.clear()
            http.cookies.set("session", world.coach)
            r = await http.request(method, path, headers={"accept": "application/json"})
            checked += 1
            if coach_path_allowed(method, path):
                takes_session = any(
                    p in route.path for p in ("{session_id", "{race_id", "{session_ref")
                )
                if takes_session:
                    assert r.status_code == 404, f"{method} {path} -> {r.status_code}"
            else:
                assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
    assert checked > 100


# ---------------------------------------------------------------------------
# §4 Field filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_filtered(world: World, http: httpx.AsyncClient) -> None:
    with_video = (await get(http, world.coach, f"/api/sessions/{world.granted}/detail")).json()
    assert with_video["first_video_url"] == "https://youtu.be/vidA"
    for k in ("has_audio", "audio_siblings", "debrief_audio", "peer_fingerprint", "shared_name"):
        assert k not in with_video
    no_video = (
        await get(http, world.coach, f"/api/sessions/{world.granted_novideo}/detail")
    ).json()
    assert "first_video_url" not in no_video


@pytest.mark.asyncio
async def test_detail_cache_never_leaks_staff_payload(
    world: World, http: httpx.AsyncClient
) -> None:
    staff = await get(http, world.admin, f"/api/sessions/{world.granted_novideo}/detail")
    assert "has_audio" in staff.json()  # warms the shared T1 cache
    coach = await get(http, world.coach, f"/api/sessions/{world.granted_novideo}/detail")
    assert "has_audio" not in coach.json()
    assert "first_video_url" not in coach.json()
    assert coach.headers.get("cache-control") == "no-store"
    staff_again = await get(http, world.admin, f"/api/sessions/{world.granted_novideo}/detail")
    assert "has_audio" in staff_again.json()


@pytest.mark.asyncio
async def test_maneuvers_video_fields_stripped_without_video(
    world: World, http: httpx.AsyncClient
) -> None:
    await _maneuver(world.storage, world.granted_novideo, START + timedelta(hours=2))
    rows = (await get(http, world.coach, f"/api/sessions/{world.granted_novideo}/maneuvers")).json()
    assert rows and all("youtube_url" not in m for m in rows)
    staff_rows = (
        await get(http, world.admin, f"/api/sessions/{world.granted_novideo}/maneuvers")
    ).json()
    assert any(m.get("youtube_url") for m in staff_rows)


@pytest.mark.asyncio
async def test_maneuvers_video_fields_kept_with_video(
    world: World, http: httpx.AsyncClient
) -> None:
    rows = (await get(http, world.coach, f"/api/sessions/{world.granted}/maneuvers")).json()
    assert any(m.get("youtube_url") for m in rows)


@pytest.mark.asyncio
async def test_cross_session_compare_strips_video_per_session(
    world: World, http: httpx.AsyncClient
) -> None:
    nm = await _maneuver(world.storage, world.granted_novideo, START + timedelta(hours=2))
    r = await get(
        http,
        world.coach,
        f"/api/maneuvers/compare?ids={world.granted}:{world.granted_maneuver},{world.granted_novideo}:{nm}",
    )
    body = r.json()
    by_sid = {m["session_id"]: m for m in body["maneuvers"]}
    assert by_sid[world.granted].get("youtube_url")
    assert "youtube_url" not in by_sid[world.granted_novideo]
    assert str(world.granted) in body["video_sync_by_session"]
    assert str(world.granted_novideo) not in body["video_sync_by_session"]


@pytest.mark.asyncio
async def test_results_notes_stripped(world: World, http: httpx.AsyncClient) -> None:
    rows = (await get(http, world.coach, f"/api/sessions/{world.granted}/results")).json()
    assert rows and all("notes" not in r for r in rows)
    staff = (await get(http, world.admin, f"/api/sessions/{world.granted}/results")).json()
    assert staff[0]["notes"] == "private crew note"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_pages(world: World, http: httpx.AsyncClient) -> None:
    r = await get(http, world.coach, f"/session/{world.granted}", html=True)
    assert r.status_code == 302
    canonical = r.headers["location"]
    page = await get(http, world.coach, canonical, html=True)
    assert page.status_code == 200
    assert 'data-user-role="coach"' in page.text
    assert "/static/llm.js" not in page.text
    assert (await get(http, world.coach, f"/session/{world.other}", html=True)).status_code == 404
    other_slug = (await world.storage.get_race(world.other)).slug  # type: ignore[union-attr]
    assert (
        await get(http, world.coach, f"/session/{world.other}/{other_slug}", html=True)
    ).status_code == 404


@pytest.mark.asyncio
async def test_compare_pages(world: World, http: httpx.AsyncClient) -> None:
    ok = await get(http, world.coach, f"/session/{world.granted}/compare", html=True)
    assert ok.status_code == 200
    no = await get(http, world.coach, f"/session/{world.other}/compare", html=True)
    assert no.status_code == 404
    assert (await get(http, world.coach, "/compare", html=True)).status_code == 200


@pytest.mark.asyncio
async def test_debrief_id_not_reachable_by_coach(world: World, http: httpx.AsyncClient) -> None:
    # /session/<n> falls back to the audio_sessions id space for staff
    # debriefs; a coach must never get there.
    r = await get(http, world.coach, "/session/99999", html=True)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_coach_nav_is_minimal(world: World, http: httpx.AsyncClient) -> None:
    page = await get(http, world.coach, "/coach", html=True)
    assert page.status_code == 200
    assert 'href="/history"' not in page.text
    assert 'href="/admin/users"' not in page.text
    assert "race_start_widget.js" not in page.text
    assert "/api/notifications/count" not in page.text


@pytest.mark.asyncio
async def test_coach_users_excluded_from_crew_selector(
    world: World, http: httpx.AsyncClient
) -> None:
    users = (await get(http, world.admin, "/api/crew/users")).json()["users"]
    assert all(u["role"] != "coach" for u in users)


# ---------------------------------------------------------------------------
# §5 Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_session_view(world: World, http: httpx.AsyncClient) -> None:
    await get(http, world.coach, f"/api/sessions/{world.granted}/detail")
    rows = await world.storage.list_audit_log(limit=20)
    views = [r for r in rows if r["action"] == "coach.session_view"]
    assert len(views) == 1
    assert f'"session_id": {world.granted}' in views[0]["detail"]
    assert '"via": "account"' in views[0]["detail"]


@pytest.mark.asyncio
async def test_audit_cross_session_compare(world: World, http: httpx.AsyncClient) -> None:
    await get(
        http, world.coach, f"/api/maneuvers/compare?ids={world.granted}:{world.granted_maneuver}"
    )
    rows = await world.storage.list_audit_log(limit=20)
    assert any(
        r["action"] == "coach.session_view" and f'"session_id": {world.granted}' in r["detail"]
        for r in rows
    )


@pytest.mark.asyncio
async def test_staff_views_not_audited_as_coach(world: World, http: httpx.AsyncClient) -> None:
    await get(http, world.admin, f"/api/sessions/{world.granted}/detail")
    rows = await world.storage.list_audit_log(limit=20)
    assert not [r for r in rows if r["action"] == "coach.session_view"]


# ---------------------------------------------------------------------------
# Data licensing: another boat's session (stored locally via federation) is
# never covered by our coach grant — per-boat opt-in.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_race_never_visible_to_coach(world: World, http: httpx.AsyncClient) -> None:
    s = world.storage
    peer = await s.import_race(
        name="Peer-R1",
        event="Jfest",
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
    sid = await _login(s, "season2@x.com", "coach")
    await grant(s, email="season2@x.com", all_sessions=True, allow_video=True)
    listed = (await get(http, sid, "/api/coach/sessions")).json()["sessions"]
    assert peer not in {r["id"] for r in listed}
    assert (await get(http, sid, f"/api/sessions/{peer}/detail")).status_code == 404
    assert (await get(http, sid, f"/api/sessions/{peer}/track")).status_code == 404
    assert (await get(http, sid, f"/session/{peer}", html=True)).status_code == 404
