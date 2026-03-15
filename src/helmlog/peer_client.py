"""Peer client — query remote co-op boats for shared session data.

Uses httpx to call the ``/co-op/*`` endpoints on remote Pis over Tailscale,
with Ed25519 signed request headers for authentication.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from helmlog.peer_auth import sign_request

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from helmlog.storage import Storage

# Timeout for peer requests (Tailscale LAN — should be fast)
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def probe_peer(ip: str, port: int = 80) -> dict[str, Any] | None:
    """Probe a peer's identity endpoint. Returns boat card dict or None."""
    url = f"http://{ip}:{port}/co-op/identity"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
    except Exception as exc:
        logger.debug("Peer probe failed for {}: {}", ip, exc)
    return None


async def discover_peers(storage: Storage, port: int = 80) -> list[dict[str, Any]]:
    """Probe all known peers and update their online status.

    Returns list of peers that responded successfully.
    """
    db = storage._conn()
    cur = await db.execute(
        "SELECT fingerprint, boat_name, tailscale_ip"
        " FROM co_op_peers WHERE tailscale_ip IS NOT NULL"
    )
    peers = [dict(r) for r in await cur.fetchall()]

    if not peers:
        return []

    async def _probe(peer: dict[str, Any]) -> dict[str, Any] | None:
        ip = peer["tailscale_ip"]
        card = await probe_peer(ip, port)
        if card is not None:
            # Update last_seen
            from datetime import UTC, datetime

            now = datetime.now(UTC).isoformat()
            await db.execute(
                "UPDATE co_op_peers SET last_seen = ? WHERE fingerprint = ?",
                (now, peer["fingerprint"]),
            )
            await db.commit()
            return {**peer, "online": True, "card": card}
        return {**peer, "online": False}

    results = await asyncio.gather(*[_probe(p) for p in peers], return_exceptions=True)
    online = []
    for r in results:
        if isinstance(r, dict) and r.get("online"):
            online.append(r)
        elif isinstance(r, Exception):
            logger.debug("Peer probe error: {}", r)
    return online


# ---------------------------------------------------------------------------
# Session queries
# ---------------------------------------------------------------------------


async def fetch_shared_sessions(
    peer_ip: str,
    co_op_id: str,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> list[dict[str, Any]]:
    """Fetch the list of shared sessions from a remote peer."""
    path = f"/co-op/{co_op_id}/sessions"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "GET", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return list(data.get("sessions", []))
            logger.warning(
                "Peer {} returned {} for sessions: {}",
                peer_ip,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Failed to fetch sessions from {}: {}", peer_ip, exc)
    return []


async def fetch_session_track(
    peer_ip: str,
    co_op_id: str,
    session_id: int,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> list[dict[str, Any]]:
    """Fetch track data for a specific session from a remote peer."""
    path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "GET", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return list(data.get("track", []))
            logger.warning(
                "Peer {} returned {} for track: {}",
                peer_ip,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Failed to fetch track from {}: {}", peer_ip, exc)
    return []


async def fetch_session_results(
    peer_ip: str,
    co_op_id: str,
    session_id: int,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> list[dict[str, Any]]:
    """Fetch race results for a specific session from a remote peer."""
    path = f"/co-op/{co_op_id}/sessions/{session_id}/results"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "GET", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return list(data.get("results", []))
    except Exception as exc:
        logger.warning("Failed to fetch results from {}: {}", peer_ip, exc)
    return []


async def fetch_session_wind_field(
    peer_ip: str,
    co_op_id: str,
    session_id: int,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> dict[str, Any] | None:
    """Fetch wind field params, course marks, and start time from a remote peer.

    Returns the full response dict (start_utc, wind_params, marks) or None on failure.
    """
    path = f"/co-op/{co_op_id}/sessions/{session_id}/wind-field"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "GET", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return data
            logger.warning(
                "Peer {} returned {} for wind-field: {}",
                peer_ip,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Failed to fetch wind-field from {}: {}", peer_ip, exc)
    return None


# ---------------------------------------------------------------------------
# Aggregate: fetch all peer sessions for a co-op
# ---------------------------------------------------------------------------


async def fetch_all_peer_sessions(
    storage: Storage,
    co_op_id: str,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> list[dict[str, Any]]:
    """Query all online peers in a co-op for their shared sessions.

    Returns a list of ``{peer, sessions}`` dicts — one per responding peer.
    """
    db = storage._conn()
    cur = await db.execute(
        "SELECT fingerprint, boat_name, sail_number, tailscale_ip"
        " FROM co_op_peers"
        " WHERE co_op_id = ? AND tailscale_ip IS NOT NULL",
        (co_op_id,),
    )
    peers = [dict(r) for r in await cur.fetchall()]

    # Skip ourselves
    try:
        from helmlog.federation import load_identity

        _, my_card = load_identity()
        peers = [p for p in peers if p["fingerprint"] != my_card.fingerprint]
    except FileNotFoundError:
        pass

    if not peers:
        return []

    async def _query(peer: dict[str, Any]) -> dict[str, Any]:
        sessions = await fetch_shared_sessions(
            peer["tailscale_ip"],
            co_op_id,
            private_key,
            fingerprint,
            port=port,
        )
        return {
            "fingerprint": peer["fingerprint"],
            "boat_name": peer.get("boat_name", ""),
            "sail_number": peer.get("sail_number", ""),
            "tailscale_ip": peer["tailscale_ip"],
            "online": bool(sessions) or await _is_online(peer["tailscale_ip"], port),
            "sessions": sessions,
        }

    results = await asyncio.gather(
        *[_query(p) for p in peers],
        return_exceptions=True,
    )

    peer_data = []
    for r in results:
        if isinstance(r, dict):
            peer_data.append(r)
        elif isinstance(r, Exception):
            logger.debug("Peer query error: {}", r)
    return peer_data


async def _is_online(ip: str, port: int) -> bool:
    """Quick check if a peer is reachable."""
    return await probe_peer(ip, port) is not None


# ---------------------------------------------------------------------------
# Session matching (#281)
# ---------------------------------------------------------------------------


async def fetch_session_matches(
    peer_ip: str,
    co_op_id: str,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> list[dict[str, Any]]:
    """Fetch session match proposals from a remote peer."""
    path = f"/co-op/{co_op_id}/session-matches"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "GET", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return list(data.get("matches", []))
            logger.warning(
                "Peer {} returned {} for session-matches: {}",
                peer_ip,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Failed to fetch session-matches from {}: {}", peer_ip, exc)
    return []


async def propose_session_match(
    peer_ip: str,
    co_op_id: str,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    local_session_id: int,
    centroid_lat: float,
    centroid_lon: float,
    start_utc: str,
    end_utc: str,
    port: int = 80,
) -> dict[str, Any] | None:
    """Propose a session match to a remote peer. Returns response dict or None."""
    path = f"/co-op/{co_op_id}/session-matches/propose"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "POST", path)

    body = {
        "local_session_id": local_session_id,
        "centroid_lat": centroid_lat,
        "centroid_lon": centroid_lon,
        "start_utc": start_utc,
        "end_utc": end_utc,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            logger.warning(
                "Peer {} returned {} for propose match: {}",
                peer_ip,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning("Failed to propose match to {}: {}", peer_ip, exc)
    return None


async def confirm_session_match(
    peer_ip: str,
    co_op_id: str,
    match_id: str,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> dict[str, Any] | None:
    """Confirm a session match on a remote peer."""
    path = f"/co-op/{co_op_id}/session-matches/{match_id}/confirm"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "POST", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, headers=headers)
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
    except Exception as exc:
        logger.warning("Failed to confirm match on {}: {}", peer_ip, exc)
    return None


async def reject_session_match(
    peer_ip: str,
    co_op_id: str,
    match_id: str,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> dict[str, Any] | None:
    """Reject a session match on a remote peer."""
    path = f"/co-op/{co_op_id}/session-matches/{match_id}/reject"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "POST", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, headers=headers)
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
    except Exception as exc:
        logger.warning("Failed to reject match on {}: {}", peer_ip, exc)
    return None


async def set_match_name(
    peer_ip: str,
    co_op_id: str,
    match_id: str,
    shared_name: str,
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    *,
    port: int = 80,
) -> dict[str, Any] | None:
    """Set or update the shared name for a match on a remote peer."""
    path = f"/co-op/{co_op_id}/session-matches/{match_id}/name"
    url = f"http://{peer_ip}:{port}{path}"
    headers = sign_request(private_key, fingerprint, "PUT", path)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.put(
                url,
                json={"shared_name": shared_name},
                headers=headers,
            )
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
    except Exception as exc:
        logger.warning("Failed to set match name on {}: {}", peer_ip, exc)
    return None
