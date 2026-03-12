"""Tests for the peer API endpoints (remote boat → this boat)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.federation import (
    _pub_key_to_base64,
    fingerprint_from_pub_bytes,
    generate_keypair,
    init_identity,
)
from helmlog.peer_auth import sign_request
from helmlog.web import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage


def _client(storage: Storage) -> httpx.AsyncClient:
    app = create_app(storage)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


class TestPeerIdentity:
    @pytest.mark.asyncio
    async def test_identity_no_init(self, storage: Storage) -> None:
        with patch("helmlog.federation.load_identity", side_effect=FileNotFoundError):
            async with _client(storage) as c:
                resp = await c.get("/co-op/identity")
                assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_identity_returns_card(
        self,
        storage: Storage,
        tmp_path: Path,
    ) -> None:
        identity_dir = tmp_path / ".helmlog" / "identity"
        init_identity(
            identity_dir,
            sail_number="69",
            boat_name="Javelina",
            owner_email="test@example.com",
        )
        from helmlog.federation import load_identity as _real_load

        _, card = _real_load(identity_dir)
        with patch("helmlog.federation.load_identity", return_value=(None, card)):
            async with _client(storage) as c:
                resp = await c.get("/co-op/identity")
                assert resp.status_code == 200
                data = resp.json()
                assert data["name"] == "Javelina"
                assert "pub" in data


class TestPeerSessions:
    @pytest.fixture
    async def setup(self, storage: Storage) -> dict:
        """Set up admin identity, a co-op, a peer, and a shared session."""
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

        # Create a session and share it
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
                "2026-03-08T12:00:00Z",
                "2026-03-08T12:30:00Z",
                "race",
            ),
        )
        await db.commit()
        session_id = cur.lastrowid or 0

        await storage.share_session(session_id, "coop1")

        return {
            "peer_priv": peer_priv,
            "peer_fp": peer_fp,
            "session_id": session_id,
        }

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.get("/co-op/coop1/sessions")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_peer_rejected(self, storage: Storage) -> None:
        priv, _pub = generate_keypair()
        headers = sign_request(priv, "unknown_fp", "GET", "/co-op/coop1/sessions")
        async with _client(storage) as c:
            resp = await c.get("/co-op/coop1/sessions", headers=headers)
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_shared_sessions(
        self,
        storage: Storage,
        setup: dict,
    ) -> None:
        path = "/co-op/coop1/sessions"
        headers = sign_request(
            setup["peer_priv"],
            setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["sessions"]) == 1
            assert data["sessions"][0]["name"] == "Test Race"
            assert data["sessions"][0]["status"] == "available"

    @pytest.mark.asyncio
    async def test_session_not_shared_returns_404(
        self,
        storage: Storage,
        setup: dict,
    ) -> None:
        path = "/co-op/coop1/sessions/99999/track"
        headers = sign_request(
            setup["peer_priv"],
            setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_non_member_coop_rejected(
        self,
        storage: Storage,
        setup: dict,
    ) -> None:
        path = "/co-op/other-coop/sessions"
        headers = sign_request(
            setup["peer_priv"],
            setup["peer_fp"],
            "GET",
            path,
        )
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 403


class TestPeerWindField:
    """Tests for the wind-field peer endpoint (#246)."""

    @pytest.fixture
    async def setup(self, storage: Storage) -> dict:
        """Set up a co-op with a synthesized session that has wind field params."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        admin_priv, admin_pub = generate_keypair()
        admin_pub_b64 = _pub_key_to_base64(admin_pub)
        peer_priv, peer_pub = generate_keypair()
        peer_pub_b64 = _pub_key_to_base64(peer_pub)
        peer_fp = fingerprint_from_pub_bytes(
            peer_pub.public_bytes(Encoding.Raw, PublicFormat.Raw),
        )

        await storage.save_co_op_membership(
            co_op_id="coop1",
            co_op_name="Test Fleet",
            co_op_pub=admin_pub_b64,
            membership_json="{}",
            role="admin",
        )

        await storage.save_co_op_peer(
            co_op_id="coop1",
            boat_pub=peer_pub_b64,
            fingerprint=peer_fp,
            membership_json="{}",
            sail_number="42",
            boat_name="Peer Boat",
        )

        # Create a synthesized session
        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO races (name, event, race_num, date, start_utc,"
            " end_utc, session_type)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "Synth Race",
                "CYC Wed",
                1,
                "2026-03-08",
                "2026-03-08T12:00:00Z",
                "2026-03-08T12:30:00Z",
                "synthesized",
            ),
        )
        await db.commit()
        session_id = cur.lastrowid or 0

        await storage.share_session(session_id, "coop1")

        # Save wind field params
        await storage.save_synth_wind_params(
            session_id,
            {
                "seed": 42,
                "base_twd": 180.0,
                "tws_low": 8.0,
                "tws_high": 14.0,
                "shift_interval_lo": 600.0,
                "shift_interval_hi": 1200.0,
                "shift_magnitude_lo": 5.0,
                "shift_magnitude_hi": 14.0,
                "ref_lat": 47.63,
                "ref_lon": -122.40,
                "duration_s": 1800.0,
                "course_type": "windward_leeward",
                "leg_distance_nm": 1.0,
                "laps": 2,
                "mark_sequence": None,
            },
        )
        await storage.save_synth_course_marks(
            session_id,
            [
                {"mark_key": "W", "mark_name": "Windward", "lat": 47.64, "lon": -122.40},
                {"mark_key": "L", "mark_name": "Leeward", "lat": 47.62, "lon": -122.40},
            ],
        )

        return {
            "peer_priv": peer_priv,
            "peer_fp": peer_fp,
            "session_id": session_id,
        }

    @pytest.mark.asyncio
    async def test_wind_field_returns_start_utc(
        self,
        storage: Storage,
        setup: dict,
    ) -> None:
        """Wind-field response must include start_utc for co-op synthesis."""
        path = f"/co-op/coop1/sessions/{setup['session_id']}/wind-field"
        headers = sign_request(setup["peer_priv"], setup["peer_fp"], "GET", path)
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "start_utc" in data
            assert data["start_utc"] == "2026-03-08T12:00:00Z"

    @pytest.mark.asyncio
    async def test_wind_field_returns_params_and_marks(
        self,
        storage: Storage,
        setup: dict,
    ) -> None:
        """Wind-field response must include wind_params and marks."""
        path = f"/co-op/coop1/sessions/{setup['session_id']}/wind-field"
        headers = sign_request(setup["peer_priv"], setup["peer_fp"], "GET", path)
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["wind_params"]["seed"] == 42
            assert data["wind_params"]["base_twd"] == 180.0
            assert len(data["marks"]) == 2

    @pytest.mark.asyncio
    async def test_wind_field_not_shared_returns_404(
        self,
        storage: Storage,
        setup: dict,
    ) -> None:
        path = "/co-op/coop1/sessions/99999/wind-field"
        headers = sign_request(setup["peer_priv"], setup["peer_fp"], "GET", path)
        async with _client(storage) as c:
            resp = await c.get(path, headers=headers)
            assert resp.status_code == 404
