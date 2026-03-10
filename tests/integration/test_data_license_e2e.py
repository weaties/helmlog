"""End-to-end data licensing tests.

Validates that the peer API enforces the data licensing policy:
- Only allowlisted instrument fields are served
- PII is never exposed (audio, transcripts, notes, crew, sails, video)
- Co-op data is view-only (no bulk export endpoints)
- Audit trail records all access
- Private sessions are invisible to peers
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from helmlog.peer_api import SHARED_TRACK_FIELDS

if TYPE_CHECKING:
    from .conftest import Fleet


class TestDataAllowlist:
    """Only explicitly allowlisted fields appear in track data."""

    @pytest.mark.asyncio
    async def test_track_fields_match_allowlist(self, fleet: Fleet) -> None:
        """Every field in track data (except timestamp) is in the allowlist."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 200

        track = resp.json()["track"]
        assert len(track) > 0

        for point in track:
            data_fields = {k for k in point if k != "timestamp"}
            assert data_fields <= SHARED_TRACK_FIELDS, (
                f"Unexpected fields in track: {data_fields - SHARED_TRACK_FIELDS}"
            )


class TestPIIProtection:
    """PII and private data must never leak through peer API."""

    @pytest.mark.asyncio
    async def test_no_audio_in_session_list(self, fleet: Fleet) -> None:
        """Session list does not include audio recording references."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        for session in resp.json()["sessions"]:
            for pii_field in (
                "audio",
                "audio_path",
                "transcript",
                "crew",
                "sails",
                "video",
                "youtube",
                "notes",
            ):
                assert pii_field not in session, f"PII field '{pii_field}' in session list"

    @pytest.mark.asyncio
    async def test_results_strip_notes(self, fleet: Fleet) -> None:
        """Race results served to peers must not contain notes."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/results"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 200
        for result in resp.json().get("results", []):
            assert "notes" not in result


class TestPrivateSessionIsolation:
    """Private (unshared) sessions are completely invisible to peers."""

    @pytest.mark.asyncio
    async def test_private_session_not_in_list(self, fleet: Fleet) -> None:
        """Unshared sessions do not appear in the co-op session list."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        private_id = fleet.boat_a.resources["private_session_id"]
        session_ids = [s["session_id"] for s in resp.json()["sessions"]]
        assert private_id not in session_ids

    @pytest.mark.asyncio
    async def test_private_session_track_returns_404(self, fleet: Fleet) -> None:
        """Direct track fetch for an unshared session returns 404."""
        co_op_id = fleet.boat_a.co_op_id
        private_id = fleet.boat_a.resources["private_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{private_id}/track"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_private_session_results_returns_404(self, fleet: Fleet) -> None:
        """Direct results fetch for an unshared session returns 404."""
        co_op_id = fleet.boat_a.co_op_id
        private_id = fleet.boat_a.resources["private_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{private_id}/results"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 404


class TestAuditCompleteness:
    """Every peer data access is audit-logged."""

    @pytest.mark.asyncio
    async def test_all_endpoints_create_audit_entries(self, fleet: Fleet) -> None:
        """Accessing sessions, track, and results all create audit entries."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]

        # Hit all three endpoints
        for endpoint in (
            "sessions",
            f"sessions/{session_id}/track",
            f"sessions/{session_id}/results",
        ):
            path = f"/co-op/{co_op_id}/{endpoint}"
            headers = fleet.boat_b.sign("GET", path)
            await fleet.boat_a.client.get(path, headers=headers)

        # Check audit entries
        db = fleet.boat_a.storage._conn()
        cur = await db.execute(
            "SELECT action FROM co_op_audit WHERE accessor_fp = ? ORDER BY id",
            (fleet.boat_b.fingerprint,),
        )
        actions = [r["action"] for r in await cur.fetchall()]
        assert "coop.peer.sessions" in actions
        assert "coop.peer.track" in actions
        assert "coop.peer.results" in actions
