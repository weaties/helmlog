"""Tests for federation admin web endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

# Patch targets — functions are locally imported in web.py handlers,
# so we patch at the helmlog.federation module level.
_FED = "helmlog.federation"


def _client(storage: Storage) -> httpx.AsyncClient:
    app = create_app(storage)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------


class TestFederationPage:
    @pytest.mark.asyncio
    async def test_page_loads(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.get("/admin/federation")
            assert resp.status_code == 200
            assert "Federation" in resp.text


# ---------------------------------------------------------------------------
# Identity API
# ---------------------------------------------------------------------------


class TestIdentityAPI:
    @pytest.mark.asyncio
    async def test_get_identity_empty(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.get("/api/federation/identity")
            assert resp.status_code == 200
            data = resp.json()
            assert data["identity"] is None
            assert data["boat_card_json"] is None

    @pytest.mark.asyncio
    async def test_get_identity_with_data(self, storage: Storage) -> None:
        await storage.save_boat_identity(
            pub_key="abc",
            fingerprint="fp123",
            sail_number="69",
            boat_name="Javelina",
        )
        async with _client(storage) as c:
            with patch(f"{_FED}.load_identity", side_effect=FileNotFoundError):
                resp = await c.get("/api/federation/identity")
                assert resp.status_code == 200
                data = resp.json()
                assert data["identity"]["fingerprint"] == "fp123"

    @pytest.mark.asyncio
    async def test_init_identity(self, storage: Storage, tmp_path: object) -> None:
        from helmlog.federation import BoatCard

        mock_card = BoatCard(
            pub_key="testpub",
            fingerprint="testfp",
            sail_number="42",
            boat_name="TestBoat",
        )
        with (
            patch(f"{_FED}.identity_exists", return_value=False),
            patch(f"{_FED}.init_identity", return_value=mock_card),
        ):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/identity",
                    json={
                        "sail_number": "42",
                        "boat_name": "TestBoat",
                    },
                )
                assert resp.status_code == 201
                data = resp.json()
                assert data["fingerprint"] == "testfp"
                assert data["boat_name"] == "TestBoat"

        # Should be persisted in storage
        identity = await storage.get_boat_identity()
        assert identity is not None
        assert identity["pub_key"] == "testpub"

    @pytest.mark.asyncio
    async def test_init_identity_missing_fields(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.post(
                "/api/federation/identity",
                json={
                    "sail_number": "",
                    "boat_name": "",
                },
            )
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_init_identity_already_exists(self, storage: Storage) -> None:
        with patch(f"{_FED}.identity_exists", return_value=True):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/identity",
                    json={
                        "sail_number": "42",
                        "boat_name": "Test",
                    },
                )
                assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Co-ops API
# ---------------------------------------------------------------------------


class TestCoOpsAPI:
    @pytest.mark.asyncio
    async def test_list_empty(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.get("/api/federation/co-ops")
            assert resp.status_code == 200
            assert resp.json()["co_ops"] == []

    @pytest.mark.asyncio
    async def test_list_with_membership(self, storage: Storage) -> None:
        await storage.save_co_op_membership(
            co_op_id="coop1",
            co_op_name="Fleet A",
            co_op_pub="pub1",
            membership_json="{}",
            role="admin",
        )
        async with _client(storage) as c:
            resp = await c.get("/api/federation/co-ops")
            data = resp.json()
            assert len(data["co_ops"]) == 1
            assert data["co_ops"][0]["co_op_name"] == "Fleet A"
            assert "peers" in data["co_ops"][0]

    @pytest.mark.asyncio
    async def test_create_coop_no_identity(self, storage: Storage) -> None:
        with patch(f"{_FED}.load_identity", side_effect=FileNotFoundError):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/co-ops",
                    json={
                        "name": "Test Fleet",
                    },
                )
                assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_coop_missing_name(self, storage: Storage) -> None:
        from helmlog.federation import BoatCard, generate_keypair

        priv, _ = generate_keypair()
        card = BoatCard(
            pub_key="pub",
            fingerprint="fp",
            sail_number="1",
            boat_name="Test",
            owner_email="test@example.com",
        )
        with patch(f"{_FED}.load_identity", return_value=(priv, card)):
            async with _client(storage) as c:
                resp = await c.post("/api/federation/co-ops", json={"name": ""})
                assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_coop_no_email(self, storage: Storage) -> None:
        from helmlog.federation import BoatCard, generate_keypair

        priv, _ = generate_keypair()
        card = BoatCard(
            pub_key="pub",
            fingerprint="fp",
            sail_number="1",
            boat_name="Test",
        )
        with patch(f"{_FED}.load_identity", return_value=(priv, card)):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/co-ops",
                    json={
                        "name": "Test Fleet",
                    },
                )
                assert resp.status_code == 422
                assert "email" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_coop_success(
        self,
        storage: Storage,
        tmp_path: object,
    ) -> None:
        from pathlib import Path

        from helmlog.federation import Charter, init_identity
        from helmlog.federation import load_identity as _real_load

        identity_dir = Path(str(tmp_path)) / ".helmlog" / "identity"
        init_identity(
            identity_dir,
            sail_number="69",
            boat_name="Javelina",
            owner_email="test@example.com",
        )
        priv, loaded_card = _real_load(identity_dir)

        mock_charter = Charter(
            co_op_id="test-coop-id",
            name="Test Fleet",
            areas=["Bay"],
            admin_boat_pub=loaded_card.pub_key,
            admin_boat_fingerprint=loaded_card.fingerprint,
            created_at="2026-03-08T00:00:00Z",
        )

        with (
            patch(f"{_FED}.load_identity", return_value=(priv, loaded_card)),
            patch(f"{_FED}.create_co_op", return_value=mock_charter),
            patch(f"{_FED}.list_co_op_members", return_value=[]),
        ):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/co-ops",
                    json={
                        "name": "Test Fleet",
                        "areas": ["Bay"],
                    },
                )
                assert resp.status_code == 201
                data = resp.json()
                assert data["name"] == "Test Fleet"

    @pytest.mark.asyncio
    async def test_create_coop_saves_admin_as_peer(
        self,
        storage: Storage,
        tmp_path: object,
    ) -> None:
        """Creating a co-op should save the admin boat as a peer."""
        from pathlib import Path

        from helmlog.federation import Charter, MembershipRecord, init_identity
        from helmlog.federation import load_identity as _real_load

        identity_dir = Path(str(tmp_path)) / ".helmlog" / "identity"
        init_identity(
            identity_dir,
            sail_number="69",
            boat_name="Javelina",
            owner_email="test@example.com",
        )
        priv, loaded_card = _real_load(identity_dir)

        mock_charter = Charter(
            co_op_id="test-coop-id",
            name="Test Fleet",
            areas=[],
            admin_boat_pub=loaded_card.pub_key,
            admin_boat_fingerprint=loaded_card.fingerprint,
            created_at="2026-03-08T00:00:00Z",
        )
        mock_member = MembershipRecord(
            co_op_id="test-coop-id",
            boat_pub=loaded_card.pub_key,
            sail_number="69",
            boat_name="Javelina",
            role="admin",
            joined_at="2026-03-08T00:00:00Z",
        )

        with (
            patch(f"{_FED}.load_identity", return_value=(priv, loaded_card)),
            patch(f"{_FED}.create_co_op", return_value=mock_charter),
            patch(f"{_FED}.list_co_op_members", return_value=[mock_member]),
        ):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/co-ops",
                    json={
                        "name": "Test Fleet",
                    },
                )
                assert resp.status_code == 201

        # Admin boat should appear as a peer
        peers = await storage.list_co_op_peers("test-coop-id")
        assert len(peers) == 1
        assert peers[0]["boat_name"] == "Javelina"
        assert peers[0]["fingerprint"] == loaded_card.fingerprint


# ---------------------------------------------------------------------------
# Invite API
# ---------------------------------------------------------------------------


class TestInviteAPI:
    @pytest.mark.asyncio
    async def test_invite_not_admin(self, storage: Storage) -> None:
        """Invite should fail if not admin of the co-op."""
        from helmlog.federation import BoatCard, generate_keypair

        priv, _ = generate_keypair()
        card = BoatCard(
            pub_key="pub",
            fingerprint="fp",
            sail_number="1",
            boat_name="Admin",
        )
        with patch(f"{_FED}.load_identity", return_value=(priv, card)):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/co-ops/nonexistent/invite",
                    json={"pub": "x", "fingerprint": "y", "sail_number": "1", "name": "Boat"},
                )
                assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invite_bad_card(self, storage: Storage) -> None:
        """Invite should fail with missing boat card fields."""
        from helmlog.federation import BoatCard, generate_keypair

        priv, _ = generate_keypair()
        card = BoatCard(
            pub_key="pub",
            fingerprint="fp",
            sail_number="1",
            boat_name="Admin",
        )
        await storage.save_co_op_membership(
            co_op_id="coop1",
            co_op_name="Fleet",
            co_op_pub="pub",
            membership_json="{}",
            role="admin",
        )
        with patch(f"{_FED}.load_identity", return_value=(priv, card)):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/co-ops/coop1/invite",
                    json={"pub": "x"},  # missing required fields
                )
                assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invite_success(
        self,
        storage: Storage,
        tmp_path: object,
    ) -> None:
        """Full invite flow — sign membership and persist peer."""
        from pathlib import Path

        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        from helmlog.federation import (
            _pub_key_to_base64,
            fingerprint_from_pub_bytes,
            generate_keypair,
            init_identity,
        )
        from helmlog.federation import load_identity as _real_load

        identity_dir = Path(str(tmp_path)) / ".helmlog" / "identity"
        init_identity(
            identity_dir,
            sail_number="69",
            boat_name="Admin Boat",
            owner_email="admin@example.com",
        )
        priv, admin_card = _real_load(identity_dir)

        # Create co-op directory structure
        co_op_dir = identity_dir.parent / "co-ops" / "coop1" / "members"
        co_op_dir.mkdir(parents=True)

        await storage.save_co_op_membership(
            co_op_id="coop1",
            co_op_name="Fleet",
            co_op_pub=admin_card.pub_key,
            membership_json="{}",
            role="admin",
        )

        # Generate invitee keypair
        _, invitee_pub = generate_keypair()
        invitee_pub_b64 = _pub_key_to_base64(invitee_pub)
        invitee_fp = fingerprint_from_pub_bytes(
            invitee_pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        )

        home = Path(str(tmp_path))
        with (
            patch(f"{_FED}.load_identity", return_value=(priv, admin_card)),
            patch("pathlib.Path.home", return_value=home),
        ):
            async with _client(storage) as c:
                resp = await c.post(
                    "/api/federation/co-ops/coop1/invite",
                    json={
                        "pub": invitee_pub_b64,
                        "fingerprint": invitee_fp,
                        "sail_number": "42",
                        "name": "Invitee Boat",
                    },
                )
                assert resp.status_code == 201
                data = resp.json()
                assert data["boat_name"] == "Invitee Boat"
                assert data["fingerprint"] == invitee_fp

        # Verify peer was persisted
        peers = await storage.list_co_op_peers("coop1")
        assert len(peers) == 1
        assert peers[0]["boat_name"] == "Invitee Boat"

        # Verify membership file was written
        member_file = co_op_dir / f"{invitee_fp}.json"
        assert member_file.exists()


# ---------------------------------------------------------------------------
# Session Sharing API
# ---------------------------------------------------------------------------


class TestSessionSharingAPI:
    @pytest.fixture
    async def session_id(self, storage: Storage) -> int:
        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Test Race", "CYC Wed", 1, "2026-03-08", "2026-03-08T12:00:00Z", "race"),
        )
        await db.commit()
        return cur.lastrowid or 0

    @pytest.mark.asyncio
    async def test_get_sharing_empty(
        self,
        storage: Storage,
        session_id: int,
    ) -> None:
        async with _client(storage) as c:
            resp = await c.get(f"/api/sessions/{session_id}/sharing")
            assert resp.status_code == 200
            data = resp.json()
            assert data["sharing"] == []
            assert data["co_ops"] == []

    @pytest.mark.asyncio
    async def test_share_and_list(
        self,
        storage: Storage,
        session_id: int,
    ) -> None:
        await storage.save_co_op_membership(
            co_op_id="coop1",
            co_op_name="Fleet A",
            co_op_pub="pub1",
            membership_json="{}",
            role="admin",
        )
        async with _client(storage) as c:
            # Share
            resp = await c.post(
                f"/api/sessions/{session_id}/share",
                json={"co_op_id": "coop1"},
            )
            assert resp.status_code == 201

            # List — should show shared
            resp = await c.get(f"/api/sessions/{session_id}/sharing")
            data = resp.json()
            assert len(data["sharing"]) == 1
            assert data["co_ops"][0]["shared"] is True

    @pytest.mark.asyncio
    async def test_unshare(
        self,
        storage: Storage,
        session_id: int,
    ) -> None:
        await storage.save_co_op_membership(
            co_op_id="coop1",
            co_op_name="Fleet A",
            co_op_pub="pub1",
            membership_json="{}",
            role="admin",
        )
        await storage.share_session(session_id, "coop1")
        async with _client(storage) as c:
            resp = await c.delete(
                f"/api/sessions/{session_id}/share/coop1",
            )
            assert resp.status_code == 200

            # Verify unshared
            resp = await c.get(f"/api/sessions/{session_id}/sharing")
            assert resp.json()["sharing"] == []

    @pytest.mark.asyncio
    async def test_share_not_member(
        self,
        storage: Storage,
        session_id: int,
    ) -> None:
        async with _client(storage) as c:
            resp = await c.post(
                f"/api/sessions/{session_id}/share",
                json={"co_op_id": "nonexistent"},
            )
            assert resp.status_code == 404
