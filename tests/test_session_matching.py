"""Tests for session matching — proximity-based session pairing across co-op boats (#281)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from helmlog.session_matching import (
    compute_session_centroid,
    confirm_match,
    expire_stale_candidates,
    find_proximity_candidates,
    propose_match,
    reject_match,
    synthesize_name,
)
from helmlog.storage import Storage, StorageConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CO_OP_ID = "test-coop-abc"
BOAT_A_FP = "fingerprint-aaa"
BOAT_B_FP = "fingerprint-bbb"

SESSION_START = datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC)
SESSION_END = datetime(2026, 3, 1, 14, 30, 0, tzinfo=UTC)

# Overlapping session on peer (starts 5 min later, ends 5 min later)
PEER_START = datetime(2026, 3, 1, 14, 5, 0, tzinfo=UTC)
PEER_END = datetime(2026, 3, 1, 14, 35, 0, tzinfo=UTC)

# Non-overlapping session on peer
FAR_START = datetime(2026, 3, 5, 14, 0, 0, tzinfo=UTC)
FAR_END = datetime(2026, 3, 5, 14, 30, 0, tzinfo=UTC)

# SF Bay coordinates
SF_LAT, SF_LON = 37.8044, -122.2712
# Point 1 NM away (~0.0167 degrees latitude)
NEAR_LAT, NEAR_LON = 37.8200, -122.2712
# Point 5 NM away (well outside 2 NM radius)
FAR_LAT, FAR_LON = 37.8900, -122.2712


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


async def _seed_co_op(storage: Storage) -> None:
    """Seed minimal co-op data for testing."""
    db = storage._conn()
    # Create co-op membership
    await db.execute(
        "INSERT OR IGNORE INTO co_op_memberships"
        " (co_op_id, co_op_name, co_op_pub, membership_json, role, status)"
        " VALUES (?, ?, ?, '{}', 'admin', 'active')",
        (CO_OP_ID, "Test Co-op", "pubkey123"),
    )
    # Create peer
    await db.execute(
        "INSERT OR IGNORE INTO co_op_peers"
        " (co_op_id, boat_pub, fingerprint, sail_number, boat_name,"
        "  tailscale_ip, membership_json)"
        " VALUES (?, 'pubB', ?, '69', 'Peer Boat', '127.0.0.1', '{}')",
        (CO_OP_ID, BOAT_B_FP),
    )
    await db.commit()


async def _create_session_with_positions(
    storage: Storage,
    name: str,
    start: datetime,
    end: datetime,
    lat: float,
    lon: float,
    *,
    num_points: int = 15,
    share_with_co_op: str | None = None,
    embargo_until: str | None = None,
) -> int:
    """Create a session with GPS positions and optionally share it."""
    race = await storage.start_race(
        event="Test Event",
        start_utc=start,
        date_str=start.strftime("%Y-%m-%d"),
        race_num=1,
        name=name,
        session_type="race",
    )
    await storage.end_race(race.id, end)

    # Seed position data
    db = storage._conn()
    for i in range(num_points):
        ts = (start + timedelta(seconds=i * 60)).isoformat()
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 5, lat + i * 0.0001, lon + i * 0.0001),
        )
    await db.commit()

    if share_with_co_op:
        await storage.share_session(
            race.id,
            share_with_co_op,
            embargo_until=embargo_until,
        )

    return race.id


# ---------------------------------------------------------------------------
# Centroid tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_centroid(storage: Storage) -> None:
    """Centroid is the average lat/lon of all GPS positions in a session."""
    sid = await _create_session_with_positions(
        storage, "Centroid Test", SESSION_START, SESSION_END, SF_LAT, SF_LON, num_points=10
    )
    lat, lon = await compute_session_centroid(storage, sid)
    # Positions go from SF_LAT to SF_LAT + 9*0.0001
    expected_lat = SF_LAT + 4.5 * 0.0001
    expected_lon = SF_LON + 4.5 * 0.0001
    assert abs(lat - expected_lat) < 0.001
    assert abs(lon - expected_lon) < 0.001


@pytest.mark.asyncio
async def test_centroid_no_positions(storage: Storage) -> None:
    """Centroid returns (0, 0) for a session with no GPS data."""
    sid = await _create_session_with_positions(
        storage, "Empty Test", SESSION_START, SESSION_END, SF_LAT, SF_LON, num_points=0
    )
    lat, lon = await compute_session_centroid(storage, sid)
    assert lat == 0.0
    assert lon == 0.0


# ---------------------------------------------------------------------------
# Proximity matching tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proximity_match_within_radius_and_time(storage: Storage) -> None:
    """Sessions within 2 NM and 15 min overlap produce a candidate."""
    await _seed_co_op(storage)

    # Local session
    local_id = await _create_session_with_positions(
        storage,
        "Local Race",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
    )

    # Peer session (close in space and time)
    peer_session = {
        "session_id": 999,
        "name": "Peer Race",
        "start_utc": PEER_START.isoformat(),
        "end_utc": PEER_END.isoformat(),
        "centroid_lat": NEAR_LAT,
        "centroid_lon": NEAR_LON,
        "fingerprint": BOAT_B_FP,
        "status": "available",
    }

    candidates = await find_proximity_candidates(storage, CO_OP_ID, peer_sessions=[peer_session])
    assert len(candidates) >= 1
    match = candidates[0]
    assert match.local_session_id == local_id
    assert match.peer_session_id == 999


@pytest.mark.asyncio
async def test_no_match_outside_radius(storage: Storage) -> None:
    """Sessions beyond 2 NM produce no candidate."""
    await _seed_co_op(storage)

    await _create_session_with_positions(
        storage,
        "Local Race Far",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
    )

    peer_session = {
        "session_id": 888,
        "name": "Far Peer Race",
        "start_utc": PEER_START.isoformat(),
        "end_utc": PEER_END.isoformat(),
        "centroid_lat": FAR_LAT,
        "centroid_lon": FAR_LON,
        "fingerprint": BOAT_B_FP,
        "status": "available",
    }

    candidates = await find_proximity_candidates(storage, CO_OP_ID, peer_sessions=[peer_session])
    assert len(candidates) == 0


@pytest.mark.asyncio
async def test_no_match_outside_time_window(storage: Storage) -> None:
    """Sessions beyond +-15 min produce no candidate."""
    await _seed_co_op(storage)

    await _create_session_with_positions(
        storage,
        "Local Race Time",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
    )

    peer_session = {
        "session_id": 777,
        "name": "Far Time Peer",
        "start_utc": FAR_START.isoformat(),
        "end_utc": FAR_END.isoformat(),
        "centroid_lat": NEAR_LAT,
        "centroid_lon": NEAR_LON,
        "fingerprint": BOAT_B_FP,
        "status": "available",
    }

    candidates = await find_proximity_candidates(storage, CO_OP_ID, peer_sessions=[peer_session])
    assert len(candidates) == 0


@pytest.mark.asyncio
async def test_embargo_blocks_proximity_scan(storage: Storage) -> None:
    """Proximity scan skips sessions that are under embargo."""
    await _seed_co_op(storage)

    embargo_until = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    await _create_session_with_positions(
        storage,
        "Embargoed Race",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
        embargo_until=embargo_until,
    )

    peer_session = {
        "session_id": 666,
        "name": "Peer Race Embargo",
        "start_utc": PEER_START.isoformat(),
        "end_utc": PEER_END.isoformat(),
        "centroid_lat": NEAR_LAT,
        "centroid_lon": NEAR_LON,
        "fingerprint": BOAT_B_FP,
        "status": "available",
    }

    candidates = await find_proximity_candidates(storage, CO_OP_ID, peer_sessions=[peer_session])
    assert len(candidates) == 0


@pytest.mark.asyncio
async def test_minimum_track_points(storage: Storage) -> None:
    """Sessions with fewer than 10 track points are excluded."""
    await _seed_co_op(storage)

    await _create_session_with_positions(
        storage,
        "Short Race",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        num_points=5,  # below threshold
        share_with_co_op=CO_OP_ID,
    )

    peer_session = {
        "session_id": 555,
        "name": "Peer Race Short",
        "start_utc": PEER_START.isoformat(),
        "end_utc": PEER_END.isoformat(),
        "centroid_lat": NEAR_LAT,
        "centroid_lon": NEAR_LON,
        "fingerprint": BOAT_B_FP,
        "status": "available",
    }

    candidates = await find_proximity_candidates(storage, CO_OP_ID, peer_sessions=[peer_session])
    assert len(candidates) == 0


# ---------------------------------------------------------------------------
# Propose / confirm / reject tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_and_confirm_match(storage: Storage) -> None:
    """Proposing creates a candidate; two confirms promote to matched."""
    await _seed_co_op(storage)

    sid = await _create_session_with_positions(
        storage,
        "Match Race",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
    )

    match_id = await propose_match(
        storage,
        local_session_id=sid,
        peer_fingerprint=BOAT_B_FP,
        peer_session_id=999,
        centroid_lat=SF_LAT,
        centroid_lon=SF_LON,
        start_utc=SESSION_START.isoformat(),
        end_utc=SESSION_END.isoformat(),
    )
    assert match_id  # non-empty UUID string

    # First confirm (proposer)
    result = await confirm_match(storage, match_id, BOAT_A_FP)
    assert result is True

    # Second confirm (peer) -> quorum reached
    result = await confirm_match(storage, match_id, BOAT_B_FP)
    assert result is True

    # Verify the session now has the match_group_id and match_confirmed
    db = storage._conn()
    cur = await db.execute("SELECT match_group_id, match_confirmed FROM races WHERE id = ?", (sid,))
    row = await cur.fetchone()
    assert row is not None
    assert dict(row)["match_group_id"] == match_id
    assert dict(row)["match_confirmed"] == 1


@pytest.mark.asyncio
async def test_reject_match(storage: Storage) -> None:
    """Rejecting a match reverts the session to unmatched."""
    await _seed_co_op(storage)

    sid = await _create_session_with_positions(
        storage,
        "Reject Race",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
    )

    match_id = await propose_match(
        storage,
        local_session_id=sid,
        peer_fingerprint=BOAT_B_FP,
        peer_session_id=888,
        centroid_lat=SF_LAT,
        centroid_lon=SF_LON,
        start_utc=SESSION_START.isoformat(),
        end_utc=SESSION_END.isoformat(),
    )

    result = await reject_match(storage, match_id, BOAT_B_FP)
    assert result is True

    # Verify proposal status is 'rejected'
    db = storage._conn()
    cur = await db.execute(
        "SELECT status FROM session_match_proposals WHERE match_group_id = ?",
        (match_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert dict(row)["status"] == "rejected"


@pytest.mark.asyncio
async def test_single_confirm_not_enough(storage: Storage) -> None:
    """One confirm is not enough for quorum (needs 2)."""
    await _seed_co_op(storage)

    sid = await _create_session_with_positions(
        storage,
        "Single Confirm",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
    )

    match_id = await propose_match(
        storage,
        local_session_id=sid,
        peer_fingerprint=BOAT_B_FP,
        peer_session_id=777,
        centroid_lat=SF_LAT,
        centroid_lon=SF_LON,
        start_utc=SESSION_START.isoformat(),
        end_utc=SESSION_END.isoformat(),
    )

    await confirm_match(storage, match_id, BOAT_A_FP)

    # Session should NOT be confirmed yet
    db = storage._conn()
    cur = await db.execute("SELECT match_confirmed FROM races WHERE id = ?", (sid,))
    row = await cur.fetchone()
    assert row is not None
    assert dict(row)["match_confirmed"] == 0


# ---------------------------------------------------------------------------
# Name synthesis tests
# ---------------------------------------------------------------------------


def test_synthesize_name_zero_names() -> None:
    """Zero names returns empty string (waiting for names)."""
    assert synthesize_name([]) == ""


def test_synthesize_name_one_name() -> None:
    """Single name is used as-is."""
    assert synthesize_name(["Sunday Regatta Race 1"]) == "Sunday Regatta Race 1"


def test_synthesize_name_two_names_fallback() -> None:
    """Two names with no LLM: pick the longest."""
    result = synthesize_name(["Race 1", "Sunday Regatta - Race 1 (Start)"])
    assert result == "Sunday Regatta - Race 1 (Start)"


def test_synthesize_name_same_names() -> None:
    """If all names are the same, use that name."""
    result = synthesize_name(["Race 1", "Race 1"])
    assert result == "Race 1"


# ---------------------------------------------------------------------------
# Shared name storage — local name preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_name_does_not_overwrite_local(storage: Storage) -> None:
    """Setting shared_name does not change the local session name."""
    sid = await _create_session_with_positions(
        storage, "My Local Name", SESSION_START, SESSION_END, SF_LAT, SF_LON
    )

    db = storage._conn()
    await db.execute(
        "UPDATE races SET shared_name = ? WHERE id = ?",
        ("Co-op Race Name", sid),
    )
    await db.commit()

    cur = await db.execute("SELECT name, shared_name FROM races WHERE id = ?", (sid,))
    row = dict(await cur.fetchone())  # type: ignore[arg-type]
    assert row["name"] == "My Local Name"
    assert row["shared_name"] == "Co-op Race Name"


# ---------------------------------------------------------------------------
# Candidate expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_stale_candidates(storage: Storage) -> None:
    """Candidates past their expiry are marked expired."""
    await _seed_co_op(storage)

    sid = await _create_session_with_positions(
        storage,
        "Expiry Race",
        SESSION_START,
        SESSION_END,
        SF_LAT,
        SF_LON,
        share_with_co_op=CO_OP_ID,
    )

    match_id = await propose_match(
        storage,
        local_session_id=sid,
        peer_fingerprint=BOAT_B_FP,
        peer_session_id=444,
        centroid_lat=SF_LAT,
        centroid_lon=SF_LON,
        start_utc=SESSION_START.isoformat(),
        end_utc=SESSION_END.isoformat(),
    )

    # Manually set expires_at to the past
    db = storage._conn()
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await db.execute(
        "UPDATE session_match_proposals SET expires_at = ? WHERE match_group_id = ?",
        (past, match_id),
    )
    await db.commit()

    count = await expire_stale_candidates(storage)
    assert count >= 1

    cur = await db.execute(
        "SELECT status FROM session_match_proposals WHERE match_group_id = ?",
        (match_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert dict(row)["status"] == "expired"
