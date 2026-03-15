"""Peer API — endpoints that remote co-op boats call over Tailscale.

Mounted at ``/co-op`` by ``web.py``. All endpoints (except ``/co-op/identity``)
require Ed25519 request authentication via the X-HelmLog-* headers.

These endpoints serve *shared* session data only — audio, notes, crew, sails,
transcripts, and video links are never exposed (per data-licensing.md).

Rate-limited and audit-logged per data-licensing.md §2 and §12.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from helmlog.peer_auth import (
    HDR_BOAT,
    HDR_NONCE,
    HDR_SIG,
    HDR_TIMESTAMP,
    resolve_peer,
    verify_peer_request,
)

router = APIRouter(prefix="/co-op", tags=["peer"])
_limiter = Limiter(key_func=get_remote_address, config_filename="/dev/null")

# Fields allowed in track responses (explicit allowlist per data licensing)
SHARED_TRACK_FIELDS = frozenset(
    {
        "LAT",
        "LON",
        "BSP",
        "HDG",
        "COG",
        "SOG",
        "TWS",
        "TWA",
        "AWS",
        "AWA",
    }
)

_WIND_REF_TRUE = 0
_WIND_REF_APPARENT = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_storage(request: Request) -> Any:  # noqa: ANN401
    return request.app.state.storage


async def _audit_peer(
    request: Request,
    action: str,
    peer: dict[str, Any],
    co_op_id: str,
    *,
    resource: str | None = None,
    points_count: int | None = None,
) -> None:
    """Log a co-op data access event to both audit trails."""
    storage = _get_storage(request)
    fp = peer.get("fingerprint", "")
    ip = request.client.host if request.client else None

    # Write to co_op_audit table (persistent, per data-licensing.md)
    await storage.log_co_op_audit(
        co_op_id=co_op_id,
        accessor_fp=fp,
        action=action,
        resource=resource,
        ip=ip,
        points_count=points_count,
    )

    # Also log to general audit trail for admin visibility
    boat = peer.get("boat_name", "?")
    detail = f"peer={boat} ({fp}) | co_op={co_op_id}"
    if resource:
        detail += f" | {resource}"
    await storage.log_action(
        action,
        detail=detail,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )


async def _authenticate_peer(
    request: Request,
    co_op_id: str | None = None,
) -> dict[str, Any]:
    """Authenticate and authorize a peer request.

    Raises HTTPException (401/403) on failure. Returns the peer row on success.
    """
    storage = _get_storage(request)
    fingerprint = request.headers.get(HDR_BOAT, "")

    if not fingerprint:
        raise HTTPException(status_code=401, detail="Missing authentication headers")

    # Resolve peer public key
    result = await resolve_peer(storage, fingerprint)
    if result is None:
        raise HTTPException(status_code=401, detail="Unknown peer: " + fingerprint)

    pub_key, peer = result

    # Verify signature and timestamp
    nonce = request.headers.get(HDR_NONCE, "")
    headers = {
        HDR_BOAT: fingerprint,
        HDR_TIMESTAMP: request.headers.get(HDR_TIMESTAMP, ""),
        HDR_NONCE: nonce,
        HDR_SIG: request.headers.get(HDR_SIG, ""),
    }
    if not verify_peer_request(request.method, request.url.path, headers, pub_key):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Persistent nonce replay check (SQLite-backed)
    if nonce:
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        if await storage.check_nonce(nonce_hash):
            logger.warning("Replayed nonce detected (persistent): {}", nonce[:16])
            raise HTTPException(status_code=401, detail="Replayed request")
        await storage.save_nonce(nonce_hash, fingerprint)

    # If co_op_id specified, verify the peer is a member of that co-op
    if co_op_id is not None:
        db = storage._conn()
        cur = await db.execute(
            "SELECT 1 FROM co_op_peers WHERE co_op_id = ? AND fingerprint = ?",
            (co_op_id, fingerprint),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=403, detail="Not a member of this co-op")

    # Update last_seen
    now = datetime.now(UTC).isoformat()
    db = storage._conn()
    await db.execute(
        "UPDATE co_op_peers SET last_seen = ? WHERE fingerprint = ?",
        (now, fingerprint),
    )
    await db.commit()

    logger.info("Peer request from {} ({})", peer.get("boat_name", "?"), fingerprint)
    return peer


async def _check_embargo(
    storage: Any,  # noqa: ANN401
    session_id: int,
    co_op_id: str,
) -> None:
    """Raise HTTPException 403 if session is under embargo for this co-op."""
    sharing = await storage.get_session_sharing(session_id)
    for s in sharing:
        if s["co_op_id"] == co_op_id and s.get("embargo_until"):
            try:
                embargo = datetime.fromisoformat(s["embargo_until"])
                if embargo > datetime.now(UTC):
                    raise HTTPException(
                        status_code=403,
                        detail="Session is under embargo",
                        headers={"X-Available-At": s["embargo_until"]},
                    )
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Public endpoint (no auth)
# ---------------------------------------------------------------------------


@router.get("/identity")
async def peer_identity(request: Request) -> JSONResponse:
    """Return this boat's public identity (boat card). No auth required."""
    from helmlog.federation import load_identity

    try:
        _, card = load_identity()
        return JSONResponse(card.to_dict())
    except FileNotFoundError:
        return JSONResponse(
            {"detail": "No identity initialized"},
            status_code=404,
        )


# ---------------------------------------------------------------------------
# Authenticated endpoints (audit-logged per data-licensing.md)
# ---------------------------------------------------------------------------


@router.get("/{co_op_id}/sessions")
@_limiter.limit("30/minute")
async def peer_sessions(
    request: Request,
    co_op_id: str,
) -> JSONResponse:
    """List sessions this boat has shared with the co-op."""
    peer = await _authenticate_peer(request, co_op_id)

    storage = _get_storage(request)
    now = datetime.now(UTC)

    # Query shared sessions for this co-op
    db = storage._conn()
    cur = await db.execute(
        "SELECT r.id, r.name, r.event, r.race_num, r.date,"
        " r.start_utc, r.end_utc, r.session_type,"
        " ss.embargo_until, ss.event_name, ss.shared_at"
        " FROM session_sharing ss"
        " JOIN races r ON ss.session_id = r.id"
        " WHERE ss.co_op_id = ?"
        " ORDER BY r.start_utc DESC",
        (co_op_id,),
    )
    rows = await cur.fetchall()

    sessions = []
    for r in rows:
        row = dict(r)
        # Check embargo
        if row.get("embargo_until"):
            try:
                embargo = datetime.fromisoformat(row["embargo_until"])
                if embargo > now:
                    sessions.append(
                        {
                            "session_id": row["id"],
                            "status": "embargoed",
                            "available_at": row["embargo_until"],
                        }
                    )
                    continue
            except ValueError:
                pass

        sessions.append(
            {
                "session_id": row["id"],
                "status": "available",
                "name": row["name"],
                "event": row["event"],
                "race_num": row["race_num"],
                "date": row["date"],
                "start_utc": row["start_utc"],
                "end_utc": row["end_utc"],
                "session_type": row["session_type"],
            }
        )

    await _audit_peer(
        request,
        "coop.peer.sessions",
        peer,
        co_op_id,
        resource=f"count={len(sessions)}",
    )
    return JSONResponse({"sessions": sessions})


@router.get("/{co_op_id}/sessions/{session_id}/track")
@_limiter.limit("10/minute")
async def peer_session_track(
    request: Request,
    co_op_id: str,
    session_id: int,
) -> JSONResponse:
    """Return instrument data for a shared session (shared fields only)."""
    peer = await _authenticate_peer(request, co_op_id)

    storage = _get_storage(request)

    # Verify session is shared with this co-op
    if not await storage.is_session_shared(session_id, co_op_id):
        return JSONResponse(
            {"detail": "Session not shared with this co-op"},
            status_code=404,
        )

    # Check embargo
    await _check_embargo(storage, session_id, co_op_id)

    # Load session time range
    db = storage._conn()
    cur = await db.execute(
        "SELECT start_utc, end_utc FROM races WHERE id = ?",
        (session_id,),
    )
    race = await cur.fetchone()
    if not race or not race["end_utc"]:
        return JSONResponse(
            {"detail": "Session not found or still running"},
            status_code=404,
        )

    start = datetime.fromisoformat(race["start_utc"])
    end = datetime.fromisoformat(race["end_utc"])

    # Load instrument data
    positions = await storage.query_range("positions", start, end)
    headings = await storage.query_range("headings", start, end)
    speeds = await storage.query_range("speeds", start, end)
    cogsog = await storage.query_range("cogsog", start, end)
    winds = await storage.query_range("winds", start, end)

    # Index by second
    pos_idx = _by_second(positions)
    hdg_idx = _by_second(headings)
    bsp_idx = _by_second(speeds)
    cs_idx = _by_second(cogsog)
    tw_idx = _by_second([w for w in winds if w.get("reference") == _WIND_REF_TRUE])
    aw_idx = _by_second([w for w in winds if w.get("reference") == _WIND_REF_APPARENT])

    # Build 1 Hz rows with shared fields only
    track: list[dict[str, Any]] = []
    t = start
    while t <= end:
        sec = t.isoformat()[:19]  # truncate to match _by_second index keys
        p = pos_idx.get(sec, {})
        if not p:
            t += timedelta(seconds=1)
            continue

        row: dict[str, Any] = {"timestamp": sec}
        row["LAT"] = p.get("latitude_deg")
        row["LON"] = p.get("longitude_deg")

        h = hdg_idx.get(sec, {})
        row["HDG"] = h.get("heading_deg")

        b = bsp_idx.get(sec, {})
        row["BSP"] = b.get("speed_kts")

        c = cs_idx.get(sec, {})
        row["COG"] = c.get("cog_deg")
        row["SOG"] = c.get("sog_kts")

        tw = tw_idx.get(sec, {})
        row["TWS"] = tw.get("wind_speed_kts")
        row["TWA"] = tw.get("wind_angle_deg")

        aw = aw_idx.get(sec, {})
        row["AWS"] = aw.get("wind_speed_kts")
        row["AWA"] = aw.get("wind_angle_deg")

        track.append(row)
        t += timedelta(seconds=1)

    await _audit_peer(
        request,
        "coop.peer.track",
        peer,
        co_op_id,
        resource=f"session={session_id}",
        points_count=len(track),
    )
    return JSONResponse({"track": track, "count": len(track)})


@router.get("/{co_op_id}/sessions/{session_id}/results")
@_limiter.limit("30/minute")
async def peer_session_results(
    request: Request,
    co_op_id: str,
    session_id: int,
) -> JSONResponse:
    """Return race results for a shared session (notes excluded per data licensing)."""
    peer = await _authenticate_peer(request, co_op_id)

    storage = _get_storage(request)

    if not await storage.is_session_shared(session_id, co_op_id):
        return JSONResponse(
            {"detail": "Session not shared with this co-op"},
            status_code=404,
        )

    # Check embargo (same as track endpoint)
    await _check_embargo(storage, session_id, co_op_id)

    results = await storage.list_race_results(session_id)

    # Strip PII fields — notes are PII per data-licensing.md
    for r in results:
        r.pop("notes", None)

    await _audit_peer(
        request,
        "coop.peer.results",
        peer,
        co_op_id,
        resource=f"session={session_id}",
    )
    return JSONResponse({"results": results})


@router.get("/{co_op_id}/sessions/{session_id}/wind-field")
@_limiter.limit("10/minute")
async def peer_session_wind_field(
    request: Request,
    co_op_id: str,
    session_id: int,
) -> JSONResponse:
    """Return wind field parameters and course marks for a shared synthesized session.

    Co-op members can use these to reconstruct the same WindField and synthesize
    their own races under identical conditions.
    """
    peer = await _authenticate_peer(request, co_op_id)

    storage = _get_storage(request)

    if not await storage.is_session_shared(session_id, co_op_id):
        return JSONResponse(
            {"detail": "Session not shared with this co-op"},
            status_code=404,
        )

    await _check_embargo(storage, session_id, co_op_id)

    params = await storage.get_synth_wind_params(session_id)
    if params is None:
        return JSONResponse(
            {"detail": "No wind field for this session"},
            status_code=404,
        )

    marks = await storage.get_synth_course_marks(session_id)

    # Look up session start_utc for co-op synthesis (#246)
    db = storage._conn()
    cur = await db.execute(
        "SELECT start_utc FROM races WHERE id = ?",
        (session_id,),
    )
    race = await cur.fetchone()
    start_utc = dict(race)["start_utc"] if race else None

    # Wind field params are synthetic simulation config, not PII
    resp = {
        "start_utc": start_utc,
        "wind_params": {
            "seed": params["seed"],
            "base_twd": params["base_twd"],
            "tws_low": params["tws_low"],
            "tws_high": params["tws_high"],
            "shift_interval_lo": params["shift_interval_lo"],
            "shift_interval_hi": params["shift_interval_hi"],
            "shift_magnitude_lo": params["shift_magnitude_lo"],
            "shift_magnitude_hi": params["shift_magnitude_hi"],
            "ref_lat": params["ref_lat"],
            "ref_lon": params["ref_lon"],
            "duration_s": params["duration_s"],
            "course_type": params["course_type"],
            "leg_distance_nm": params["leg_distance_nm"],
            "laps": params["laps"],
            "mark_sequence": params["mark_sequence"],
        },
        "marks": marks,
    }

    await _audit_peer(
        request,
        "coop.peer.wind_field",
        peer,
        co_op_id,
        resource=f"session={session_id}",
    )
    return JSONResponse(resp)


# ---------------------------------------------------------------------------
# Session matching endpoints (#281)
# ---------------------------------------------------------------------------


@router.get("/{co_op_id}/session-matches")
@_limiter.limit("30/minute")
async def peer_session_matches(
    request: Request,
    co_op_id: str,
) -> JSONResponse:
    """List session match proposals for this co-op."""
    peer = await _authenticate_peer(request, co_op_id)
    storage = _get_storage(request)

    from helmlog.session_matching import get_match_proposals

    matches = await get_match_proposals(storage, co_op_id)

    await _audit_peer(
        request,
        "coop.peer.session_matches.list",
        peer,
        co_op_id,
        resource=f"count={len(matches)}",
    )
    return JSONResponse({"matches": matches})


@router.post("/{co_op_id}/session-matches/propose")
@_limiter.limit("10/minute")
async def peer_propose_match(
    request: Request,
    co_op_id: str,
) -> JSONResponse:
    """Propose a session match based on centroid and time range.

    The receiving boat finds its best local session match and creates
    a match proposal linking the two sessions.
    """
    peer = await _authenticate_peer(request, co_op_id)
    storage = _get_storage(request)

    body = await request.json()
    centroid_lat = body.get("centroid_lat", 0.0)
    centroid_lon = body.get("centroid_lon", 0.0)
    start_utc = body.get("start_utc", "")
    end_utc = body.get("end_utc", "")
    peer_session_id = body.get("local_session_id", 0)

    if not start_utc or not end_utc:
        raise HTTPException(status_code=400, detail="start_utc and end_utc required")

    from helmlog.session_matching import find_best_local_match, propose_match

    local_id = await find_best_local_match(
        storage,
        co_op_id,
        centroid_lat,
        centroid_lon,
        start_utc,
        end_utc,
    )

    if local_id is None:
        raise HTTPException(
            status_code=404,
            detail="No matching local session found",
        )

    fp = peer.get("fingerprint", "")
    match_id = await propose_match(
        storage,
        local_session_id=local_id,
        peer_fingerprint=fp,
        peer_session_id=peer_session_id,
        centroid_lat=centroid_lat,
        centroid_lon=centroid_lon,
        start_utc=start_utc,
        end_utc=end_utc,
    )

    await _audit_peer(
        request,
        "coop.peer.session_matches.propose",
        peer,
        co_op_id,
        resource=f"match={match_id}",
    )
    return JSONResponse(
        {
            "match_group_id": match_id,
            "matched_session_id": local_id,
            "status": "candidate",
        }
    )


@router.post("/{co_op_id}/session-matches/{match_id}/confirm")
@_limiter.limit("10/minute")
async def peer_confirm_match(
    request: Request,
    co_op_id: str,
    match_id: str,
) -> JSONResponse:
    """Confirm a session match proposal."""
    peer = await _authenticate_peer(request, co_op_id)
    storage = _get_storage(request)

    from helmlog.session_matching import confirm_match

    fp = peer.get("fingerprint", "")
    ok = await confirm_match(storage, match_id, fp)

    if not ok:
        raise HTTPException(status_code=404, detail="Match not found or cannot be confirmed")

    # Check if now confirmed
    db = storage._conn()
    cur = await db.execute(
        "SELECT status FROM session_match_proposals WHERE match_group_id = ?",
        (match_id,),
    )
    row = await cur.fetchone()
    status = dict(row)["status"] if row else "candidate"

    await _audit_peer(
        request,
        "coop.peer.session_matches.confirm",
        peer,
        co_op_id,
        resource=f"match={match_id}",
    )
    return JSONResponse({"match_group_id": match_id, "status": status})


@router.post("/{co_op_id}/session-matches/{match_id}/reject")
@_limiter.limit("10/minute")
async def peer_reject_match(
    request: Request,
    co_op_id: str,
    match_id: str,
) -> JSONResponse:
    """Reject a session match proposal."""
    peer = await _authenticate_peer(request, co_op_id)
    storage = _get_storage(request)

    from helmlog.session_matching import reject_match

    fp = peer.get("fingerprint", "")
    ok = await reject_match(storage, match_id, fp)

    if not ok:
        raise HTTPException(status_code=404, detail="Match not found or cannot be rejected")

    await _audit_peer(
        request,
        "coop.peer.session_matches.reject",
        peer,
        co_op_id,
        resource=f"match={match_id}",
    )
    return JSONResponse({"match_group_id": match_id, "status": "rejected"})


@router.put("/{co_op_id}/session-matches/{match_id}/name")
@_limiter.limit("10/minute")
async def peer_set_match_name(
    request: Request,
    co_op_id: str,
    match_id: str,
) -> JSONResponse:
    """Propose or update the shared name for a confirmed match."""
    peer = await _authenticate_peer(request, co_op_id)
    storage = _get_storage(request)

    body = await request.json()
    shared_name = body.get("shared_name", "")
    if not shared_name:
        raise HTTPException(status_code=400, detail="shared_name required")

    from helmlog.session_matching import set_shared_name

    fp = peer.get("fingerprint", "")
    ok = await set_shared_name(storage, match_id, shared_name, fp)

    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Match not found or not confirmed",
        )

    await _audit_peer(
        request,
        "coop.peer.session_matches.name",
        peer,
        co_op_id,
        resource=f"match={match_id}",
    )
    return JSONResponse(
        {
            "match_group_id": match_id,
            "shared_name": shared_name,
        }
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _by_second(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index rows by truncated-to-second ISO timestamp."""
    idx: dict[str, dict[str, Any]] = {}
    for r in rows:
        ts = r.get("ts", "")
        if isinstance(ts, str) and len(ts) >= 19:
            sec = ts[:19]  # truncate to second
            if sec not in idx:
                idx[sec] = r
    return idx
