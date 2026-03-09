"""Federation — boat identity, co-op membership, and cryptographic signing.

Provides Ed25519 keypair management, boat card creation, co-op charter and
membership record signing/verification, and session sharing helpers.

Key material lives on the filesystem (~/.helmlog/identity/); only public
key references are stored in SQLite.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_IDENTITY_DIR = Path.home() / ".helmlog" / "identity"
_FINGERPRINT_LENGTH = 16  # chars of base64url — collision-safe for <1000 boats


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoatCard:
    """Public identity of a boat — freely shareable."""

    pub_key: str  # base64-encoded Ed25519 public key
    fingerprint: str  # SHA-256 truncated base64url
    sail_number: str
    boat_name: str
    owner_email: str | None = None  # required for co-op, optional standalone

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "pub": self.pub_key,
            "fingerprint": self.fingerprint,
            "sail_number": self.sail_number,
            "name": self.boat_name,
        }
        if self.owner_email:
            d["owner_email"] = self.owner_email
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass(frozen=True)
class Charter:
    """Signed co-op charter record."""

    co_op_id: str  # fingerprint of admin's public key (single-moderator mode)
    name: str
    areas: list[str]
    admin_boat_pub: str
    admin_boat_fingerprint: str
    created_at: str
    heartbeat_inactive_days: int = 60
    sharing_delay: str = "immediate"
    session_visibility: str = "event_scoped"
    benchmark_min_boats: int = 4
    benchmark_cache_ttl: int = 86400
    admin_sig: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "charter",
            "co_op_id": self.co_op_id,
            "name": self.name,
            "areas": self.areas,
            "admin_boat": {
                "pub": self.admin_boat_pub,
                "fingerprint": self.admin_boat_fingerprint,
            },
            "created_at": self.created_at,
            "heartbeat_inactive_days": self.heartbeat_inactive_days,
            "sharing_delay": self.sharing_delay,
            "session_visibility": self.session_visibility,
            "benchmark_min_boats": self.benchmark_min_boats,
            "benchmark_cache_ttl": self.benchmark_cache_ttl,
            "admin_sig": self.admin_sig,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass(frozen=True)
class MembershipRecord:
    """Signed membership record proving a boat belongs to a co-op."""

    co_op_id: str
    boat_pub: str
    sail_number: str
    boat_name: str
    role: str  # "member" | "admin"
    joined_at: str
    owner_email: str | None = None
    admin_sig: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "membership",
            "co_op_id": self.co_op_id,
            "boat_pub": self.boat_pub,
            "sail_number": self.sail_number,
            "boat_name": self.boat_name,
            "role": self.role,
            "joined_at": self.joined_at,
            "admin_sig": self.admin_sig,
        }
        if self.owner_email:
            d["owner_email"] = self.owner_email
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass(frozen=True)
class RevocationRecord:
    """Signed revocation record for revoking a boat's co-op membership."""

    co_op_id: str
    boat_pub: str
    reason: str  # "voluntary_departure" | "expulsion"
    effective_at: str
    grace_until: str
    admin_sig: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "revocation",
            "co_op_id": self.co_op_id,
            "boat_pub": self.boat_pub,
            "reason": self.reason,
            "effective_at": self.effective_at,
            "grace_until": self.grace_until,
            "admin_sig": self.admin_sig,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def fingerprint_from_pub_bytes(pub_bytes: bytes) -> str:
    """Derive a truncated base64url fingerprint from raw public key bytes."""
    digest = hashlib.sha256(pub_bytes).digest()
    return base64.urlsafe_b64encode(digest).decode()[:_FINGERPRINT_LENGTH]


def _pub_key_to_base64(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _pub_key_from_base64(b64: str) -> Ed25519PublicKey:
    raw = base64.b64decode(b64)
    return Ed25519PublicKey.from_public_bytes(raw)


def _priv_key_to_pem(priv: Ed25519PrivateKey) -> bytes:
    return priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _priv_key_from_pem(pem: bytes) -> Ed25519PrivateKey:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        msg = "Expected Ed25519 private key"
        raise TypeError(msg)
    return key


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a new Ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


# ---------------------------------------------------------------------------
# Signing and verification
# ---------------------------------------------------------------------------


def sign_message(private_key: Ed25519PrivateKey, message: bytes) -> bytes:
    """Sign a message with an Ed25519 private key. Returns 64-byte signature."""
    return private_key.sign(message)


def verify_signature(public_key: Ed25519PublicKey, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature. Returns True if valid, False otherwise."""
    try:
        public_key.verify(signature, message)
    except Exception:
        return False
    return True


def sign_json(private_key: Ed25519PrivateKey, data: dict[str, Any]) -> str:
    """Sign a JSON-serializable dict. Returns base64-encoded signature.

    The dict is serialized with sorted keys and no whitespace for canonical form.
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    sig = sign_message(private_key, canonical)
    return base64.b64encode(sig).decode()


def verify_json_sig(public_key: Ed25519PublicKey, data: dict[str, Any], sig_b64: str) -> bool:
    """Verify a signature over a JSON dict."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    sig = base64.b64decode(sig_b64)
    return verify_signature(public_key, canonical, sig)


# ---------------------------------------------------------------------------
# Boat identity management
# ---------------------------------------------------------------------------


def init_identity(
    identity_dir: Path | None = None,
    *,
    sail_number: str,
    boat_name: str,
    owner_email: str | None = None,
    force: bool = False,
) -> BoatCard:
    """Generate a keypair and write identity files to disk.

    Creates:
      identity_dir/boat.key   (PEM, mode 0600)
      identity_dir/boat.pub   (base64 raw public key)
      identity_dir/boat.json  (boat card)

    Raises FileExistsError if identity already exists and force=False.
    """
    identity_dir = identity_dir or _DEFAULT_IDENTITY_DIR
    identity_dir.mkdir(parents=True, exist_ok=True)

    key_path = identity_dir / "boat.key"
    if key_path.exists() and not force:
        msg = f"Identity already exists at {key_path}. Use force=True to overwrite."
        raise FileExistsError(msg)

    private_key, public_key = generate_keypair()

    # Write private key (mode 0600)
    key_path.write_bytes(_priv_key_to_pem(private_key))
    os.chmod(key_path, 0o600)

    # Write public key
    pub_b64 = _pub_key_to_base64(public_key)
    (identity_dir / "boat.pub").write_text(pub_b64 + "\n")

    # Compute fingerprint
    pub_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    fp = fingerprint_from_pub_bytes(pub_bytes)

    # Write boat card
    card = BoatCard(
        pub_key=pub_b64,
        fingerprint=fp,
        sail_number=sail_number,
        boat_name=boat_name,
        owner_email=owner_email,
    )
    (identity_dir / "boat.json").write_text(card.to_json() + "\n")

    logger.info("Identity created: {} ({})", boat_name, fp)
    return card


def load_identity(identity_dir: Path | None = None) -> tuple[Ed25519PrivateKey, BoatCard]:
    """Load an existing identity from disk.

    Returns (private_key, boat_card).
    Raises FileNotFoundError if the identity doesn't exist.
    """
    identity_dir = identity_dir or _DEFAULT_IDENTITY_DIR

    key_path = identity_dir / "boat.key"
    card_path = identity_dir / "boat.json"

    if not key_path.exists():
        msg = f"No identity found at {key_path}. Run 'helmlog identity init' first."
        raise FileNotFoundError(msg)

    private_key = _priv_key_from_pem(key_path.read_bytes())

    card_data = json.loads(card_path.read_text())
    card = BoatCard(
        pub_key=card_data["pub"],
        fingerprint=card_data["fingerprint"],
        sail_number=card_data["sail_number"],
        boat_name=card_data["name"],
        owner_email=card_data.get("owner_email"),
    )

    return private_key, card


def identity_exists(identity_dir: Path | None = None) -> bool:
    """Check whether an identity has been initialized."""
    identity_dir = identity_dir or _DEFAULT_IDENTITY_DIR
    return (identity_dir / "boat.key").exists()


# ---------------------------------------------------------------------------
# Co-op management
# ---------------------------------------------------------------------------


def create_co_op(
    private_key: Ed25519PrivateKey,
    boat_card: BoatCard,
    *,
    name: str,
    areas: list[str] | None = None,
    identity_dir: Path | None = None,
) -> Charter:
    """Create a new co-op with this boat as the single moderator.

    Writes charter and self-membership to ~/.helmlog/co-ops/<co_op_id>/.
    """
    identity_dir = identity_dir or _DEFAULT_IDENTITY_DIR

    now = datetime.now(UTC).isoformat()

    # Co-op ID: unique per co-op (hash of admin fingerprint + name + timestamp)
    id_input = f"{boat_card.fingerprint}:{name}:{now}"
    co_op_id = (
        base64.urlsafe_b64encode(hashlib.sha256(id_input.encode()).digest())[:16]
        .decode()
    )

    # Build charter (without sig first, then sign)
    charter = Charter(
        co_op_id=co_op_id,
        name=name,
        areas=areas or [],
        admin_boat_pub=boat_card.pub_key,
        admin_boat_fingerprint=boat_card.fingerprint,
        created_at=now,
    )

    # Sign the charter
    charter_dict = charter.to_dict()
    del charter_dict["admin_sig"]  # sign everything except the sig field
    sig = sign_json(private_key, charter_dict)
    charter = Charter(**{**vars(charter), "admin_sig": sig})

    # Write to filesystem
    co_op_dir = identity_dir.parent / "co-ops" / co_op_id
    co_op_dir.mkdir(parents=True, exist_ok=True)
    (co_op_dir / "charter.json").write_text(charter.to_json() + "\n")

    # Create self-membership (admin role)
    membership = sign_membership(
        private_key,
        co_op_id=co_op_id,
        boat_card=boat_card,
        role="admin",
    )

    members_dir = co_op_dir / "members"
    members_dir.mkdir(exist_ok=True)
    (members_dir / f"{boat_card.fingerprint}.json").write_text(membership.to_json() + "\n")

    logger.info("Co-op '{}' created (id: {})", name, co_op_id)
    return charter


def sign_membership(
    admin_key: Ed25519PrivateKey,
    *,
    co_op_id: str,
    boat_card: BoatCard,
    role: str = "member",
) -> MembershipRecord:
    """Create and sign a membership record for a boat."""
    now = datetime.now(UTC).isoformat()

    record = MembershipRecord(
        co_op_id=co_op_id,
        boat_pub=boat_card.pub_key,
        sail_number=boat_card.sail_number,
        boat_name=boat_card.boat_name,
        role=role,
        joined_at=now,
        owner_email=boat_card.owner_email,
    )

    # Sign everything except admin_sig
    rec_dict = record.to_dict()
    del rec_dict["admin_sig"]
    sig = sign_json(admin_key, rec_dict)

    return MembershipRecord(**{**vars(record), "admin_sig": sig})


def verify_membership(admin_pub_b64: str, record: MembershipRecord) -> bool:
    """Verify a membership record's admin signature."""
    pub = _pub_key_from_base64(admin_pub_b64)
    rec_dict = record.to_dict()
    sig = rec_dict.pop("admin_sig")
    return verify_json_sig(pub, rec_dict, sig)


def sign_revocation(
    admin_key: Ed25519PrivateKey,
    *,
    co_op_id: str,
    boat_pub: str,
    reason: str = "voluntary_departure",
    grace_days: int = 30,
) -> RevocationRecord:
    """Create and sign a revocation record."""
    now = datetime.now(UTC)
    from datetime import timedelta

    effective = now.isoformat()
    grace = (now + timedelta(days=grace_days)).isoformat()

    record = RevocationRecord(
        co_op_id=co_op_id,
        boat_pub=boat_pub,
        reason=reason,
        effective_at=effective,
        grace_until=grace,
    )

    rec_dict = record.to_dict()
    del rec_dict["admin_sig"]
    sig = sign_json(admin_key, rec_dict)

    return RevocationRecord(**{**vars(record), "admin_sig": sig})


def verify_revocation(admin_pub_b64: str, record: RevocationRecord) -> bool:
    """Verify a revocation record's admin signature."""
    pub = _pub_key_from_base64(admin_pub_b64)
    rec_dict = record.to_dict()
    sig = rec_dict.pop("admin_sig")
    return verify_json_sig(pub, rec_dict, sig)


# ---------------------------------------------------------------------------
# Co-op filesystem helpers
# ---------------------------------------------------------------------------


def load_charter(co_op_id: str, identity_dir: Path | None = None) -> Charter:
    """Load a co-op charter from the filesystem."""
    identity_dir = identity_dir or _DEFAULT_IDENTITY_DIR
    co_op_dir = identity_dir.parent / "co-ops" / co_op_id
    charter_path = co_op_dir / "charter.json"

    if not charter_path.exists():
        msg = f"No charter found at {charter_path}"
        raise FileNotFoundError(msg)

    data = json.loads(charter_path.read_text())
    admin = data.get("admin_boat", {})
    return Charter(
        co_op_id=data["co_op_id"],
        name=data["name"],
        areas=data.get("areas", []),
        admin_boat_pub=admin.get("pub", ""),
        admin_boat_fingerprint=admin.get("fingerprint", ""),
        created_at=data["created_at"],
        heartbeat_inactive_days=data.get("heartbeat_inactive_days", 60),
        sharing_delay=data.get("sharing_delay", "immediate"),
        session_visibility=data.get("session_visibility", "event_scoped"),
        benchmark_min_boats=data.get("benchmark_min_boats", 4),
        benchmark_cache_ttl=data.get("benchmark_cache_ttl", 86400),
        admin_sig=data.get("admin_sig", ""),
    )


def list_co_op_members(
    co_op_id: str, identity_dir: Path | None = None
) -> list[MembershipRecord]:
    """Load all membership records for a co-op from the filesystem."""
    identity_dir = identity_dir or _DEFAULT_IDENTITY_DIR
    members_dir = identity_dir.parent / "co-ops" / co_op_id / "members"

    if not members_dir.exists():
        return []

    records: list[MembershipRecord] = []
    for path in sorted(members_dir.glob("*.json")):
        data = json.loads(path.read_text())
        records.append(
            MembershipRecord(
                co_op_id=data["co_op_id"],
                boat_pub=data["boat_pub"],
                sail_number=data["sail_number"],
                boat_name=data["boat_name"],
                role=data["role"],
                joined_at=data["joined_at"],
                owner_email=data.get("owner_email"),
                admin_sig=data.get("admin_sig", ""),
            )
        )
    return records


def list_co_ops(identity_dir: Path | None = None) -> list[Charter]:
    """List all co-ops this boat belongs to (from filesystem)."""
    identity_dir = identity_dir or _DEFAULT_IDENTITY_DIR
    co_ops_dir = identity_dir.parent / "co-ops"

    if not co_ops_dir.exists():
        return []

    charters: list[Charter] = []
    for d in sorted(co_ops_dir.iterdir()):
        charter_path = d / "charter.json"
        if charter_path.exists():
            charters.append(load_charter(d.name, identity_dir))
    return charters
