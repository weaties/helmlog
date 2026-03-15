"""Session matching — proximity-based session pairing across co-op boats (#281).

Matches sessions from different boats in a co-op based on time overlap and
geographic proximity. The lifecycle: Unmatched -> Candidate -> Matched -> Named.

Key rules:
- Proximity scan blocked while ANY session in the pair is embargoed
- Time overlap window: +/-15 min (configurable)
- Geographic radius: 2 nautical miles (configurable)
- Minimum 10 track points per session
- Quorum: 2 boats to confirm a match
- Candidate expiry: 48 hours after embargo lift (configurable)
"""

from __future__ import annotations

import contextlib
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_TIME_OVERLAP_MINUTES = 15
DEFAULT_RADIUS_NM = 2.0
DEFAULT_MIN_TRACK_POINTS = 10
DEFAULT_QUORUM = 2
DEFAULT_CANDIDATE_EXPIRY_HOURS = 48

# Earth radius in nautical miles (for haversine)
_EARTH_RADIUS_NM = 3440.065


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchCandidate:
    """A potential session match between two boats."""

    match_group_id: str
    local_session_id: int
    peer_session_id: int
    peer_fingerprint: str
    distance_nm: float
    time_overlap_minutes: float
    status: str  # candidate, confirmed, rejected, expired


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in nautical miles between two points."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))

    return _EARTH_RADIUS_NM * c


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


async def compute_session_centroid(
    storage: Storage,
    session_id: int,
) -> tuple[float, float]:
    """Compute the average lat/lon from GPS positions for a session.

    Returns (0.0, 0.0) if no positions exist.
    """
    db = storage._conn()

    # Get session time range
    cur = await db.execute(
        "SELECT start_utc, end_utc FROM races WHERE id = ?",
        (session_id,),
    )
    race = await cur.fetchone()
    if not race or not race["start_utc"] or not race["end_utc"]:
        return 0.0, 0.0

    start = race["start_utc"]
    end = race["end_utc"]

    cur = await db.execute(
        "SELECT AVG(latitude_deg) AS avg_lat, AVG(longitude_deg) AS avg_lon,"
        " COUNT(*) AS cnt"
        " FROM positions WHERE ts >= ? AND ts <= ?",
        (start, end),
    )
    row = await cur.fetchone()
    if not row or row["cnt"] == 0:
        return 0.0, 0.0

    return float(row["avg_lat"]), float(row["avg_lon"])


async def _count_session_positions(
    storage: Storage,
    session_id: int,
) -> int:
    """Count GPS positions within a session's time range."""
    db = storage._conn()
    cur = await db.execute(
        "SELECT start_utc, end_utc FROM races WHERE id = ?",
        (session_id,),
    )
    race = await cur.fetchone()
    if not race or not race["start_utc"] or not race["end_utc"]:
        return 0

    cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM positions WHERE ts >= ? AND ts <= ?",
        (race["start_utc"], race["end_utc"]),
    )
    row = await cur.fetchone()
    return int(row["cnt"]) if row else 0


def _time_overlap_minutes(
    start1: datetime,
    end1: datetime,
    start2: datetime,
    end2: datetime,
    *,
    window_minutes: int = DEFAULT_TIME_OVERLAP_MINUTES,
) -> float:
    """Compute time overlap in minutes between two sessions with a tolerance window.

    Extends each session's start/end by window_minutes before checking overlap.
    Returns 0 if no overlap.
    """
    window = timedelta(minutes=window_minutes)
    effective_start1 = start1 - window
    effective_end1 = end1 + window
    effective_start2 = start2 - window
    effective_end2 = end2 + window

    overlap_start = max(effective_start1, effective_start2)
    overlap_end = min(effective_end1, effective_end2)

    if overlap_start >= overlap_end:
        return 0.0

    return (overlap_end - overlap_start).total_seconds() / 60.0


async def _is_session_embargoed(
    storage: Storage,
    session_id: int,
    co_op_id: str,
) -> bool:
    """Check if a session is currently under embargo for a co-op."""
    sharing = await storage.get_session_sharing(session_id)
    now = datetime.now(UTC)
    for s in sharing:
        if s["co_op_id"] == co_op_id and s.get("embargo_until"):
            try:
                embargo = datetime.fromisoformat(s["embargo_until"])
                if embargo > now:
                    return True
            except ValueError:
                pass
    return False


async def find_proximity_candidates(
    storage: Storage,
    co_op_id: str,
    *,
    peer_sessions: list[dict[str, Any]],
    radius_nm: float = DEFAULT_RADIUS_NM,
    time_window_minutes: int = DEFAULT_TIME_OVERLAP_MINUTES,
    min_track_points: int = DEFAULT_MIN_TRACK_POINTS,
) -> list[MatchCandidate]:
    """Scan local sessions and match against peer sessions by proximity.

    Args:
        storage: Local storage instance
        co_op_id: Co-op to match within
        peer_sessions: List of peer session dicts with keys:
            session_id, name, start_utc, end_utc, centroid_lat, centroid_lon,
            fingerprint, status
        radius_nm: Maximum distance in nautical miles for a match
        time_window_minutes: Tolerance window for time overlap
        min_track_points: Minimum GPS positions required per session

    Returns:
        List of MatchCandidate objects for sessions that match.
    """
    db = storage._conn()

    # Get local sessions shared with this co-op that are not already matched
    cur = await db.execute(
        "SELECT r.id, r.name, r.start_utc, r.end_utc"
        " FROM races r"
        " JOIN session_sharing ss ON r.id = ss.session_id AND ss.co_op_id = ?"
        " WHERE r.end_utc IS NOT NULL"
        "   AND (r.match_group_id IS NULL OR r.match_confirmed = 0)",
        (co_op_id,),
    )
    local_sessions = [dict(r) for r in await cur.fetchall()]

    candidates: list[MatchCandidate] = []

    for local in local_sessions:
        local_id = local["id"]

        # Check embargo
        if await _is_session_embargoed(storage, local_id, co_op_id):
            logger.debug("Skipping embargoed session {} for matching", local_id)
            continue

        # Check minimum track points
        point_count = await _count_session_positions(storage, local_id)
        if point_count < min_track_points:
            logger.debug(
                "Skipping session {} — only {} positions (need {})",
                local_id,
                point_count,
                min_track_points,
            )
            continue

        # Compute local centroid
        local_lat, local_lon = await compute_session_centroid(storage, local_id)
        if local_lat == 0.0 and local_lon == 0.0:
            continue

        local_start = datetime.fromisoformat(local["start_utc"])
        local_end = datetime.fromisoformat(local["end_utc"])

        for peer in peer_sessions:
            if peer.get("status") == "embargoed":
                continue

            peer_start_str = peer.get("start_utc", "")
            peer_end_str = peer.get("end_utc", "")
            if not peer_start_str or not peer_end_str:
                continue

            peer_start = datetime.fromisoformat(peer_start_str)
            peer_end = datetime.fromisoformat(peer_end_str)

            # Check time overlap
            overlap = _time_overlap_minutes(
                local_start,
                local_end,
                peer_start,
                peer_end,
                window_minutes=time_window_minutes,
            )
            if overlap <= 0:
                continue

            # Check geographic proximity
            peer_lat = peer.get("centroid_lat", 0.0)
            peer_lon = peer.get("centroid_lon", 0.0)
            if peer_lat == 0.0 and peer_lon == 0.0:
                continue

            distance = haversine_nm(local_lat, local_lon, peer_lat, peer_lon)
            if distance > radius_nm:
                continue

            match_id = str(uuid.uuid4())
            candidates.append(
                MatchCandidate(
                    match_group_id=match_id,
                    local_session_id=local_id,
                    peer_session_id=peer["session_id"],
                    peer_fingerprint=peer.get("fingerprint", ""),
                    distance_nm=round(distance, 3),
                    time_overlap_minutes=round(overlap, 1),
                    status="candidate",
                )
            )

    return candidates


async def propose_match(
    storage: Storage,
    *,
    local_session_id: int,
    peer_fingerprint: str,
    peer_session_id: int,
    centroid_lat: float,
    centroid_lon: float,
    start_utc: str,
    end_utc: str,
    expiry_hours: int = DEFAULT_CANDIDATE_EXPIRY_HOURS,
) -> str:
    """Create a match proposal and store it. Returns the match_group_id (UUID)."""
    match_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    expires = now + timedelta(hours=expiry_hours)

    db = storage._conn()

    # Insert proposal
    await db.execute(
        "INSERT INTO session_match_proposals"
        " (match_group_id, proposer_fingerprint, local_session_id, peer_session_id,"
        "  centroid_lat, centroid_lon, start_utc, end_utc, status, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
        (
            match_id,
            peer_fingerprint,
            local_session_id,
            peer_session_id,
            centroid_lat,
            centroid_lon,
            start_utc,
            end_utc,
            now.isoformat(),
            expires.isoformat(),
        ),
    )

    # Set match_group_id on the local session
    await db.execute(
        "UPDATE races SET match_group_id = ?, match_confirmed = 0 WHERE id = ?",
        (match_id, local_session_id),
    )
    await db.commit()

    logger.info(
        "Match proposed: {} (session {} <-> peer session {} from {})",
        match_id,
        local_session_id,
        peer_session_id,
        peer_fingerprint,
    )
    return match_id


async def confirm_match(
    storage: Storage,
    match_group_id: str,
    boat_fingerprint: str,
    *,
    quorum: int = DEFAULT_QUORUM,
) -> bool:
    """Record a confirmation for a match. Returns True if successful.

    When quorum is reached, sets match_confirmed=1 on the associated session.
    """
    db = storage._conn()
    now = datetime.now(UTC).isoformat()

    # Check proposal exists and is still candidate
    cur = await db.execute(
        "SELECT status, local_session_id FROM session_match_proposals WHERE match_group_id = ?",
        (match_group_id,),
    )
    row = await cur.fetchone()
    if not row:
        logger.warning("Confirm failed: match {} not found", match_group_id)
        return False

    status = dict(row)["status"]
    if status not in ("candidate", "confirmed"):
        logger.warning("Confirm failed: match {} has status {}", match_group_id, status)
        return False

    # Record confirmation (ignore duplicate)
    with contextlib.suppress(Exception):
        await db.execute(
            "INSERT INTO session_match_confirmations"
            " (match_group_id, fingerprint, confirmed_at)"
            " VALUES (?, ?, ?)",
            (match_group_id, boat_fingerprint, now),
        )

    # Count confirmations
    cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM session_match_confirmations WHERE match_group_id = ?",
        (match_group_id,),
    )
    count_row = await cur.fetchone()
    count = int(count_row["cnt"]) if count_row else 0

    if count >= quorum:
        # Update proposal status
        await db.execute(
            "UPDATE session_match_proposals SET status = 'confirmed' WHERE match_group_id = ?",
            (match_group_id,),
        )
        # Update session
        local_id = dict(row)["local_session_id"]
        if local_id:
            await db.execute(
                "UPDATE races SET match_confirmed = 1 WHERE id = ?",
                (local_id,),
            )
        logger.info("Match {} confirmed (quorum reached)", match_group_id)

    await db.commit()
    return True


async def reject_match(
    storage: Storage,
    match_group_id: str,
    boat_fingerprint: str,
) -> bool:
    """Reject a match proposal. Returns True if successful."""
    db = storage._conn()

    cur = await db.execute(
        "SELECT status, local_session_id FROM session_match_proposals WHERE match_group_id = ?",
        (match_group_id,),
    )
    row = await cur.fetchone()
    if not row:
        return False

    row_dict = dict(row)
    if row_dict["status"] not in ("candidate", "confirmed"):
        return False

    await db.execute(
        "UPDATE session_match_proposals SET status = 'rejected' WHERE match_group_id = ?",
        (match_group_id,),
    )

    # Clear match from session
    local_id = row_dict.get("local_session_id")
    if local_id:
        await db.execute(
            "UPDATE races SET match_group_id = NULL, match_confirmed = 0 WHERE id = ?",
            (local_id,),
        )

    await db.commit()
    logger.info("Match {} rejected by {}", match_group_id, boat_fingerprint)
    return True


def synthesize_name(local_names: list[str]) -> str:
    """Synthesize a shared session name from local names.

    Rules:
    - 0 names: return empty string (waiting for names)
    - 1 name: use as-is
    - 2+ identical names: use that name
    - 2+ different names: pick the longest (LLM fallback not implemented yet)
    """
    if not local_names:
        return ""

    if len(local_names) == 1:
        return local_names[0]

    # If all names are the same, use that
    if len(set(local_names)) == 1:
        return local_names[0]

    # Fallback: pick the longest name
    return max(local_names, key=len)


async def set_shared_name(
    storage: Storage,
    match_group_id: str,
    shared_name: str,
    proposed_by: str,
) -> bool:
    """Set or update the shared name for a match group.

    Updates session_match_names table and races.shared_name.
    Does NOT overwrite races.name (the local name).
    """
    db = storage._conn()
    now = datetime.now(UTC).isoformat()

    # Verify the match exists and is confirmed
    cur = await db.execute(
        "SELECT status, local_session_id FROM session_match_proposals WHERE match_group_id = ?",
        (match_group_id,),
    )
    row = await cur.fetchone()
    if not row:
        return False

    if dict(row)["status"] != "confirmed":
        return False

    # Upsert shared name record
    await db.execute(
        "INSERT INTO session_match_names"
        " (match_group_id, shared_name, proposed_by, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(match_group_id) DO UPDATE SET"
        "   shared_name = excluded.shared_name,"
        "   proposed_by = excluded.proposed_by,"
        "   updated_at = excluded.updated_at",
        (match_group_id, shared_name, proposed_by, now),
    )

    # Update the races.shared_name for the local session
    local_id = dict(row).get("local_session_id")
    if local_id:
        await db.execute(
            "UPDATE races SET shared_name = ? WHERE id = ?",
            (shared_name, local_id),
        )

    await db.commit()
    logger.info("Shared name set for match {}: {}", match_group_id, shared_name)
    return True


async def expire_stale_candidates(storage: Storage) -> int:
    """Mark expired candidates as 'expired'. Returns the count of expired proposals."""
    db = storage._conn()
    now = datetime.now(UTC).isoformat()

    cur = await db.execute(
        "UPDATE session_match_proposals SET status = 'expired'"
        " WHERE status = 'candidate' AND expires_at < ?",
        (now,),
    )
    await db.commit()

    count = cur.rowcount or 0
    if count:
        logger.info("Expired {} stale session match candidates", count)
    return count


async def get_match_proposals(
    storage: Storage,
    co_op_id: str,
) -> list[dict[str, Any]]:
    """List all match proposals for sessions shared with a co-op."""
    db = storage._conn()
    cur = await db.execute(
        "SELECT smp.match_group_id, smp.proposer_fingerprint,"
        " smp.local_session_id, smp.peer_session_id,"
        " smp.centroid_lat, smp.centroid_lon,"
        " smp.start_utc, smp.end_utc, smp.status,"
        " smp.created_at, smp.expires_at,"
        " r.name AS local_name, r.shared_name,"
        " smn.shared_name AS match_shared_name"
        " FROM session_match_proposals smp"
        " JOIN races r ON smp.local_session_id = r.id"
        " JOIN session_sharing ss ON r.id = ss.session_id AND ss.co_op_id = ?"
        " LEFT JOIN session_match_names smn"
        "   ON smp.match_group_id = smn.match_group_id"
        " ORDER BY smp.created_at DESC",
        (co_op_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def find_best_local_match(
    storage: Storage,
    co_op_id: str,
    centroid_lat: float,
    centroid_lon: float,
    start_utc: str,
    end_utc: str,
    *,
    radius_nm: float = DEFAULT_RADIUS_NM,
    time_window_minutes: int = DEFAULT_TIME_OVERLAP_MINUTES,
    min_track_points: int = DEFAULT_MIN_TRACK_POINTS,
) -> int | None:
    """Find the best local session matching the given criteria.

    Returns the session_id or None.
    """
    db = storage._conn()

    cur = await db.execute(
        "SELECT r.id, r.start_utc, r.end_utc"
        " FROM races r"
        " JOIN session_sharing ss ON r.id = ss.session_id AND ss.co_op_id = ?"
        " WHERE r.end_utc IS NOT NULL"
        "   AND (r.match_group_id IS NULL OR r.match_confirmed = 0)",
        (co_op_id,),
    )
    sessions = [dict(r) for r in await cur.fetchall()]

    peer_start = datetime.fromisoformat(start_utc)
    peer_end = datetime.fromisoformat(end_utc)

    best_id: int | None = None
    best_distance = float("inf")

    for s in sessions:
        sid = s["id"]

        # Check embargo
        if await _is_session_embargoed(storage, sid, co_op_id):
            continue

        # Check track points
        point_count = await _count_session_positions(storage, sid)
        if point_count < min_track_points:
            continue

        local_start = datetime.fromisoformat(s["start_utc"])
        local_end = datetime.fromisoformat(s["end_utc"])

        overlap = _time_overlap_minutes(
            local_start,
            local_end,
            peer_start,
            peer_end,
            window_minutes=time_window_minutes,
        )
        if overlap <= 0:
            continue

        local_lat, local_lon = await compute_session_centroid(storage, sid)
        if local_lat == 0.0 and local_lon == 0.0:
            continue

        distance = haversine_nm(local_lat, local_lon, centroid_lat, centroid_lon)
        if distance <= radius_nm and distance < best_distance:
            best_distance = distance
            best_id = sid

    return best_id
