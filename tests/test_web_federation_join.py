"""Tests for the POST /api/federation/join endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.federation import (
    _pub_key_to_base64,
    fingerprint_from_pub_bytes,
    generate_keypair,
    init_identity,
    sign_membership,
)
from helmlog.web import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage


def _client(storage: Storage) -> httpx.AsyncClient:
    app = create_app(storage)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestJoinEndpoint:
    @pytest.fixture
    def identity_dir(self, tmp_path: Path) -> Path:
        identity_dir = tmp_path / ".helmlog" / "identity"
        init_identity(
            identity_dir,
            sail_number="99",
            boat_name="Joining Boat",
            owner_email="join@example.com",
        )
        return identity_dir

    @pytest.mark.asyncio
    async def test_join_success(self, storage: Storage, identity_dir: Path) -> None:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        from helmlog.federation import load_identity

        _, card = load_identity(identity_dir)
        admin_priv, admin_pub = generate_keypair()
        admin_pub_b64 = _pub_key_to_base64(admin_pub)
        admin_fp = fingerprint_from_pub_bytes(
            admin_pub.public_bytes(Encoding.Raw, PublicFormat.Raw),
        )

        # Generate a properly signed membership (matching real invite bundle)
        membership = sign_membership(admin_priv, co_op_id="fleet1", boat_card=card)

        bundle = {
            "co_op_id": "fleet1",
            "co_op_name": "Test Fleet",
            "admin_pub": admin_pub_b64,
            "admin_fingerprint": admin_fp,
            "admin_boat_name": "Admin Boat",
            "admin_sail_number": "1",
            "admin_tailscale_ip": "100.64.0.1",
            "membership": membership.to_dict(),
        }

        with patch("helmlog.federation.load_identity", return_value=(None, card)):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/join",
                    json=bundle,
                )
                assert resp.status_code == 201
                data = resp.json()
                assert data["co_op_id"] == "fleet1"
                assert data["co_op_name"] == "Test Fleet"

        # Verify membership saved
        memberships = await storage.list_co_op_memberships()
        assert any(m["co_op_id"] == "fleet1" for m in memberships)

        # Verify admin saved as peer
        peers = await storage.list_co_op_peers("fleet1")
        admin_peers = [p for p in peers if p["fingerprint"] == admin_fp]
        assert len(admin_peers) == 1
        assert admin_peers[0]["boat_name"] == "Admin Boat"
        assert admin_peers[0]["tailscale_ip"] == "100.64.0.1"

    @pytest.mark.asyncio
    async def test_join_rejects_invalid_signature(self, storage: Storage) -> None:
        """Join with a tampered/unsigned membership should be rejected with 422."""
        admin_priv, admin_pub = generate_keypair()  # noqa: F841
        admin_pub_b64 = _pub_key_to_base64(admin_pub)

        bundle = {
            "co_op_id": "fleet_bad",
            "co_op_name": "Bad Fleet",
            "admin_pub": admin_pub_b64,
            "admin_fingerprint": "somefp",
            "admin_boat_name": "Admin",
            "admin_sail_number": "1",
            "membership": {
                "type": "membership",
                "co_op_id": "fleet_bad",
                "boat_pub": "fakepub==",
                "sail_number": "99",
                "boat_name": "Cheat Boat",
                "role": "member",
                "joined_at": "2026-01-01T00:00:00+00:00",
                "admin_sig": "bm90YXZhbGlkc2lnbmF0dXJlYXRhbGw=",  # invalid sig
            },
        }

        async with _client(storage) as c:
            resp = await c.post("/api/federation/join", json=bundle)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_join_missing_fields(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.post(
                "/api/federation/join",
                json={"co_op_id": "", "co_op_name": "", "admin_pub": ""},
            )
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_join_idempotent(self, storage: Storage, identity_dir: Path) -> None:
        """Joining the same co-op twice should be handled by ON CONFLICT."""
        from helmlog.federation import load_identity

        _, card = load_identity(identity_dir)
        bundle = {
            "co_op_id": "fleet2",
            "co_op_name": "Fleet 2",
            "admin_pub": "AAAA",
            "admin_fingerprint": "fp_admin",
            "admin_boat_name": "Admin",
            "admin_sail_number": "1",
            "membership": {},
        }

        with patch("helmlog.federation.load_identity", return_value=(None, card)):
            async with _client(storage) as c:
                resp1 = await c.post("/api/federation/join", json=bundle)
                assert resp1.status_code == 201
                resp2 = await c.post("/api/federation/join", json=bundle)
                assert resp2.status_code == 201

        # Should have exactly one membership
        memberships = await storage.list_co_op_memberships()
        fleet2 = [m for m in memberships if m["co_op_id"] == "fleet2"]
        assert len(fleet2) == 1
