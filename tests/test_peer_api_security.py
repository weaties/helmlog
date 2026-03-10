"""Security and data-licensing regression tests for the peer API.

These tests verify:
- PII fields (notes) are stripped from results
- Embargo enforcement on track and results endpoints
- Co-op audit logging to co_op_audit table
- Nonce replay protection via SQLite persistence
- Stale timestamp rejection
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.federation import (
    _pub_key_to_base64,
    fingerprint_from_pub_bytes,
    generate_keypair,
)
from helmlog.peer_auth import sign_request
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


def _client(storage: Storage) -> httpx.AsyncClient:
    app = create_app(storage)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


@pytest.fixture
async def peer_setup(storage: Storage) -> dict:
    """Set up a co-op with an admin and a peer, a shared session, and race results."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    # Admin (this boat)
    admin_priv, admin_pub = generate_keypair()
    admin_pub_b64 = _pub_key_to_base64(admin_pub)

    # Peer (remote boat)
    peer_priv, peer_pub = generate_keypair()
    peer_pub_b64 = _pub_key_to_base64(peer_pub)
    peer_fp = fingerprint_from_pub_bytes(
        peer_pub.public_bytes(Encoding.Raw, PublicFormat.Raw),
    )

    # Store admin identity
    await storage.save_boat_identity(
        pub_key=admin_pub_b64,
        fingerprint="admin_fp",
        sail_number="1",
        boat_name="Admin Boat",
    )

    # Create co-op membership
    await storage.save_co_op_membership(
        co_op_id="coop1",
        co_op_name="Test Fleet",
        co_op_pub=admin_pub_b64,
        membership_json="{}",
        role="admin",
    )

    # Register peer
    await storage.save_co_op_peer(
        co_op_id="coop1",
        boat_pub=peer_pub_b64,
        fingerprint=peer_fp,
        membership_json="{}",
        sail_number="42",
        boat_name="Peer Boat",
    )

    # Create a session
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc,"
        " end_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Test Race",
            "CYC Wed",
            1,
            "2026-03-08",
            "2026-03-08T12:00:00",
            "2026-03-08T12:30:00",
            "race",
        ),
    )
    await db.commit()
    session_id = cur.lastrowid or 0

    # Share the session (no embargo)
    await storage.share_session(session_id, "coop1")

    # Add race results with notes (PII)
    # First create a boat entry for results
    cur = await db.execute(
        "INSERT INTO boats (sail_number, name, class) VALUES (?, ?, ?)",
        ("42", "Peer Boat", "J105"),
    )
    await db.commit()
    boat_id = cur.lastrowid or 0

    await storage.upsert_race_result(
        race_id=session_id,
        place=1,
        boat_id=boat_id,
        finish_time="12:30:00",
        notes="CONFIDENTIAL: Helm was upset about the start",
    )

    return {
        "peer_priv": peer_priv,
        "peer_fp": peer_fp,
        "session_id": session_id,
    }


class TestResultsNotesFiltered:
    """Verify that PII (notes field) is stripped from peer results responses."""

    @pytest.mark.asyncio
    async def test_notes_excluded_from_results(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        path = f"/co-op/coop1/sessions/{peer_setup['session_id']}/results"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            results = data["results"]
            assert len(results) >= 1
            for r in results:
                assert "notes" not in r, "notes field must be stripped from peer results (PII)"
                # Verify other fields are still present
                assert "place" in r
                assert "sail_number" in r


class TestEmbargoEnforcement:
    """Verify embargo is enforced on both track and results endpoints."""

    @pytest.fixture
    async def embargoed_setup(self, storage: Storage, peer_setup: dict) -> dict:
        """Add an embargo to the shared session."""
        session_id = peer_setup["session_id"]
        future = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
        await storage.unshare_session(session_id, "coop1")
        await storage.share_session(session_id, "coop1", embargo_until=future)
        return {**peer_setup, "embargo_until": future}

    @pytest.mark.asyncio
    async def test_embargoed_results_returns_403(
        self,
        storage: Storage,
        embargoed_setup: dict,
    ) -> None:
        path = f"/co-op/coop1/sessions/{embargoed_setup['session_id']}/results"
        headers = sign_request(
            embargoed_setup["peer_priv"],
            embargoed_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 403
            assert "embargo" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_embargoed_track_returns_403(
        self,
        storage: Storage,
        embargoed_setup: dict,
    ) -> None:
        path = f"/co-op/coop1/sessions/{embargoed_setup['session_id']}/track"
        headers = sign_request(
            embargoed_setup["peer_priv"],
            embargoed_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 403
            assert "embargo" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_expired_embargo_allows_results(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        """An embargo in the past should not block access."""
        session_id = peer_setup["session_id"]
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await storage.unshare_session(session_id, "coop1")
        await storage.share_session(session_id, "coop1", embargo_until=past)

        path = f"/co-op/coop1/sessions/{session_id}/results"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200


class TestCoOpAuditLogging:
    """Verify peer data access is recorded in the co_op_audit table."""

    @pytest.mark.asyncio
    async def test_sessions_list_audited(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        path = "/co-op/coop1/sessions"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200

        # Check co_op_audit table has an entry
        db = storage._conn()
        cur = await db.execute(
            "SELECT co_op_id, accessor_fp, action FROM co_op_audit"
            " WHERE action = 'coop.peer.sessions'"
        )
        row = await cur.fetchone()
        assert row is not None, "Session list should be audited in co_op_audit"
        assert row["co_op_id"] == "coop1"
        assert row["accessor_fp"] == peer_setup["peer_fp"]

    @pytest.mark.asyncio
    async def test_results_audited(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        path = f"/co-op/coop1/sessions/{peer_setup['session_id']}/results"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200

        db = storage._conn()
        cur = await db.execute(
            "SELECT co_op_id, accessor_fp, action, resource FROM co_op_audit"
            " WHERE action = 'coop.peer.results'"
        )
        row = await cur.fetchone()
        assert row is not None, "Results access should be audited in co_op_audit"
        assert row["accessor_fp"] == peer_setup["peer_fp"]


class TestNoncePersistence:
    """Verify nonces are persisted to SQLite for replay protection."""

    @pytest.mark.asyncio
    async def test_nonce_saved_to_db(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        path = "/co-op/coop1/sessions"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200

        # Verify nonce was saved to request_nonces table
        db = storage._conn()
        cur = await db.execute("SELECT COUNT(*) FROM request_nonces")
        row = await cur.fetchone()
        assert row[0] >= 1, "Nonce should be persisted to request_nonces table"


class TestStaleTimestampRejection:
    """Verify requests with old timestamps are rejected."""

    @pytest.mark.asyncio
    async def test_old_timestamp_rejected(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        old_ts = (datetime.now(UTC) - timedelta(minutes=25)).isoformat()
        path = "/co-op/coop1/sessions"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
            timestamp=old_ts,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 401, "Requests >20min old should be rejected"


class TestResultsEndpointBasic:
    """Basic results endpoint tests."""

    @pytest.mark.asyncio
    async def test_not_shared_returns_404(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        path = "/co-op/coop1/sessions/99999/results"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_shared_results_returned(
        self,
        storage: Storage,
        peer_setup: dict,
    ) -> None:
        path = f"/co-op/coop1/sessions/{peer_setup['session_id']}/results"
        headers = sign_request(
            peer_setup["peer_priv"],
            peer_setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["results"]) >= 1
            assert data["results"][0]["place"] == 1
