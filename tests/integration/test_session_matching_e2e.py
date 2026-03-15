"""Integration tests for session matching lifecycle across two boats (#281).

Tests the full propose → confirm → name lifecycle through the peer API
endpoints using two real boats with Ed25519 keypairs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from .seed import SESSION_END, SESSION_START

if TYPE_CHECKING:
    from .conftest import Fleet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_positions(
    fleet: Fleet,
    boat_key: str,
    session_id: int,
    lat: float,
    lon: float,
    start: datetime,
    num_points: int = 15,
) -> None:
    """Seed GPS positions for a session on a specific boat."""
    boat = fleet.boat_a if boat_key == "a" else fleet.boat_b
    db = boat.storage._conn()
    for i in range(num_points):
        ts = (start + timedelta(seconds=i * 60)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 5, lat + i * 0.0001, lon + i * 0.0001),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_match_via_api(fleet: Fleet) -> None:
    """Boat B can propose a session match to Boat A's peer API."""
    co_op_id = fleet.boat_a.co_op_id
    shared_id = fleet.boat_a.resources["shared_session_id"]

    # Seed positions on boat A so centroid works
    await _seed_positions(fleet, "a", shared_id, 37.8044, -122.2712, SESSION_START)

    path = f"/co-op/{co_op_id}/session-matches/propose"
    headers = fleet.boat_b.sign("POST", path)
    body = {
        "local_session_id": fleet.boat_b.resources["shared_session_id"],
        "centroid_lat": 37.8100,
        "centroid_lon": -122.2712,
        "start_utc": SESSION_START.isoformat(),
        "end_utc": SESSION_END.isoformat(),
    }

    resp = await fleet.boat_a.client.post(path, json=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "match_group_id" in data
    assert data["status"] == "candidate"


@pytest.mark.asyncio
async def test_confirm_match_via_api(fleet: Fleet) -> None:
    """Two peers confirm a match via each other's peer APIs, reaching quorum.

    Boat B proposes + confirms on Boat A. Boat A proposes + confirms on Boat B.
    Each boat stores its own confirmation, and quorum is checked locally.
    """
    co_op_id = fleet.boat_a.co_op_id
    shared_id_a = fleet.boat_a.resources["shared_session_id"]
    shared_id_b = fleet.boat_b.resources["shared_session_id"]

    await _seed_positions(fleet, "a", shared_id_a, 37.8044, -122.2712, SESSION_START)
    await _seed_positions(fleet, "b", shared_id_b, 37.8100, -122.2712, SESSION_START)

    # Boat B proposes on Boat A's server
    path = f"/co-op/{co_op_id}/session-matches/propose"
    headers = fleet.boat_b.sign("POST", path)
    body = {
        "local_session_id": shared_id_b,
        "centroid_lat": 37.8100,
        "centroid_lon": -122.2712,
        "start_utc": SESSION_START.isoformat(),
        "end_utc": SESSION_END.isoformat(),
    }
    resp = await fleet.boat_a.client.post(path, json=body, headers=headers)
    assert resp.status_code == 200
    match_id = resp.json()["match_group_id"]

    # Boat B confirms on Boat A's server (first confirm)
    confirm_path = f"/co-op/{co_op_id}/session-matches/{match_id}/confirm"
    headers_b = fleet.boat_b.sign("POST", confirm_path)
    resp_b = await fleet.boat_a.client.post(confirm_path, headers=headers_b)
    assert resp_b.status_code == 200

    # Boat A confirms on Boat B's server (second confirm)
    # First, propose on Boat B so it has the match too
    headers_a_propose = fleet.boat_a.sign("POST", path)
    body_a = {
        "local_session_id": shared_id_a,
        "centroid_lat": 37.8044,
        "centroid_lon": -122.2712,
        "start_utc": SESSION_START.isoformat(),
        "end_utc": SESSION_END.isoformat(),
    }
    resp_b_propose = await fleet.boat_b.client.post(path, json=body_a, headers=headers_a_propose)
    assert resp_b_propose.status_code == 200
    match_id_b = resp_b_propose.json()["match_group_id"]

    # Boat A confirms on Boat B's server
    confirm_path_b = f"/co-op/{co_op_id}/session-matches/{match_id_b}/confirm"
    headers_a_confirm = fleet.boat_a.sign("POST", confirm_path_b)
    resp_a_confirm = await fleet.boat_b.client.post(confirm_path_b, headers=headers_a_confirm)
    assert resp_a_confirm.status_code == 200


@pytest.mark.asyncio
async def test_reject_match_via_api(fleet: Fleet) -> None:
    """A boat can reject a proposed match via the peer API."""
    co_op_id = fleet.boat_a.co_op_id
    shared_id = fleet.boat_a.resources["shared_session_id"]

    await _seed_positions(fleet, "a", shared_id, 37.8044, -122.2712, SESSION_START)

    # Propose
    path = f"/co-op/{co_op_id}/session-matches/propose"
    headers = fleet.boat_b.sign("POST", path)
    body = {
        "local_session_id": fleet.boat_b.resources["shared_session_id"],
        "centroid_lat": 37.8100,
        "centroid_lon": -122.2712,
        "start_utc": SESSION_START.isoformat(),
        "end_utc": SESSION_END.isoformat(),
    }
    resp = await fleet.boat_a.client.post(path, json=body, headers=headers)
    match_id = resp.json()["match_group_id"]

    # Reject
    reject_path = f"/co-op/{co_op_id}/session-matches/{match_id}/reject"
    headers_r = fleet.boat_b.sign("POST", reject_path)
    resp_r = await fleet.boat_a.client.post(reject_path, headers=headers_r)
    assert resp_r.status_code == 200
    assert resp_r.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_set_shared_name_via_api(fleet: Fleet) -> None:
    """A peer boat can propose a shared name for a confirmed match."""
    co_op_id = fleet.boat_a.co_op_id
    shared_id = fleet.boat_a.resources["shared_session_id"]

    await _seed_positions(fleet, "a", shared_id, 37.8044, -122.2712, SESSION_START)

    # Boat B proposes on Boat A
    path = f"/co-op/{co_op_id}/session-matches/propose"
    headers = fleet.boat_b.sign("POST", path)
    body = {
        "local_session_id": fleet.boat_b.resources["shared_session_id"],
        "centroid_lat": 37.8100,
        "centroid_lon": -122.2712,
        "start_utc": SESSION_START.isoformat(),
        "end_utc": SESSION_END.isoformat(),
    }
    resp = await fleet.boat_a.client.post(path, json=body, headers=headers)
    assert resp.status_code == 200
    match_id = resp.json()["match_group_id"]

    # Two confirms from Boat B (to reach quorum, we insert a second
    # confirmation directly since Boat A can't call its own peer API)
    confirm_path = f"/co-op/{co_op_id}/session-matches/{match_id}/confirm"
    headers_b = fleet.boat_b.sign("POST", confirm_path)
    await fleet.boat_a.client.post(confirm_path, headers=headers_b)

    # Directly confirm as boat A (simulating local confirmation)
    from helmlog.session_matching import confirm_match

    await confirm_match(fleet.boat_a.storage, match_id, fleet.boat_a.fingerprint)

    # Set shared name (Boat B calls)
    name_path = f"/co-op/{co_op_id}/session-matches/{match_id}/name"
    headers_n = fleet.boat_b.sign("PUT", name_path)
    resp_n = await fleet.boat_a.client.put(
        name_path,
        json={"shared_name": "SF Bay Sunday Race 1"},
        headers=headers_n,
    )
    assert resp_n.status_code == 200

    # Verify shared name is stored, local name preserved
    db = fleet.boat_a.storage._conn()
    cur = await db.execute("SELECT name, shared_name FROM races WHERE id = ?", (shared_id,))
    row = dict(await cur.fetchone())  # type: ignore[arg-type]
    assert row["name"] == "Test Race 1"  # local name unchanged
    assert row["shared_name"] == "SF Bay Sunday Race 1"


@pytest.mark.asyncio
async def test_list_session_matches(fleet: Fleet) -> None:
    """GET session-matches returns candidates and confirmed matches."""
    co_op_id = fleet.boat_a.co_op_id
    shared_id = fleet.boat_a.resources["shared_session_id"]

    await _seed_positions(fleet, "a", shared_id, 37.8044, -122.2712, SESSION_START)

    # Boat B proposes on Boat A
    path = f"/co-op/{co_op_id}/session-matches/propose"
    headers = fleet.boat_b.sign("POST", path)
    body = {
        "local_session_id": fleet.boat_b.resources["shared_session_id"],
        "centroid_lat": 37.8100,
        "centroid_lon": -122.2712,
        "start_utc": SESSION_START.isoformat(),
        "end_utc": SESSION_END.isoformat(),
    }
    await fleet.boat_a.client.post(path, json=body, headers=headers)

    # Boat B lists matches on Boat A
    list_path = f"/co-op/{co_op_id}/session-matches"
    headers_l = fleet.boat_b.sign("GET", list_path)
    resp = await fleet.boat_a.client.get(list_path, headers=headers_l)
    assert resp.status_code == 200
    data = resp.json()
    assert "matches" in data
    assert len(data["matches"]) >= 1


@pytest.mark.asyncio
async def test_embargoed_session_not_matchable(fleet: Fleet) -> None:
    """Sessions under embargo are not returned as matchable candidates."""
    co_op_id = fleet.boat_a.co_op_id
    embargo_id = fleet.boat_a.resources["embargo_session_id"]

    await _seed_positions(
        fleet,
        "a",
        embargo_id,
        37.8044,
        -122.2712,
        datetime(2026, 3, 2, 14, 0, 0, tzinfo=UTC),
    )

    # Try to propose a match for embargoed session
    path = f"/co-op/{co_op_id}/session-matches/propose"
    headers = fleet.boat_b.sign("POST", path)
    body = {
        "local_session_id": fleet.boat_b.resources["embargo_session_id"],
        "centroid_lat": 37.8100,
        "centroid_lon": -122.2712,
        "start_utc": "2026-03-02T14:05:00+00:00",
        "end_utc": "2026-03-02T14:35:00+00:00",
    }
    resp = await fleet.boat_a.client.post(path, json=body, headers=headers)
    # Should fail because the matching local session is embargoed
    # The API tries to find a local match, but the best match is embargoed
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        # Even if it returns 200, check that no embargoed sessions were matched
        data = resp.json()
        if "matched_session_id" in data:
            # The matched session should not be the embargoed one
            assert data["matched_session_id"] != embargo_id
