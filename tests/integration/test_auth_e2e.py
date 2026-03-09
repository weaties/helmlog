"""End-to-end authentication tests — real Ed25519 signing between two boats.

Validates that the peer authentication protocol works correctly:
- Valid signatures accepted
- Tampered/missing/forged signatures rejected
- Non-member boats rejected
- Nonce replay rejected
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.federation import generate_keypair
from helmlog.peer_auth import HDR_SIG, sign_request

if TYPE_CHECKING:
    from .conftest import Fleet


class TestValidAuth:
    """Requests with valid signatures from known peers succeed."""

    @pytest.mark.asyncio
    async def test_valid_signature_accepted(self, fleet: Fleet) -> None:
        """A properly signed request from a known peer succeeds."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_bidirectional_auth(self, fleet: Fleet) -> None:
        """Both boats can authenticate to each other."""
        co_op_id = fleet.boat_a.co_op_id

        # B → A
        path = f"/co-op/{co_op_id}/sessions"
        headers_b = fleet.boat_b.sign("GET", path)
        resp = await fleet.boat_a.client.get(path, headers=headers_b)
        assert resp.status_code == 200

        # A → B
        headers_a = fleet.boat_a.sign("GET", path)
        resp = await fleet.boat_b.client.get(path, headers=headers_a)
        assert resp.status_code == 200


class TestInvalidAuth:
    """Requests with bad authentication are rejected."""

    @pytest.mark.asyncio
    async def test_missing_headers_returns_401(self, fleet: Fleet) -> None:
        """Request with no auth headers returns 401."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"

        resp = await fleet.boat_a.client.get(path)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_fingerprint_returns_401(self, fleet: Fleet) -> None:
        """Request from an unknown boat fingerprint returns 401."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"

        # Generate a completely new keypair not known to either boat
        stranger_priv, _ = generate_keypair()
        headers = sign_request(stranger_priv, "unknown-fp-12345", "GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_tampered_signature_returns_401(self, fleet: Fleet) -> None:
        """Request with a tampered signature is rejected."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        # Tamper with the signature (flip a byte)
        sig = base64.b64decode(headers[HDR_SIG])
        tampered = bytes([sig[0] ^ 0xFF]) + sig[1:]
        headers[HDR_SIG] = base64.b64encode(tampered).decode()

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_path_in_signature_returns_401(self, fleet: Fleet) -> None:
        """Signature for a different path is rejected."""
        co_op_id = fleet.boat_a.co_op_id
        real_path = f"/co-op/{co_op_id}/sessions"

        # Sign for a different path
        headers = fleet.boat_b.sign("GET", "/co-op/wrong-path/sessions")

        resp = await fleet.boat_a.client.get(real_path, headers=headers)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_method_in_signature_returns_401(self, fleet: Fleet) -> None:
        """Signature for a different HTTP method is rejected."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"

        # Sign as POST, send as GET
        headers = fleet.boat_b.sign("POST", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_forged_identity_returns_401(self, fleet: Fleet) -> None:
        """Request signed by wrong key but claiming to be a known boat is rejected."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"

        # Sign with a stranger's key but claim to be boat B
        stranger_priv, _ = generate_keypair()
        headers = sign_request(
            stranger_priv,
            fleet.boat_b.fingerprint,
            "GET",
            path,
        )

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 401


class TestNonMember:
    """Requests from authenticated but non-member boats are rejected."""

    @pytest.mark.asyncio
    async def test_non_member_co_op_returns_403(self, fleet: Fleet) -> None:
        """A known peer requesting data from a co-op they don't belong to gets 403."""
        # Use a fake co-op ID that neither boat belongs to
        path = "/co-op/nonexistent-coop/sessions"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 403


class TestNonceReplay:
    """Replay protection via nonce deduplication."""

    @pytest.mark.asyncio
    async def test_replayed_nonce_rejected(self, fleet: Fleet) -> None:
        """A request with a reused nonce is rejected."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"

        # Create a signed request with a specific nonce
        timestamp = datetime.now(UTC).isoformat()
        nonce = os.urandom(16).hex()
        headers = sign_request(
            fleet.boat_b.identity["private_key"],
            fleet.boat_b.fingerprint,
            "GET",
            path,
            timestamp=timestamp,
            nonce=nonce,
        )

        # First request succeeds
        resp1 = await fleet.boat_a.client.get(path, headers=headers)
        assert resp1.status_code == 200

        # Replay with same nonce — rejected
        resp2 = await fleet.boat_a.client.get(path, headers=headers)
        assert resp2.status_code == 401
