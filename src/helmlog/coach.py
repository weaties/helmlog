"""Coach access: grant lifecycle, scope, path allowlist, field filtering (#848).

A *coach grant* gives a coach time-limited, read-only access to selected
races (spec: issue #848). This module is pure — no I/O — so the policy can be
tested exhaustively; storage, auth and routes call into it.

Policy source: docs/data-licensing.md (data-type matrix, "Coach and combined
datasets"). Coaches get instrument data, derived metrics and race results for
granted races, YouTube video only when the grant enables it, and never crew,
emails, biometrics, audio, transcripts, notes, sails or bulk exports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

GrantState = Literal["scheduled", "active", "expired", "revoked"]

# Policy: no perpetual coach access — grants are renewed each season.
MAX_GRANT_DAYS = 366


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def grant_state(grant: Mapping[str, Any], now: datetime) -> GrantState:
    """Compute a grant's lifecycle state (spec §1). Revocation wins."""
    if grant.get("revoked_at"):
        return "revoked"
    if now < _ts(grant["starts_at"]):
        return "scheduled"
    if now >= _ts(grant["expires_at"]):
        return "expired"
    return "active"


def validate_grant_window(starts_at: datetime, expires_at: datetime, now: datetime) -> None:
    """Raise ValueError unless the window is a valid, time-limited grant."""
    if expires_at <= now:
        raise ValueError("expires_at must be in the future")
    if expires_at <= starts_at:
        raise ValueError("expires_at must be after starts_at")
    if expires_at - starts_at > timedelta(days=MAX_GRANT_DAYS):
        raise ValueError(f"grants may last at most {MAX_GRANT_DAYS} days")


@dataclass(frozen=True)
class CoachScope:
    """The union of a coach principal's active grants."""

    grant_ids: tuple[int, ...] = ()
    all_sessions: bool = False
    session_ids: frozenset[int] = frozenset()
    video_all: bool = False
    video_session_ids: frozenset[int] = frozenset()
    # Sessions this boat may never share: other co-op boats' races stored
    # locally (peer_fingerprint set). Policy: per-boat opt-in only.
    excluded_session_ids: frozenset[int] = frozenset()

    def can_read(self, session_id: int) -> bool:
        if session_id in self.excluded_session_ids:
            return False
        return self.all_sessions or session_id in self.session_ids

    def can_video(self, session_id: int) -> bool:
        if not self.can_read(session_id):
            return False
        return self.video_all or session_id in self.video_session_ids


def build_scope(
    grants: Iterable[Mapping[str, Any]],
    now: datetime,
    *,
    excluded: Iterable[int] = (),
) -> CoachScope:
    """Union the *active* grants into a scope; other states contribute nothing.

    ``excluded`` sessions (other boats' races) are never readable.
    """
    ids: list[int] = []
    all_sessions = False
    video_all = False
    sessions: set[int] = set()
    video_sessions: set[int] = set()
    for g in grants:
        if grant_state(g, now) != "active":
            continue
        ids.append(int(g["id"]))
        video = bool(g["allow_video"])
        if g["all_sessions"]:
            all_sessions = True
            video_all = video_all or video
        else:
            sid_set = {int(s) for s in g["session_ids"]}
            sessions |= sid_set
            if video:
                video_sessions |= sid_set
    return CoachScope(
        grant_ids=tuple(sorted(ids)),
        all_sessions=all_sessions,
        session_ids=frozenset(sessions),
        video_all=video_all,
        video_session_ids=frozenset(video_sessions),
        excluded_session_ids=frozenset(int(x) for x in excluded),
    )


# ---------------------------------------------------------------------------
# Middleware allowlist (spec §3, layer 1). Deny by default: a coach principal
# may only GET/HEAD these exact path shapes. Per-session checks happen in the
# routes (layer 2). /api/analysis/* is deliberately absent: plugin output
# includes sail_vmg (sail selection), which the policy keeps from coaches.
# ---------------------------------------------------------------------------

_SESSION_API_LEAVES = (
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
    "maneuvers/compare",
    "results",
    "videos",
)

_COACH_PATHS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"/coach",
        r"/api/coach/sessions",
        r"/api/coach/timezone",
        r"/api/me",
        r"/session/\d+",
        r"/session/\d+/compare",
        r"/session/\d+/[^/]+",
        r"/compare",
        r"/api/sessions/\d+/(?:" + "|".join(re.escape(x) for x in _SESSION_API_LEAVES) + r")",
        r"/api/maneuvers/compare",
        r"/api/maneuvers/overlay",
    )
)


def coach_path_allowed(method: str, path: str) -> bool:
    """True if a coach principal may make this request at all."""
    if method not in ("GET", "HEAD"):
        return False
    return any(p.fullmatch(path) for p in _COACH_PATHS)


# ---------------------------------------------------------------------------
# Field filtering (spec §4). Allowlists, so fields added later stay hidden
# from coaches until someone decides they're coach-safe.
# ---------------------------------------------------------------------------

_DETAIL_FIELDS = (
    "id",
    "type",
    "name",
    "event",
    "race_num",
    "date",
    "start_utc",
    "end_utc",
    "duration_s",
    "has_track",
    "has_wind_field",
)
_DETAIL_VIDEO_FIELDS = ("first_video_url",)

_RESULT_FIELDS = (
    "id",
    "race_id",
    "place",
    "boat_id",
    "sail_number",
    "boat_name",
    "boat_class",
    "finish_time",
    "dnf",
    "dns",
    "points",
    "status_code",
    "elapsed_seconds",
    "corrected_seconds",
    "imported",
)

VIDEO_KEYS = frozenset(
    {"youtube_url", "video_id", "video_offset_s", "video_sync", "video_sync_by_session", "videos"}
)


def filter_detail_for_coach(detail: Mapping[str, Any], *, video: bool) -> dict[str, Any]:
    fields = _DETAIL_FIELDS + (_DETAIL_VIDEO_FIELDS if video else ())
    return {k: detail[k] for k in fields if k in detail}


def filter_results_for_coach(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: r[k] for k in _RESULT_FIELDS if k in r} for r in rows]


def strip_video_fields(value: Any) -> Any:  # noqa: ANN401 — arbitrary JSON
    """Recursively drop YouTube link/sync keys from a JSON-shaped value."""
    if isinstance(value, dict):
        return {k: strip_video_fields(v) for k, v in value.items() if k not in VIDEO_KEYS}
    if isinstance(value, list):
        return [strip_video_fields(v) for v in value]
    return value
