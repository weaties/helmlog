"""Pure coach-access logic (#848): grant lifecycle, scope, path allowlist,
field filtering. Spec: https://github.com/weaties/helmlog/issues/848 (§1–§4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helmlog.coach import (
    CoachScope,
    build_scope,
    coach_path_allowed,
    filter_detail_for_coach,
    filter_results_for_coach,
    grant_state,
    strip_video_fields,
    validate_grant_window,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _grant(
    gid: int = 1,
    *,
    starts: datetime = NOW - DAY,
    expires: datetime = NOW + DAY,
    revoked: datetime | None = None,
    all_sessions: bool = False,
    allow_video: bool = False,
    session_ids: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "id": gid,
        "starts_at": _iso(starts),
        "expires_at": _iso(expires),
        "revoked_at": _iso(revoked) if revoked else None,
        "all_sessions": int(all_sessions),
        "allow_video": int(allow_video),
        "session_ids": session_ids or [],
    }


# ---------------------------------------------------------------------------
# §1 Grant lifecycle — each transition is a test case
# ---------------------------------------------------------------------------


class TestGrantState:
    def test_scheduled_before_start(self) -> None:
        assert grant_state(_grant(starts=NOW + DAY, expires=NOW + 2 * DAY), NOW) == "scheduled"

    def test_active_at_start_boundary(self) -> None:
        assert grant_state(_grant(starts=NOW, expires=NOW + DAY), NOW) == "active"

    def test_active_inside_window(self) -> None:
        assert grant_state(_grant(), NOW) == "active"

    def test_expired_at_expiry_boundary(self) -> None:
        assert grant_state(_grant(starts=NOW - 2 * DAY, expires=NOW), NOW) == "expired"

    def test_revoked_while_active(self) -> None:
        assert grant_state(_grant(revoked=NOW - timedelta(minutes=1)), NOW) == "revoked"

    def test_revoked_while_scheduled(self) -> None:
        g = _grant(starts=NOW + DAY, expires=NOW + 2 * DAY, revoked=NOW - DAY)
        assert grant_state(g, NOW) == "revoked"

    def test_revoked_wins_over_expired(self) -> None:
        g = _grant(starts=NOW - 3 * DAY, expires=NOW - DAY, revoked=NOW - 2 * DAY)
        assert grant_state(g, NOW) == "revoked"


class TestGrantWindow:
    def test_valid_window(self) -> None:
        validate_grant_window(NOW, NOW + 30 * DAY, NOW)

    def test_expiry_must_be_after_start(self) -> None:
        with pytest.raises(ValueError, match="after"):
            validate_grant_window(NOW + 2 * DAY, NOW + DAY, NOW)

    def test_expiry_must_be_in_future(self) -> None:
        with pytest.raises(ValueError, match="future"):
            validate_grant_window(NOW - 10 * DAY, NOW - DAY, NOW)

    def test_no_perpetual_access(self) -> None:
        validate_grant_window(NOW, NOW + 366 * DAY, NOW)
        with pytest.raises(ValueError, match="366"):
            validate_grant_window(NOW, NOW + 367 * DAY, NOW)


# ---------------------------------------------------------------------------
# Scope — union of active grants
# ---------------------------------------------------------------------------


class TestBuildScope:
    def test_empty_when_no_grants(self) -> None:
        s = build_scope([], NOW)
        assert s.grant_ids == ()
        assert not s.can_read(1)

    def test_selected_sessions(self) -> None:
        s = build_scope([_grant(session_ids=[3, 4])], NOW)
        assert s.can_read(3) and s.can_read(4)
        assert not s.can_read(5)

    def test_all_sessions(self) -> None:
        s = build_scope([_grant(all_sessions=True)], NOW)
        assert s.can_read(999)

    def test_inactive_grants_ignored(self) -> None:
        grants = [
            _grant(1, session_ids=[1], starts=NOW + DAY, expires=NOW + 2 * DAY),
            _grant(2, session_ids=[2], starts=NOW - 2 * DAY, expires=NOW - DAY),
            _grant(3, session_ids=[3], revoked=NOW - DAY),
            _grant(4, session_ids=[4]),
        ]
        s = build_scope(grants, NOW)
        assert s.grant_ids == (4,)
        assert [s.can_read(i) for i in (1, 2, 3, 4)] == [False, False, False, True]

    def test_video_requires_read_and_allow_video(self) -> None:
        s = build_scope(
            [_grant(session_ids=[1], allow_video=True), _grant(2, session_ids=[2])], NOW
        )
        assert s.can_video(1)
        assert not s.can_video(2)
        assert not s.can_video(3)

    def test_video_is_or_across_grants_covering_session(self) -> None:
        s = build_scope(
            [_grant(1, session_ids=[7]), _grant(2, session_ids=[7], allow_video=True)], NOW
        )
        assert s.can_video(7)

    def test_all_sessions_video(self) -> None:
        s = build_scope([_grant(all_sessions=True, allow_video=True)], NOW)
        assert s.can_video(123)

    def test_all_sessions_without_video_plus_selected_with_video(self) -> None:
        s = build_scope(
            [_grant(1, all_sessions=True), _grant(2, session_ids=[5], allow_video=True)], NOW
        )
        assert s.can_read(6) and not s.can_video(6)
        assert s.can_video(5)

    def test_scope_is_frozen(self) -> None:
        s = build_scope([_grant(session_ids=[1])], NOW)
        assert isinstance(s, CoachScope)
        with pytest.raises(AttributeError):
            s.all_sessions = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §3 Middleware allowlist (deny by default)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/coach",
        "/api/coach/sessions",
        "/api/me",
        "/api/coach/timezone",
        "/session/12",
        "/session/12/2026-race-slug",
        "/session/12/compare",
        "/compare",
        "/api/sessions/12/detail",
        "/api/sessions/12/track",
        "/api/sessions/12/replay",
        "/api/sessions/12/polar",
        "/api/sessions/12/wind-field",
        "/api/sessions/12/wind-timeseries",
        "/api/sessions/12/course-overlay",
        "/api/sessions/12/race-start-overlay",
        "/api/sessions/12/vakaros-overlay",
        "/api/sessions/12/maneuvers",
        "/api/sessions/12/maneuvers/compare",
        "/api/sessions/12/results",
        "/api/sessions/12/videos",
        "/api/maneuvers/compare",
        "/api/maneuvers/overlay",
    ],
)
def test_allowlisted_get_paths(path: str) -> None:
    assert coach_path_allowed("GET", path)
    assert coach_path_allowed("HEAD", path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/analysis/results/12",
        "/api/analysis/models",
        "/history",
        "/maneuvers",
        "/analysis",
        "/admin/users",
        "/admin/coaches",
        "/api/sessions",
        "/api/sessions/12/summary",
        "/api/sessions/12/maneuvers.csv",
        "/api/sessions/12/sails",
        "/api/sessions/12/moments",
        "/api/sessions/12/notes",
        "/api/sessions/12/llm/qa",
        "/api/sessions/12/videos/redirect",
        "/api/races/12/crew",
        "/api/races/12/export.csv",
        "/api/audio/3/stream",
        "/api/crew/users",
        "/api/users/names",
        "/api/state",
        "/api/tags",
        "/api/notifications/count",
        "/ws/live",
        "/session/12/compare/extra",
        "/api/sessions/abc/detail",
        "/api/sessions/12/detail/extra",
    ],
)
def test_denied_paths(path: str) -> None:
    assert not coach_path_allowed("GET", path)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_denied_even_on_allowlisted_paths(method: str) -> None:
    assert not coach_path_allowed(method, "/api/sessions/12/detail")
    assert not coach_path_allowed(method, "/api/sessions/12/results")


# ---------------------------------------------------------------------------
# §4 Field filtering (allowlist)
# ---------------------------------------------------------------------------

_DETAIL = {
    "id": 5,
    "type": "race",
    "name": "R1",
    "event": "Jfest",
    "race_num": 1,
    "date": "2026-09-26",
    "start_utc": "2026-09-26T17:00:00+00:00",
    "end_utc": "2026-09-26T18:00:00+00:00",
    "duration_s": 3600.0,
    "has_track": True,
    "has_wind_field": False,
    "first_video_url": "https://youtu.be/x",
    "has_audio": True,
    "audio_session_id": 9,
    "audio_start_utc": "x",
    "audio_channels": 2,
    "audio_siblings": [{"stream_url": "/api/audio/9/stream"}],
    "use_streaming_audio": False,
    "debrief_audio": {"audio_session_id": 10},
    "peer_fingerprint": "abc",
    "shared_name": "secret",
    "match_group_id": "g",
    "match_status": "candidate",
    "some_future_field": "hidden by default",
}


def test_detail_filter_without_video() -> None:
    out = filter_detail_for_coach(_DETAIL, video=False)
    assert out == {
        "id": 5,
        "type": "race",
        "name": "R1",
        "event": "Jfest",
        "race_num": 1,
        "date": "2026-09-26",
        "start_utc": "2026-09-26T17:00:00+00:00",
        "end_utc": "2026-09-26T18:00:00+00:00",
        "duration_s": 3600.0,
        "has_track": True,
        "has_wind_field": False,
    }


def test_detail_filter_with_video() -> None:
    out = filter_detail_for_coach(_DETAIL, video=True)
    assert out["first_video_url"] == "https://youtu.be/x"
    assert "has_audio" not in out
    assert "some_future_field" not in out


def test_strip_video_fields_nested() -> None:
    payload = {
        "maneuvers": [
            {"id": 1, "youtube_url": "u", "video_offset_s": 3.0, "bsp": 6.1},
            {"id": 2, "video_id": "v", "nested": {"youtube_url": "u2"}},
        ],
        "video_sync": {"video_id": "v"},
        "videos": [{"video_id": "v"}],
    }
    assert strip_video_fields(payload) == {
        "maneuvers": [{"id": 1, "bsp": 6.1}, {"id": 2, "nested": {}}],
    }


def test_results_filter_is_an_allowlist() -> None:
    row = {
        "id": 1,
        "race_id": 5,
        "place": 1,
        "boat_id": 2,
        "sail_number": "USA 123",
        "boat_name": "Corvo",
        "boat_class": "J/105",
        "finish_time": "x",
        "dnf": False,
        "dns": False,
        "notes": "private",
        "created_at": "y",
        "points": 1.0,
        "status_code": None,
        "elapsed_seconds": 100,
        "corrected_seconds": 99,
        "imported": True,
        "some_future_field": "hidden",
    }
    (out,) = filter_results_for_coach([row])
    assert "notes" not in out
    assert "some_future_field" not in out
    assert "created_at" not in out
    assert out["place"] == 1 and out["boat_name"] == "Corvo" and out["points"] == 1.0


class TestPeerRacesExcluded:
    """Policy: per-boat opt-in. Another co-op boat's session stored locally
    (peer_fingerprint set) is never ours to share, even under all-sessions."""

    def test_all_sessions_excludes_peer_races(self) -> None:
        s = build_scope([_grant(all_sessions=True, allow_video=True)], NOW, excluded={9})
        assert s.can_read(1)
        assert not s.can_read(9)
        assert not s.can_video(9)

    def test_selected_excludes_peer_races(self) -> None:
        s = build_scope([_grant(session_ids=[1, 9], allow_video=True)], NOW, excluded={9})
        assert s.can_read(1) and not s.can_read(9)
