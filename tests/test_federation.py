"""Tests for helmlog.federation — identity, signing, co-op management."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from helmlog.federation import (
    BoatCard,
    Charter,
    MembershipRecord,
    RevocationRecord,
    create_co_op,
    fingerprint_from_pub_bytes,
    generate_keypair,
    identity_exists,
    init_identity,
    list_co_op_members,
    list_co_ops,
    load_charter,
    load_identity,
    sign_json,
    sign_membership,
    sign_message,
    sign_revocation,
    verify_json_sig,
    verify_membership,
    verify_revocation,
    verify_signature,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    return generate_keypair()


@pytest.fixture
def identity_dir(tmp_path: Path) -> Path:
    return tmp_path / ".helmlog" / "identity"


@pytest.fixture
def boat_card(identity_dir: Path) -> BoatCard:
    return init_identity(
        identity_dir,
        sail_number="69",
        boat_name="Javelina",
        owner_email="skipper@example.com",
    )


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


class TestKeyManagement:
    def test_generate_keypair(self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]) -> None:
        priv, pub = keypair
        assert priv is not None
        assert pub is not None

    def test_fingerprint_deterministic(
        self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
    ) -> None:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        _, pub = keypair
        raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        fp1 = fingerprint_from_pub_bytes(raw)
        fp2 = fingerprint_from_pub_bytes(raw)
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_fingerprint_unique(self) -> None:
        _, pub1 = generate_keypair()
        _, pub2 = generate_keypair()
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        raw1 = pub1.public_bytes(Encoding.Raw, PublicFormat.Raw)
        raw2 = pub2.public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert fingerprint_from_pub_bytes(raw1) != fingerprint_from_pub_bytes(raw2)


# ---------------------------------------------------------------------------
# Signing and verification
# ---------------------------------------------------------------------------


class TestSigning:
    def test_sign_and_verify(self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]) -> None:
        priv, pub = keypair
        message = b"hello world"
        sig = sign_message(priv, message)
        assert verify_signature(pub, message, sig)

    def test_verify_wrong_message(
        self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey],
    ) -> None:
        priv, pub = keypair
        sig = sign_message(priv, b"hello")
        assert not verify_signature(pub, b"world", sig)

    def test_verify_wrong_key(self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]) -> None:
        priv, _ = keypair
        _, other_pub = generate_keypair()
        sig = sign_message(priv, b"test")
        assert not verify_signature(other_pub, b"test", sig)

    def test_sign_json_roundtrip(self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]) -> None:
        priv, pub = keypair
        data = {"foo": "bar", "num": 42}
        sig = sign_json(priv, data)
        assert verify_json_sig(pub, data, sig)

    def test_sign_json_key_order_independent(
        self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]
    ) -> None:
        priv, pub = keypair
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}
        sig = sign_json(priv, data1)
        # Should verify with different key order (sorted keys used internally)
        assert verify_json_sig(pub, data2, sig)

    def test_sign_json_tampered(self, keypair: tuple[Ed25519PrivateKey, Ed25519PublicKey]) -> None:
        priv, pub = keypair
        data = {"value": 100}
        sig = sign_json(priv, data)
        tampered = {"value": 999}
        assert not verify_json_sig(pub, tampered, sig)


# ---------------------------------------------------------------------------
# Identity management
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_init_identity(self, identity_dir: Path) -> None:
        card = init_identity(
            identity_dir,
            sail_number="69",
            boat_name="Javelina",
            owner_email="skipper@example.com",
        )
        assert card.sail_number == "69"
        assert card.boat_name == "Javelina"
        assert card.owner_email == "skipper@example.com"
        assert len(card.fingerprint) == 16
        assert len(card.pub_key) > 0

    def test_init_creates_files(self, identity_dir: Path) -> None:
        init_identity(identity_dir, sail_number="69", boat_name="Test")
        assert (identity_dir / "boat.key").exists()
        assert (identity_dir / "boat.pub").exists()
        assert (identity_dir / "boat.json").exists()

    def test_key_file_permissions(self, identity_dir: Path) -> None:
        init_identity(identity_dir, sail_number="69", boat_name="Test")
        key_stat = (identity_dir / "boat.key").stat()
        assert key_stat.st_mode & 0o777 == 0o600

    def test_init_refuses_overwrite(self, identity_dir: Path) -> None:
        init_identity(identity_dir, sail_number="69", boat_name="Test")
        with pytest.raises(FileExistsError):
            init_identity(identity_dir, sail_number="69", boat_name="Test")

    def test_init_force_overwrite(self, identity_dir: Path) -> None:
        card1 = init_identity(identity_dir, sail_number="69", boat_name="Test")
        card2 = init_identity(identity_dir, sail_number="42", boat_name="Other", force=True)
        assert card1.fingerprint != card2.fingerprint
        assert card2.sail_number == "42"

    def test_load_identity(self, identity_dir: Path) -> None:
        original = init_identity(
            identity_dir, sail_number="69", boat_name="Javelina",
            owner_email="test@example.com",
        )
        priv, loaded = load_identity(identity_dir)
        assert loaded.pub_key == original.pub_key
        assert loaded.fingerprint == original.fingerprint
        assert loaded.sail_number == "69"
        assert loaded.boat_name == "Javelina"
        assert loaded.owner_email == "test@example.com"
        # Verify the private key works
        sig = sign_message(priv, b"test")
        assert len(sig) == 64

    def test_load_identity_not_found(self, identity_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_identity(identity_dir)

    def test_identity_exists(self, identity_dir: Path) -> None:
        assert not identity_exists(identity_dir)
        init_identity(identity_dir, sail_number="69", boat_name="Test")
        assert identity_exists(identity_dir)

    def test_boat_card_json(self, boat_card: BoatCard) -> None:
        data = json.loads(boat_card.to_json())
        assert data["pub"] == boat_card.pub_key
        assert data["fingerprint"] == boat_card.fingerprint
        assert data["sail_number"] == "69"
        assert data["name"] == "Javelina"
        assert data["owner_email"] == "skipper@example.com"

    def test_boat_card_no_email(self, identity_dir: Path) -> None:
        card = init_identity(identity_dir, sail_number="1", boat_name="Anon")
        data = json.loads(card.to_json())
        assert "owner_email" not in data


# ---------------------------------------------------------------------------
# Co-op management
# ---------------------------------------------------------------------------


class TestCoOp:
    def test_create_co_op(self, identity_dir: Path, boat_card: BoatCard) -> None:
        priv, _ = load_identity(identity_dir)
        charter = create_co_op(
            priv, boat_card, name="Puget Sound J/105",
            areas=["Elliott Bay"], identity_dir=identity_dir,
        )
        assert charter.name == "Puget Sound J/105"
        assert len(charter.co_op_id) == 16  # URL-safe base64 hash prefix
        assert charter.co_op_id != boat_card.fingerprint
        assert charter.areas == ["Elliott Bay"]
        assert charter.admin_sig != ""

    def test_create_co_op_writes_files(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        charter = create_co_op(
            priv, boat_card, name="Test Fleet", identity_dir=identity_dir,
        )
        co_op_dir = identity_dir.parent / "co-ops" / charter.co_op_id
        assert (co_op_dir / "charter.json").exists()
        assert (co_op_dir / "members" / f"{boat_card.fingerprint}.json").exists()

    def test_create_two_coops_different_ids(
        self, identity_dir: Path, boat_card: BoatCard,
    ) -> None:
        """Same boat creating two co-ops should produce distinct IDs."""
        priv, _ = load_identity(identity_dir)
        c1 = create_co_op(
            priv, boat_card, name="Fleet A", identity_dir=identity_dir,
        )
        c2 = create_co_op(
            priv, boat_card, name="Fleet B", identity_dir=identity_dir,
        )
        assert c1.co_op_id != c2.co_op_id

    def test_create_co_op_self_membership(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        charter = create_co_op(
            priv, boat_card, name="Test", identity_dir=identity_dir,
        )
        members = list_co_op_members(charter.co_op_id, identity_dir)
        assert len(members) == 1
        assert members[0].boat_pub == boat_card.pub_key
        assert members[0].role == "admin"

    def test_charter_signature_valid(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        charter = create_co_op(
            priv, boat_card, name="Test", identity_dir=identity_dir,
        )
        # Verify charter signature
        charter_dict = charter.to_dict()
        sig = charter_dict.pop("admin_sig")
        from helmlog.federation import _pub_key_from_base64

        pub = _pub_key_from_base64(boat_card.pub_key)
        assert verify_json_sig(pub, charter_dict, sig)

    def test_load_charter(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        original = create_co_op(
            priv, boat_card, name="Loaded Fleet", identity_dir=identity_dir,
        )
        loaded = load_charter(original.co_op_id, identity_dir)
        assert loaded.name == "Loaded Fleet"
        assert loaded.co_op_id == original.co_op_id
        assert loaded.admin_sig == original.admin_sig

    def test_list_co_ops(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        create_co_op(priv, boat_card, name="Fleet A", identity_dir=identity_dir)
        # For a second co-op, we need a different co_op_id (different admin key)
        # Since co_op_id = admin fingerprint, one boat = one co-op in single-moderator mode
        co_ops = list_co_ops(identity_dir)
        assert len(co_ops) == 1
        assert co_ops[0].name == "Fleet A"


# ---------------------------------------------------------------------------
# Membership signing and verification
# ---------------------------------------------------------------------------


class TestMembership:
    def test_sign_membership(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        record = sign_membership(
            priv, co_op_id="test-co-op", boat_card=boat_card, role="member",
        )
        assert record.co_op_id == "test-co-op"
        assert record.boat_pub == boat_card.pub_key
        assert record.role == "member"
        assert record.admin_sig != ""

    def test_verify_membership_valid(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        record = sign_membership(
            priv, co_op_id="test", boat_card=boat_card,
        )
        assert verify_membership(boat_card.pub_key, record)

    def test_verify_membership_wrong_key(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        record = sign_membership(priv, co_op_id="test", boat_card=boat_card)

        # Verify with a different key should fail
        from helmlog.federation import _pub_key_to_base64

        _, other_pub = generate_keypair()
        other_b64 = _pub_key_to_base64(other_pub)
        assert not verify_membership(other_b64, record)

    def test_membership_json_roundtrip(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        record = sign_membership(priv, co_op_id="test", boat_card=boat_card)
        data = json.loads(record.to_json())
        assert data["type"] == "membership"
        assert data["co_op_id"] == "test"
        assert data["boat_pub"] == boat_card.pub_key


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    def test_sign_revocation(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        record = sign_revocation(
            priv,
            co_op_id="test",
            boat_pub=boat_card.pub_key,
            reason="voluntary_departure",
            grace_days=30,
        )
        assert record.co_op_id == "test"
        assert record.reason == "voluntary_departure"
        assert record.admin_sig != ""

    def test_verify_revocation(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        record = sign_revocation(
            priv, co_op_id="test", boat_pub=boat_card.pub_key,
        )
        assert verify_revocation(boat_card.pub_key, record)

    def test_revocation_json(
        self, identity_dir: Path, boat_card: BoatCard
    ) -> None:
        priv, _ = load_identity(identity_dir)
        record = sign_revocation(
            priv, co_op_id="test", boat_pub=boat_card.pub_key,
        )
        data = json.loads(record.to_json())
        assert data["type"] == "revocation"
        assert data["reason"] == "voluntary_departure"
        assert data["grace_until"] > data["effective_at"]


# ---------------------------------------------------------------------------
# Dataclass serialization
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_charter_to_dict(self) -> None:
        charter = Charter(
            co_op_id="abc123",
            name="Test Co-op",
            areas=["Bay"],
            admin_boat_pub="pubkey",
            admin_boat_fingerprint="fp",
            created_at="2026-01-01T00:00:00Z",
        )
        d = charter.to_dict()
        assert d["type"] == "charter"
        assert d["co_op_id"] == "abc123"
        assert d["admin_boat"]["pub"] == "pubkey"

    def test_membership_record_to_dict(self) -> None:
        record = MembershipRecord(
            co_op_id="abc",
            boat_pub="pub",
            sail_number="42",
            boat_name="Test",
            role="member",
            joined_at="2026-01-01T00:00:00Z",
        )
        d = record.to_dict()
        assert d["type"] == "membership"
        assert "owner_email" not in d  # None is excluded

    def test_membership_record_with_email(self) -> None:
        record = MembershipRecord(
            co_op_id="abc",
            boat_pub="pub",
            sail_number="42",
            boat_name="Test",
            role="member",
            joined_at="2026-01-01T00:00:00Z",
            owner_email="test@example.com",
        )
        d = record.to_dict()
        assert d["owner_email"] == "test@example.com"

    def test_revocation_record_to_dict(self) -> None:
        record = RevocationRecord(
            co_op_id="abc",
            boat_pub="pub",
            reason="expulsion",
            effective_at="2026-01-01",
            grace_until="2026-02-01",
        )
        d = record.to_dict()
        assert d["type"] == "revocation"
        assert d["reason"] == "expulsion"
