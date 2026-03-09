"""Tests for federation-related storage methods in helmlog.storage."""

from __future__ import annotations

import pytest

from helmlog.storage import Storage, StorageConfig


@pytest.fixture
async def storage(tmp_path: object) -> Storage:  # type: ignore[override]
    """Create an in-memory storage instance with all migrations applied."""
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s  # type: ignore[misc]
    await s.close()


class TestBoatIdentity:
    @pytest.mark.asyncio
    async def test_save_and_get(self, storage: Storage) -> None:
        await storage.save_boat_identity(
            pub_key="abc123",
            fingerprint="fp123",
            sail_number="69",
            boat_name="Javelina",
        )
        identity = await storage.get_boat_identity()
        assert identity is not None
        assert identity["pub_key"] == "abc123"
        assert identity["fingerprint"] == "fp123"
        assert identity["sail_number"] == "69"
        assert identity["boat_name"] == "Javelina"
        assert identity["created_at"] is not None

    @pytest.mark.asyncio
    async def test_upsert(self, storage: Storage) -> None:
        await storage.save_boat_identity("a", "f1", "1", "Boat1")
        await storage.save_boat_identity("b", "f2", "2", "Boat2")
        identity = await storage.get_boat_identity()
        assert identity is not None
        assert identity["pub_key"] == "b"
        assert identity["boat_name"] == "Boat2"

    @pytest.mark.asyncio
    async def test_get_empty(self, storage: Storage) -> None:
        assert await storage.get_boat_identity() is None


class TestCoOpMemberships:
    @pytest.mark.asyncio
    async def test_save_and_list(self, storage: Storage) -> None:
        await storage.save_co_op_membership(
            co_op_id="coop1",
            co_op_name="Test Fleet",
            co_op_pub="pubkey1",
            membership_json='{"test": true}',
            role="admin",
        )
        memberships = await storage.list_co_op_memberships()
        assert len(memberships) == 1
        assert memberships[0]["co_op_id"] == "coop1"
        assert memberships[0]["co_op_name"] == "Test Fleet"
        assert memberships[0]["role"] == "admin"
        assert memberships[0]["status"] == "active"

    @pytest.mark.asyncio
    async def test_get_specific(self, storage: Storage) -> None:
        await storage.save_co_op_membership(
            co_op_id="coop1", co_op_name="Fleet A",
            co_op_pub="pub1", membership_json="{}",
        )
        result = await storage.get_co_op_membership("coop1")
        assert result is not None
        assert result["co_op_name"] == "Fleet A"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, storage: Storage) -> None:
        assert await storage.get_co_op_membership("nope") is None

    @pytest.mark.asyncio
    async def test_upsert_reactivates(self, storage: Storage) -> None:
        await storage.save_co_op_membership(
            co_op_id="coop1", co_op_name="Fleet",
            co_op_pub="pub", membership_json="{}",
        )
        # Re-save should update
        await storage.save_co_op_membership(
            co_op_id="coop1", co_op_name="Fleet Updated",
            co_op_pub="pub2", membership_json='{"v": 2}',
        )
        result = await storage.get_co_op_membership("coop1")
        assert result is not None
        assert result["co_op_name"] == "Fleet Updated"
        assert result["status"] == "active"


class TestSessionSharing:
    @pytest.fixture
    async def session_id(self, storage: Storage) -> int:
        """Create a race session and return its ID."""
        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("Test Race", "CYC Wednesday", 1, "2026-03-08", "2026-03-08T12:00:00Z", "race"),
        )
        await db.commit()
        return cur.lastrowid or 0

    @pytest.mark.asyncio
    async def test_share_and_get(self, storage: Storage, session_id: int) -> None:
        await storage.save_co_op_membership(
            co_op_id="coop1", co_op_name="Fleet",
            co_op_pub="pub", membership_json="{}",
        )
        await storage.share_session(session_id, "coop1", event_name="CYC Wednesday")
        shares = await storage.get_session_sharing(session_id)
        assert len(shares) == 1
        assert shares[0]["co_op_id"] == "coop1"
        assert shares[0]["event_name"] == "CYC Wednesday"
        assert shares[0]["co_op_name"] == "Fleet"

    @pytest.mark.asyncio
    async def test_is_shared(self, storage: Storage, session_id: int) -> None:
        assert not await storage.is_session_shared(session_id, "coop1")
        await storage.share_session(session_id, "coop1")
        assert await storage.is_session_shared(session_id, "coop1")

    @pytest.mark.asyncio
    async def test_unshare(self, storage: Storage, session_id: int) -> None:
        await storage.share_session(session_id, "coop1")
        assert await storage.is_session_shared(session_id, "coop1")
        result = await storage.unshare_session(session_id, "coop1")
        assert result is True
        assert not await storage.is_session_shared(session_id, "coop1")

    @pytest.mark.asyncio
    async def test_unshare_nonexistent(self, storage: Storage, session_id: int) -> None:
        result = await storage.unshare_session(session_id, "nope")
        assert result is False

    @pytest.mark.asyncio
    async def test_share_with_embargo(self, storage: Storage, session_id: int) -> None:
        await storage.share_session(
            session_id, "coop1", embargo_until="2026-04-01T00:00:00Z",
        )
        shares = await storage.get_session_sharing(session_id)
        assert shares[0]["embargo_until"] == "2026-04-01T00:00:00Z"


class TestCoOpPeers:
    @pytest.mark.asyncio
    async def test_save_and_list(self, storage: Storage) -> None:
        await storage.save_co_op_peer(
            co_op_id="coop1",
            boat_pub="pub1",
            fingerprint="fp1",
            membership_json='{"role": "member"}',
            sail_number="42",
            boat_name="Blackhawk",
            tailscale_ip="100.64.0.1",
        )
        peers = await storage.list_co_op_peers("coop1")
        assert len(peers) == 1
        assert peers[0]["fingerprint"] == "fp1"
        assert peers[0]["boat_name"] == "Blackhawk"
        assert peers[0]["tailscale_ip"] == "100.64.0.1"

    @pytest.mark.asyncio
    async def test_upsert_peer(self, storage: Storage) -> None:
        await storage.save_co_op_peer(
            co_op_id="coop1", boat_pub="pub1", fingerprint="fp1",
            membership_json="{}", boat_name="OldName",
        )
        await storage.save_co_op_peer(
            co_op_id="coop1", boat_pub="pub1", fingerprint="fp1",
            membership_json='{"v":2}', boat_name="NewName",
        )
        peers = await storage.list_co_op_peers("coop1")
        assert len(peers) == 1
        assert peers[0]["boat_name"] == "NewName"

    @pytest.mark.asyncio
    async def test_list_empty(self, storage: Storage) -> None:
        assert await storage.list_co_op_peers("nope") == []
