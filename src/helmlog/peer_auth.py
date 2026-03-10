"""Peer-to-peer request authentication for federation API.

Implements the Section 4 authentication protocol from the federation design:
each request carries four X-HelmLog-* headers with an Ed25519 signature
over ``METHOD /path timestamp nonce``.

Usage — signing outbound requests::

    headers = sign_request(private_key, fingerprint, "GET", "/co-op/abc/sessions")
    httpx.get(url, headers=headers)

Usage — verifying inbound requests (FastAPI dependency)::

    @app.get("/co-op/{co_op_id}/sessions")
    async def sessions(peer=Depends(require_peer_auth)):
        ...
"""

from __future__ import annotations

import base64
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from helmlog.storage import Storage

# Header names
HDR_BOAT = "X-HelmLog-Boat"
HDR_TIMESTAMP = "X-HelmLog-Timestamp"
HDR_NONCE = "X-HelmLog-Nonce"
HDR_SIG = "X-HelmLog-Sig"

# Clock skew windows (seconds)
_DEFAULT_WINDOW = 5 * 60  # 5 minutes
_RELAXED_WINDOW = 20 * 60  # 20 minutes

# In-memory nonce cache: {nonce: expiry_timestamp}
_seen_nonces: dict[str, float] = {}
_NONCE_PRUNE_INTERVAL = 60  # seconds between pruning sweeps
_last_prune: float = 0.0


# ---------------------------------------------------------------------------
# Signing (outbound)
# ---------------------------------------------------------------------------


def sign_request(
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    method: str,
    path: str,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build signed X-HelmLog-* headers for an outbound peer request.

    Returns a dict of headers to include in the HTTP request.
    """
    from helmlog.federation import sign_message

    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()
    if nonce is None:
        nonce = os.urandom(16).hex()

    canonical = f"{method.upper()} {path} {timestamp} {nonce}".encode()
    sig = sign_message(private_key, canonical)

    return {
        HDR_BOAT: fingerprint,
        HDR_TIMESTAMP: timestamp,
        HDR_NONCE: nonce,
        HDR_SIG: base64.b64encode(sig).decode(),
    }


# ---------------------------------------------------------------------------
# Verification (inbound)
# ---------------------------------------------------------------------------


def _prune_nonces() -> None:
    """Remove expired nonces from the in-memory cache."""
    global _last_prune  # noqa: PLW0603
    now = time.monotonic()
    if now - _last_prune < _NONCE_PRUNE_INTERVAL:
        return
    _last_prune = now
    cutoff = time.time() - _RELAXED_WINDOW
    expired = [n for n, exp in _seen_nonces.items() if exp < cutoff]
    for n in expired:
        del _seen_nonces[n]


def verify_peer_request(
    method: str,
    path: str,
    headers: dict[str, str],
    peer_pub_key: Ed25519PublicKey,
) -> bool:
    """Verify an inbound request's signature and replay protection.

    Returns True if the request is authentic and not replayed.
    """
    from helmlog.federation import verify_signature

    timestamp = headers.get(HDR_TIMESTAMP, "")
    nonce = headers.get(HDR_NONCE, "")
    sig_b64 = headers.get(HDR_SIG, "")

    if not all([timestamp, nonce, sig_b64]):
        return False

    # Verify signature
    canonical = f"{method.upper()} {path} {timestamp} {nonce}".encode()
    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        return False

    if not verify_signature(peer_pub_key, canonical, sig):
        return False

    # Check timestamp window — reject requests beyond the relaxed window
    try:
        ts = datetime.fromisoformat(timestamp)
        delta = abs((datetime.now(UTC) - ts).total_seconds())
        if delta > _RELAXED_WINDOW:
            logger.warning(
                "Peer request timestamp {} is {}s old (beyond relaxed window) — rejected",
                timestamp,
                int(delta),
            )
            return False
        if delta > _DEFAULT_WINDOW:
            logger.info(
                "Peer request timestamp {} is {}s old (beyond default window, within relaxed)",
                timestamp,
                int(delta),
            )
    except ValueError:
        logger.warning("Invalid timestamp format: {} — rejected", timestamp)
        return False

    # Nonce replay check
    _prune_nonces()
    if nonce in _seen_nonces:
        logger.warning("Replayed nonce detected: {}", nonce[:16])
        return False
    _seen_nonces[nonce] = time.time()

    return True


async def resolve_peer(
    storage: Storage,
    fingerprint: str,
) -> tuple[Ed25519PublicKey, dict[str, Any]] | None:
    """Look up a peer's public key by fingerprint.

    Searches co_op_peers for the fingerprint. Returns (public_key, peer_row)
    or None if not found.
    """
    from helmlog.federation import _pub_key_from_base64

    # Search across all co-ops for this fingerprint
    db = storage._conn()
    cur = await db.execute(
        "SELECT boat_pub, co_op_id, fingerprint, boat_name"
        " FROM co_op_peers WHERE fingerprint = ? LIMIT 1",
        (fingerprint,),
    )
    row = await cur.fetchone()
    if not row:
        return None

    try:
        pub_key = _pub_key_from_base64(row["boat_pub"])
    except Exception:
        logger.warning("Invalid public key for peer {}", fingerprint)
        return None

    return pub_key, dict(row)
