"""Cross-session maneuver compare — best/median selection (#741).

A coaching tool: given a pool of enriched maneuvers (the output of
:func:`helmlog.analysis.maneuvers.enrich_maneuvers_for_ids`), pick the
3 best by some success metric and the 3 closest to the median, so the
coach can compare top performers vs typical performers side-by-side.

All current candidate metrics (``distance_loss_m``, ``time_to_recover_s``,
``vmg_loss_kts``, ...) follow a smaller-is-better convention; ``pick_best``
sorts ascending. Items missing the chosen metric are excluded from ranking.
"""

from __future__ import annotations

import json
import statistics
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from helmlog.storage import Storage

# Filterable maneuver-type values exposed by the API. Internally, ``rounding``
# maneuvers carry a ``details.rounding_kind`` of ``"weather"`` / ``"leeward"``
# (or absent for legacy rows), so the two split out at the filter layer.
ROUNDING_TYPES = {"weather_rounding", "leeward_rounding"}


def _recency_key(m: dict[str, Any]) -> str:
    """Sort key for tie-breaking — newer session_start_utc wins.

    ISO-8601 strings sort lexicographically in chronological order, so a
    plain string compare suffices. Missing values sort earliest.
    """
    return str(m.get("session_start_utc") or "")


def _matches_type(m: dict[str, Any], maneuver_type: str) -> bool:
    if maneuver_type in ROUNDING_TYPES:
        if m.get("type") != "rounding":
            return False
        kind = (m.get("details") or {}).get("rounding_kind")
        wanted = "weather" if maneuver_type == "weather_rounding" else "leeward"
        return kind == wanted
    return m.get("type") == maneuver_type


def filter_pool(
    items: list[dict[str, Any]],
    *,
    maneuver_type: str | None = None,
    tws_min: float | None = None,
    tws_max: float | None = None,
    direction: str | None = None,
    gun_by_session: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Filter an enriched-maneuver pool by type, entry-TWS, direction, and
    pre/post-start cut-off.

    TWS range is inclusive on both ends. Items missing ``entry_tws`` are
    dropped only when a TWS range is supplied.

    ``direction`` follows the convention in ``routes/sessions.py``:
    ``is_ps = turn_angle_deg < 0`` (so PS matches negative turn, SP matches
    non-negative). Items without a turn angle are dropped when set.

    ``gun_by_session`` enables the post-start cut: ``{session_id: gun_iso}``;
    items whose ``ts`` is before their session's gun are dropped. Sessions
    not in the dict pass through (no gun = no cut).
    """
    out = []
    for m in items:
        if maneuver_type is not None and not _matches_type(m, maneuver_type):
            continue
        if tws_min is not None or tws_max is not None:
            tws = m.get("entry_tws")
            if tws is None:
                continue
            if tws_min is not None and tws < tws_min:
                continue
            if tws_max is not None and tws > tws_max:
                continue
        if direction is not None:
            ang = m.get("turn_angle_deg")
            if ang is None:
                continue
            is_ps = ang < 0
            if direction == "PS" and not is_ps:
                continue
            if direction == "SP" and is_ps:
                continue
        if gun_by_session:
            sid = m.get("session_id")
            gun = gun_by_session.get(sid) if isinstance(sid, int) else None
            if gun and str(m.get("ts") or "") < gun:
                continue
        out.append(m)
    return out


async def load_gun_times(
    storage: Storage,
    session_ids: list[int],
) -> dict[int, str]:
    """Compute each session's effective race gun (latest Vakaros race_start
    event inside the session window, falling back to ``start_utc``).

    Mirrors the query used by ``GET /api/maneuvers`` in routes/sessions.py
    so the post-start filter on this endpoint and the existing maneuvers
    browser cut at the same instant.
    """
    if not session_ids:
        return {}
    db = storage._read_conn()
    placeholders = ",".join("?" * len(session_ids))
    cursor = await db.execute(
        f"""
        SELECT r.id AS session_id,
               COALESCE(
                 (SELECT MAX(vre.ts)
                    FROM vakaros_race_events vre
                   WHERE vre.session_id = r.vakaros_session_id
                     AND vre.event_type = 'race_start'
                     AND vre.ts BETWEEN r.start_utc
                                    AND COALESCE(r.end_utc, r.start_utc)),
                 r.start_utc
               ) AS gun_utc
          FROM races r
         WHERE r.id IN ({placeholders})
        """,
        session_ids,
    )
    rows = await cursor.fetchall()
    return {int(r[0]): str(r[1]) for r in rows if r[1] is not None}


def pick_best(
    items: list[dict[str, Any]],
    *,
    metric: str,
    n: int = 3,
) -> list[dict[str, Any]]:
    """Return up to ``n`` maneuvers with the smallest ``metric`` values.

    Items where ``metric`` is None are excluded. Ties are broken by session
    recency (most recent first), so a current-form best beats an old best.
    """
    rankable = [m for m in items if m.get(metric) is not None]
    rankable.sort(key=lambda m: (float(m[metric]), _negate(_recency_key(m))))
    return rankable[:n]


def pick_median(
    items: list[dict[str, Any]],
    *,
    metric: str,
    n: int = 3,
    exclude_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``n`` maneuvers whose ``metric`` is closest to the median.

    The median is computed from the ``metric`` values of items not excluded
    and not None — so the median represents typical performance of the pool
    that's available to fill the median slots. Ties on distance-to-median
    are broken by session recency (most recent first).

    ``exclude_ids`` lets the caller hand in the ids already chosen by
    :func:`pick_best` so the best and median sets don't overlap.
    """
    excluded = exclude_ids or set()
    rankable = [m for m in items if m.get(metric) is not None and m.get("id") not in excluded]
    if not rankable:
        return []

    values = [float(m[metric]) for m in rankable]
    median = statistics.median(values)

    rankable.sort(key=lambda m: (abs(float(m[metric]) - median), _negate(_recency_key(m))))
    return rankable[:n]


def assemble_compare_set(
    items: list[dict[str, Any]],
    *,
    metric: str,
    n: int = 3,
) -> dict[str, Any]:
    """Top-level: split a filtered pool into ``best`` and ``median`` cells.

    The two sets are disjoint — anything chosen as best is excluded from the
    median pool, so a coach reviewing the page never sees the same maneuver
    in two cells. Returns the metric's median value (across the full pool)
    so the UI can label the median row with the actual number coaches are
    comparing against.
    """
    rankable_values = [float(m[metric]) for m in items if m.get(metric) is not None]
    median_value: float | None = statistics.median(rankable_values) if rankable_values else None

    best = pick_best(items, metric=metric, n=n)
    best_ids = {m["id"] for m in best if m.get("id") is not None}
    median = pick_median(items, metric=metric, n=n, exclude_ids=best_ids)

    return {
        "best": best,
        "median": median,
        "median_value": median_value,
        "pool_size": len(items),
    }


async def list_maneuver_pairs(
    storage: Storage,
    *,
    maneuver_type: str | None = None,
) -> list[tuple[int, int]]:
    """Return all ``(session_id, maneuver_id)`` pairs across the boat's DB.

    Filtering by tack / gybe is pushed to SQL via the ``type`` column. The
    rounding split (``weather_rounding`` vs ``leeward_rounding``) reads the
    ``details`` JSON in-Python because SQLite has no first-class JSON path
    operator on this schema. Roundings without a ``rounding_kind`` are
    skipped under the rounding filters — they cannot be classified.
    """
    db = storage._read_conn()
    if maneuver_type in ROUNDING_TYPES:
        cursor = await db.execute(
            "SELECT session_id, id, details FROM maneuvers WHERE type = 'rounding'"
        )
        rows = await cursor.fetchall()
        wanted = "weather" if maneuver_type == "weather_rounding" else "leeward"
        out: list[tuple[int, int]] = []
        for r in rows:
            details_raw = r[2]
            if not details_raw:
                continue
            try:
                details = json.loads(details_raw)
            except (TypeError, ValueError):
                continue
            if details.get("rounding_kind") == wanted:
                out.append((int(r[0]), int(r[1])))
        return out

    if maneuver_type is None:
        cursor = await db.execute("SELECT session_id, id FROM maneuvers")
    else:
        cursor = await db.execute(
            "SELECT session_id, id FROM maneuvers WHERE type = ?",
            (maneuver_type,),
        )
    rows = await cursor.fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def _negate(s: str) -> tuple[int, ...]:
    """Sort-key adapter: invert a string's ordering so newer (lex-greater)
    sorts first when used as a secondary tiebreaker on ascending sorts.

    ISO-8601 timestamps as plain strings sort old → new ascending. We want
    new → old as a *tie-break under* an ascending primary key, so we map
    each character to its negated codepoint. Cheap and total over any str.
    """
    return tuple(-ord(c) for c in s)
