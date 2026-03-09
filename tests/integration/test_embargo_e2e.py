"""End-to-end embargo tests — temporal sharing enforcement.

Validates that embargoed sessions are visible in the session list (with status
"embargoed") but their track and results data cannot be fetched until the
embargo lifts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .conftest import Fleet


class TestEmbargoEnforcement:
    """Embargoed sessions are listed but data access is blocked."""

    @pytest.mark.asyncio
    async def test_embargoed_session_track_returns_403(self, fleet: Fleet) -> None:
        """Fetching track data for an embargoed session returns 403."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["embargo_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 403
        assert "embargo" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_embargoed_session_results_returns_403(self, fleet: Fleet) -> None:
        """Fetching results for an embargoed session returns 403."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["embargo_session_id"]
        path = f"/co-op/{co_op_id}/sessions/{session_id}/results"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_embargo_lifted_allows_access(self, fleet: Fleet) -> None:
        """After embargo lifts, track data becomes accessible."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["embargo_session_id"]

        # Manually set embargo_until to the past
        db = fleet.boat_a.storage._conn()
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await db.execute(
            "UPDATE session_sharing SET embargo_until = ? WHERE session_id = ? AND co_op_id = ?",
            (past, session_id, co_op_id),
        )
        await db.commit()

        # Now track fetch should succeed (or 404 if no data, but not 403)
        path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
        headers = fleet.boat_b.sign("GET", path)
        resp = await fleet.boat_a.client.get(path, headers=headers)
        # 200 (has data) or 404 (no instrument data seeded for this session) — but NOT 403
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_embargo_visible_in_session_list(self, fleet: Fleet) -> None:
        """Embargoed sessions appear in session list with status 'embargoed'."""
        co_op_id = fleet.boat_a.co_op_id
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)

        resp = await fleet.boat_a.client.get(path, headers=headers)
        sessions = resp.json()["sessions"]

        embargo_id = fleet.boat_a.resources["embargo_session_id"]
        embargoed = [s for s in sessions if s["session_id"] == embargo_id]
        assert len(embargoed) == 1
        assert embargoed[0]["status"] == "embargoed"
        assert "available_at" in embargoed[0]


class TestShareUnshare:
    """Session sharing lifecycle — share, verify, unshare, verify gone."""

    @pytest.mark.asyncio
    async def test_unshare_removes_access(self, fleet: Fleet) -> None:
        """After unsharing a session, it no longer appears in peer's list."""
        co_op_id = fleet.boat_a.co_op_id
        session_id = fleet.boat_a.resources["shared_session_id"]

        # Verify it's visible first
        path = f"/co-op/{co_op_id}/sessions"
        headers = fleet.boat_b.sign("GET", path)
        resp = await fleet.boat_a.client.get(path, headers=headers)
        ids_before = [s["session_id"] for s in resp.json()["sessions"]]
        assert session_id in ids_before

        # Unshare
        await fleet.boat_a.storage.unshare_session(session_id, co_op_id)

        # Verify it's gone (need fresh nonce — new sign call)
        headers2 = fleet.boat_b.sign("GET", path)
        resp2 = await fleet.boat_a.client.get(path, headers=headers2)
        ids_after = [s["session_id"] for s in resp2.json()["sessions"]]
        assert session_id not in ids_after

        # Track fetch also fails
        track_path = f"/co-op/{co_op_id}/sessions/{session_id}/track"
        headers3 = fleet.boat_b.sign("GET", track_path)
        resp3 = await fleet.boat_a.client.get(track_path, headers=headers3)
        assert resp3.status_code == 404
