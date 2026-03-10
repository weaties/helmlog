"""Tests for peer-to-peer request authentication."""

from __future__ import annotations

import base64

import pytest

from helmlog.federation import generate_keypair
from helmlog.peer_auth import (
    HDR_BOAT,
    HDR_NONCE,
    HDR_SIG,
    HDR_TIMESTAMP,
    sign_request,
    verify_peer_request,
)


@pytest.fixture
def keypair() -> tuple:
    priv, pub = generate_keypair()
    return priv, pub


class TestSignRequest:
    def test_returns_all_headers(self, keypair: tuple) -> None:
        priv, _pub = keypair
        headers = sign_request(priv, "fp123", "GET", "/co-op/abc/sessions")
        assert HDR_BOAT in headers
        assert HDR_TIMESTAMP in headers
        assert HDR_NONCE in headers
        assert HDR_SIG in headers
        assert headers[HDR_BOAT] == "fp123"

    def test_custom_timestamp_and_nonce(self, keypair: tuple) -> None:
        priv, _pub = keypair
        headers = sign_request(
            priv,
            "fp123",
            "GET",
            "/path",
            timestamp="2026-03-08T00:00:00Z",
            nonce="abc123",
        )
        assert headers[HDR_TIMESTAMP] == "2026-03-08T00:00:00Z"
        assert headers[HDR_NONCE] == "abc123"


class TestVerifyPeerRequest:
    def test_valid_signature(self, keypair: tuple) -> None:
        priv, pub = keypair
        headers = sign_request(priv, "fp123", "GET", "/co-op/abc/sessions")
        assert verify_peer_request("GET", "/co-op/abc/sessions", headers, pub)

    def test_wrong_path_fails(self, keypair: tuple) -> None:
        priv, pub = keypair
        headers = sign_request(priv, "fp123", "GET", "/co-op/abc/sessions")
        assert not verify_peer_request("GET", "/co-op/WRONG/sessions", headers, pub)

    def test_wrong_method_fails(self, keypair: tuple) -> None:
        priv, pub = keypair
        headers = sign_request(priv, "fp123", "GET", "/path")
        assert not verify_peer_request("POST", "/path", headers, pub)

    def test_tampered_signature_fails(self, keypair: tuple) -> None:
        priv, pub = keypair
        headers = sign_request(priv, "fp123", "GET", "/path")
        headers[HDR_SIG] = base64.b64encode(b"x" * 64).decode()
        assert not verify_peer_request("GET", "/path", headers, pub)

    def test_missing_headers_fails(self, keypair: tuple) -> None:
        _priv, pub = keypair
        assert not verify_peer_request("GET", "/path", {}, pub)

    def test_nonce_replay_rejected(self, keypair: tuple) -> None:
        priv, pub = keypair
        headers = sign_request(
            priv,
            "fp123",
            "GET",
            "/path",
            nonce="unique-nonce-replay-test",
        )
        # First request succeeds
        assert verify_peer_request("GET", "/path", headers, pub)
        # Replay with same nonce fails
        assert not verify_peer_request("GET", "/path", headers, pub)

    def test_different_key_fails(self, keypair: tuple) -> None:
        priv, _pub = keypair
        _, other_pub = generate_keypair()
        headers = sign_request(priv, "fp123", "GET", "/path")
        assert not verify_peer_request("GET", "/path", headers, other_pub)
