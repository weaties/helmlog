"""Seed two in-memory Storage instances with federation test data.

Creates two "boats" (A and B), a co-op where A is admin and B is a member,
sessions with various sharing/embargo states, and instrument data for track
fetch testing.

Used by both the in-process pytest fixtures and the Docker entry point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from helmlog.federation import (
    BoatCard,
    Charter,
    MembershipRecord,
    _pub_key_to_base64,
    fingerprint_from_pub_bytes,
    generate_keypair,
    sign_json,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Boat identity helpers
# ---------------------------------------------------------------------------


def make_boat_identity(
    sail_number: str,
    boat_name: str,
) -> dict[str, Any]:
    """Generate a keypair and boat card for a test boat."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    priv, pub = generate_keypair()
    pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    fp = fingerprint_from_pub_bytes(pub_bytes)
    pub_b64 = _pub_key_to_base64(pub)

    card = BoatCard(
        pub_key=pub_b64,
        fingerprint=fp,
        sail_number=sail_number,
        boat_name=boat_name,
    )
    return {
        "private_key": priv,
        "public_key": pub,
        "pub_b64": pub_b64,
        "fingerprint": fp,
        "card": card,
        "sail_number": sail_number,
        "boat_name": boat_name,
    }


def make_charter(admin: dict[str, Any], co_op_name: str) -> Charter:
    """Create a signed charter with the admin boat."""
    now = datetime.now(UTC).isoformat()
    charter = Charter(
        co_op_id=f"test-coop-{admin['fingerprint'][:8]}",
        name=co_op_name,
        areas=["SF Bay"],
        admin_boat_pub=admin["pub_b64"],
        admin_boat_fingerprint=admin["fingerprint"],
        created_at=now,
    )
    # Sign the charter
    sig = sign_json(admin["private_key"], charter.to_dict())
    return Charter(**{**charter.__dict__, "admin_sig": sig})


def make_membership(
    charter: Charter,
    boat: dict[str, Any],
    admin: dict[str, Any],
    role: str = "member",
) -> MembershipRecord:
    """Create a signed membership record."""
    now = datetime.now(UTC).isoformat()
    record = MembershipRecord(
        co_op_id=charter.co_op_id,
        boat_pub=boat["pub_b64"],
        sail_number=boat["sail_number"],
        boat_name=boat["boat_name"],
        role=role,
        joined_at=now,
    )
    sig = sign_json(admin["private_key"], record.to_dict())
    return MembershipRecord(**{**record.__dict__, "admin_sig": sig})


# ---------------------------------------------------------------------------
# Session / instrument data seeding
# ---------------------------------------------------------------------------

# Session times — 10 minutes of data
SESSION_START = datetime(2026, 3, 1, 14, 0, 0, tzinfo=UTC)
SESSION_END = datetime(2026, 3, 1, 14, 10, 0, tzinfo=UTC)

# Embargoed session — embargo lifts in the future
EMBARGO_SESSION_START = datetime(2026, 3, 2, 14, 0, 0, tzinfo=UTC)
EMBARGO_SESSION_END = datetime(2026, 3, 2, 14, 10, 0, tzinfo=UTC)
EMBARGO_UNTIL = (datetime.now(UTC) + timedelta(days=7)).isoformat()


async def seed_storage(
    storage: Storage,
    boat: dict[str, Any],
    peer: dict[str, Any],
    charter: Charter,
    boat_membership: MembershipRecord,
    peer_membership: MembershipRecord,
) -> dict[str, Any]:
    """Seed a storage instance with federation data and sessions.

    Returns a dict of created resource IDs for use in tests.
    """
    # Save this boat's identity
    await storage.save_boat_identity(
        pub_key=boat["pub_b64"],
        fingerprint=boat["fingerprint"],
        sail_number=boat["sail_number"],
        boat_name=boat["boat_name"],
    )

    # Save co-op membership
    await storage.save_co_op_membership(
        co_op_id=charter.co_op_id,
        co_op_name=charter.name,
        co_op_pub=charter.admin_boat_pub,
        membership_json=boat_membership.to_json(),
        role=boat_membership.role,
    )

    # Save peer (the other boat) as a known co-op peer
    await storage.save_co_op_peer(
        co_op_id=charter.co_op_id,
        boat_pub=peer["pub_b64"],
        fingerprint=peer["fingerprint"],
        membership_json=peer_membership.to_json(),
        sail_number=peer["sail_number"],
        boat_name=peer["boat_name"],
        tailscale_ip="127.0.0.1",
    )

    db = storage._conn()

    # Create a normal session (shared, no embargo)
    race = await storage.start_race(
        event="Integration Test Regatta",
        start_utc=SESSION_START,
        date_str="2026-03-01",
        race_num=1,
        name="Test Race 1",
        session_type="race",
    )
    await storage.end_race(race.id, SESSION_END)

    # Share this session with the co-op
    await storage.share_session(race.id, charter.co_op_id)

    # Seed some instrument data for the track endpoint
    for i in range(10):
        ts = (SESSION_START + timedelta(seconds=i)).isoformat()
        lat = 37.8044 + i * 0.0001
        lon = -122.2712 + i * 0.0001
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 5, lat, lon),
        )
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg, deviation_deg, variation_deg)"
            " VALUES (?, ?, ?, NULL, NULL)",
            (ts, 5, 180.0 + i),
        )
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (ts, 5, 5.0 + i * 0.1),
        )
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (ts, 5, 45.0 + i, 6.0 + i * 0.1),
        )
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 5, 15.0, 30.0, 0),  # true wind
        )
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 5, 18.0, 25.0, 2),  # apparent wind
        )
    await db.commit()

    # Create an embargoed session
    embargo_race = await storage.start_race(
        event="Integration Test Regatta",
        start_utc=EMBARGO_SESSION_START,
        date_str="2026-03-02",
        race_num=1,
        name="Embargoed Race",
        session_type="race",
    )
    await storage.end_race(embargo_race.id, EMBARGO_SESSION_END)
    await storage.share_session(
        embargo_race.id,
        charter.co_op_id,
        embargo_until=EMBARGO_UNTIL,
    )

    # Create an unshared session (should NOT be visible to peers)
    private_race = await storage.start_race(
        event="Private Practice",
        start_utc=datetime(2026, 3, 3, 14, 0, 0, tzinfo=UTC),
        date_str="2026-03-03",
        race_num=1,
        name="Private Session",
        session_type="practice",
    )
    await storage.end_race(private_race.id, datetime(2026, 3, 3, 14, 10, 0, tzinfo=UTC))

    return {
        "shared_session_id": race.id,
        "embargo_session_id": embargo_race.id,
        "private_session_id": private_race.id,
        "co_op_id": charter.co_op_id,
    }
