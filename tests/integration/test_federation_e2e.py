"""End-to-end federation tests — co-op lifecycle, session listing, track fetch.

Tests exercise real Ed25519 signing across two in-memory boats.
Boat A is the admin/data-holder; Boat B queries A's endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .conftest import Fleet


class TestIdentity:
    """Public identity endpoint (no auth required)."""

    @pytest.mark.asyncio
    async def test_identity_returns_boat_card(self, fleet: Fleet) -> None:
        """GET /co-op/identity returns the boat's public identity."""
        resp = await fleet.boat_a.client.get("/co-op/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fingerprint"] == fleet.boat_a.fingerprint
        assert data["sail_number"] == "42"
        assert data["name"] == "Javelina"

    @pytest.mark.asyncio
    async def test_both_boats_have_identity(self, fleet: Fleet) -> None:
        resp_a = await fleet.boat_a.client.get("/co-op/identity")
        resp_b = await fleet.boat_b.client.get("/co-op/identity")
        assert resp_a.json()["fingerprint"] != resp_b.json()["fingerprint"]


class TestSessionList:
    """Authenticated session listing endpoint."""

    @pytest.mark.asyncio
    async def test_list_sessions_authenticated(self, fleet: Fleet) -> None:
        """Boat B can list sessions shared by Boat A."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 200

        data = resp.json()
        sessions = data["sessions"]
        # Should see the shared session and the embargoed session
        assert len(sessions) >= 2

        available = [s for s in sessions if s["status"] == "available"]
        embargoed = [s for s in sessions if s["status"] == "embargoed"]
        assert len(available) >= 1
        assert len(embargoed) >= 1

        # The private session should NOT appear
        private_id = fleet.boat_a.resources["private_session_id"]
        session_ids = [s.get("session_id") for s in sessions]
        assert private_id not in session_ids

    @pytest.mark.asyncio
    async def test_embargoed_session_shows_available_at(self, fleet: Fleet) -> None:
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        embargoed = [s for s in resp.json()["sessions"] if s["status"] == "embargoed"]
        assert embargoed[0]["available_at"]  # has a timestamp


class TestTrackFetch:
    """Authenticated track data fetch."""

    @pytest.mark.asyncio
    async def test_fetch_shared_track(self, fleet: Fleet) -> None:
        """Boat B can fetch track data for a shared session on Boat A."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 200

        data = resp.json()
        assert data["count"] > 0
        track = data["track"]
        assert len(track) > 0

        # Verify all expected fields are present
        point = track[0]
        assert "timestamp" in point
        assert "LAT" in point
        assert "LON" in point
        assert "HDG" in point
        assert "BSP" in point
        assert "COG" in point
        assert "SOG" in point
        assert "TWS" in point
        assert "TWA" in point
        assert "AWS" in point
        assert "AWA" in point

    @pytest.mark.asyncio
    async def test_track_contains_no_pii_fields(self, fleet: Fleet) -> None:
        """Track data must not contain PII or private fields."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        data = resp.json()

        # These fields must never appear in co-op track data
        forbidden_fields = {"notes", "audio", "transcript", "crew", "sails", "video"}
        for point in data["track"]:
            for field in forbidden_fields:
                assert field not in point, f"PII field '{field}' leaked in track data"

    @pytest.mark.asyncio
    async def test_fetch_unshared_session_returns_404(self, fleet: Fleet) -> None:
        """Fetching a session not shared with the co-op returns 404."""
        co_op_id = fleet.boat_a.co_op_id
        private_id = fleet.boat_a.resources["private_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{private_id}/track"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 404


class TestSessionResults:
    """Race results endpoint."""

    @pytest.mark.asyncio
    async def test_results_excludes_notes(self, fleet: Fleet) -> None:
        """Race results must not include notes (PII per data licensing)."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/results"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 200

        for result in resp.json().get("results", []):
            assert "notes" not in result, "Notes leaked in co-op results"


class TestAuditLogging:
    """Verify that peer access creates audit trail entries."""

    @pytest.mark.asyncio
    async def test_session_list_creates_audit_entry(self, fleet: Fleet) -> None:
        """Accessing sessions creates an audit log entry on the serving boat."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        await fleet.boat_a.client.get(path, headers=headers)

        # Check audit log on boat A
        db = fleet.boat_a.storage._conn()
        cur = await db.execute(
            "SELECT * FROM co_op_audit WHERE accessor_fp = ?",
            (fleet.boat_b.fingerprint,),
        )
        rows = await cur.fetchall()
        assert len(rows) >= 1
        assert rows[0]["action"] == "coop.peer.sessions"

    @pytest.mark.asyncio
    async def test_track_fetch_creates_audit_with_points_count(self, fleet: Fleet) -> None:
        """Track fetch audit entry includes the number of data points served."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
        headers = fleet.boat_b.sign("GET", path)

        await fleet.boat_a.client.get(path, headers=headers)

        db = fleet.boat_a.storage._conn()
        cur = await db.execute(
            "SELECT * FROM co_op_audit WHERE action = 'coop.peer.track'",
        )
        rows = await cur.fetchall()
        assert len(rows) >= 1
        assert rows[0]["points_count"] is not None
        assert rows[0]["points_count"] > 0
