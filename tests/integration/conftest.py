"""Integration test fixtures — two boats with real keypairs and federation.

Sets up two in-memory Storage instances (boat_a = admin, boat_b = member),
creates a co-op, seeds sessions (shared, embargoed, private), and provides
httpx async clients wired to each boat's FastAPI app.

All crypto is real Ed25519 — no mocking. The only thing simulated is the
network (httpx ASGITransport instead of TCP).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest_asyncio

from helmlog.federation import _priv_key_to_pem
from helmlog.peer_auth import sign_request
from helmlog.storage import Storage, StorageConfig
from helmlog.web import create_app

from .seed import (
    make_boat_identity,
    make_charter,
    make_membership,
    seed_storage,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_identity_to_disk(identity: dict[str, Any], identity_dir: Path) -> None:
    """Write a boat's keypair and card to a temp directory.

    This lets the /co-op/identity endpoint (which calls load_identity())
    find the identity on disk.
    """
    identity_dir.mkdir(parents=True, exist_ok=True)

    # Write private key
    key_path = identity_dir / "boat.key"
    key_path.write_bytes(_priv_key_to_pem(identity["private_key"]))
    os.chmod(key_path, 0o600)

    # Write public key
    (identity_dir / "boat.pub").write_text(identity["pub_b64"] + "\n")

    # Write boat card
    card_data = identity["card"].to_dict()
    (identity_dir / "boat.json").write_text(json.dumps(card_data, indent=2) + "\n")


@dataclass
class Boat:
    """A test boat with identity, storage, app, and HTTP client."""

    identity: dict[str, Any]
    storage: Storage
    app: Any
    client: httpx.AsyncClient
    resources: dict[str, Any]  # session IDs, co-op ID, etc.
    identity_dir: Path | None = None

    @property
    def fingerprint(self) -> str:
        return self.identity["fingerprint"]

    @property
    def co_op_id(self) -> str:
        return self.resources["co_op_id"]

    def sign(self, method: str, path: str) -> dict[str, str]:
        """Sign a request as this boat."""
        return sign_request(
            self.identity["private_key"],
            self.identity["fingerprint"],
            method,
            path,
        )


@dataclass
class Fleet:
    """Two boats in a co-op, ready for integration testing."""

    boat_a: Boat  # admin
    boat_b: Boat  # member


@pytest_asyncio.fixture
async def fleet(tmp_path: Path) -> Fleet:  # type: ignore[misc]
    """Two-boat fleet with a co-op, shared sessions, and signed memberships."""

    # Generate identities
    id_a = make_boat_identity("42", "Javelina")
    id_b = make_boat_identity("69", "Corvo")

    # Write identities to temp dirs so load_identity() works
    dir_a = tmp_path / "identity-a"
    dir_b = tmp_path / "identity-b"
    _write_identity_to_disk(id_a, dir_a)
    _write_identity_to_disk(id_b, dir_b)

    # Create co-op (A is admin)
    charter = make_charter(id_a, "SF Bay Racing Co-op")
    mem_a = make_membership(charter, id_a, id_a, role="admin")
    mem_b = make_membership(charter, id_b, id_a, role="member")

    # Set up storage for each boat
    storage_a = Storage(StorageConfig(db_path=":memory:"))
    await storage_a.connect()

    storage_b = Storage(StorageConfig(db_path=":memory:"))
    await storage_b.connect()

    # Seed boat A's storage (A is admin, B is its peer)
    resources_a = await seed_storage(
        storage_a,
        id_a,
        id_b,
        charter,
        mem_a,
        mem_b,
    )

    # Seed boat B's storage (B is member, A is its peer)
    resources_b = await seed_storage(
        storage_b,
        id_b,
        id_a,
        charter,
        mem_b,
        mem_a,
    )

    # Create FastAPI apps with middleware that sets the identity dir
    # per-request so load_identity() finds the right keypair.
    import helmlog.federation as fed

    app_a = create_app(storage_a)
    app_b = create_app(storage_b)

    @app_a.middleware("http")
    async def _set_identity_a(request: Any, call_next: Any) -> Any:  # noqa: ANN401
        fed._DEFAULT_IDENTITY_DIR = dir_a
        return await call_next(request)

    @app_b.middleware("http")
    async def _set_identity_b(request: Any, call_next: Any) -> Any:  # noqa: ANN401
        fed._DEFAULT_IDENTITY_DIR = dir_b
        return await call_next(request)

    # Create HTTP clients
    client_a = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_a),
        base_url="http://boat-a",
    )
    client_b = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b),
        base_url="http://boat-b",
    )

    fleet = Fleet(
        boat_a=Boat(
            identity=id_a,
            storage=storage_a,
            app=app_a,
            client=client_a,
            resources=resources_a,
            identity_dir=dir_a,
        ),
        boat_b=Boat(
            identity=id_b,
            storage=storage_b,
            app=app_b,
            client=client_b,
            resources=resources_b,
            identity_dir=dir_b,
        ),
    )

    yield fleet  # type: ignore[misc]

    await client_a.aclose()
    await client_b.aclose()
    await storage_a.close()
    await storage_b.close()
