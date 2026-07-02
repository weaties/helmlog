"""Tests for sk_reader: Signal K delta parsing and SKReader reconnect behaviour."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from helmlog.nmea2000 import (
    AttitudeRecord,
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PositionRecord,
    RudderRecord,
    SpeedRecord,
    WindRecord,
)
from helmlog.sk_reader import SK_SOURCE_ADDR, SKReader, SKReaderConfig, process_delta

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2025-08-10T14:05:30.000Z"
_TS_DT = datetime(2025, 8, 10, 14, 5, 30, tzinfo=UTC)
_FAKE_REQUEST = httpx.Request("GET", "http://test")


def _delta(path: str, value: object, ts: str = _TS) -> str:
    """Minimal Signal K delta JSON for a single path/value pair."""
    return json.dumps(
        {
            "context": "vessels.self",
            "updates": [{"timestamp": ts, "values": [{"path": path, "value": value}]}],
        }
    )


# ---------------------------------------------------------------------------
# TestPathConversions — pure unit tests of process_delta conversions
# ---------------------------------------------------------------------------


class TestPathConversions:
    """Each SK path → correct record type with correct unit conversion."""

    def test_heading_rad_to_deg(self) -> None:
        buf: dict[str, float] = {}
        records = process_delta(_delta("navigation.headingTrue", math.pi), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, HeadingRecord)
        assert abs(rec.heading_deg - 180.0) < 1e-6
        assert rec.deviation_deg is None
        assert rec.variation_deg is None
        assert rec.source_addr == SK_SOURCE_ADDR
        assert rec.timestamp == _TS_DT

    def test_speed_mps_to_kts(self) -> None:
        buf: dict[str, float] = {}
        records = process_delta(_delta("navigation.speedThroughWater", 1.0), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, SpeedRecord)
        assert abs(rec.speed_kts - 1.94384449) < 1e-4

    def test_depth_passthrough(self) -> None:
        buf: dict[str, float] = {}
        records = process_delta(_delta("environment.depth.belowKeel", 12.5), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, DepthRecord)
        assert rec.depth_m == pytest.approx(12.5)
        assert rec.offset_m is None

    def test_position_dict_passthrough(self) -> None:
        buf: dict[str, float] = {}
        pos = {"latitude": 41.79, "longitude": -71.87}
        records = process_delta(_delta("navigation.position", pos), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, PositionRecord)
        assert rec.latitude_deg == pytest.approx(41.79)
        assert rec.longitude_deg == pytest.approx(-71.87)
        # No $source on the delta — position falls back to the SK constant
        # so existing rows in the DB stay comparable.
        assert rec.source_addr == SK_SOURCE_ADDR

    def test_position_distinct_source_addr_per_signalk_source(self) -> None:
        """Two physical GPS antennas alternating on the bus must end up in
        rows with different source_addr values, so downstream consumers can
        deduplicate or filter by source. Regression for the stair-step
        track artifact found on corvopi-tst1.
        """
        buf: dict[str, float] = {}
        pos = {"latitude": 41.79, "longitude": -71.87}

        def delta_with_source(src: str) -> str:
            return json.dumps(
                {
                    "context": "vessels.self",
                    "updates": [
                        {
                            "$source": src,
                            "timestamp": _TS,
                            "values": [{"path": "navigation.position", "value": pos}],
                        }
                    ],
                }
            )

        rec_a = process_delta(delta_with_source("n2k.0.130577.0"), buf)[0]
        rec_b = process_delta(delta_with_source("n2k.1.43.0"), buf)[0]
        assert isinstance(rec_a, PositionRecord)
        assert isinstance(rec_b, PositionRecord)
        assert rec_a.source_addr != rec_b.source_addr
        assert rec_a.source_addr != SK_SOURCE_ADDR
        assert rec_b.source_addr != SK_SOURCE_ADDR

    def test_temperature_kelvin_to_celsius(self) -> None:
        buf: dict[str, float] = {}
        records = process_delta(_delta("environment.water.temperature", 293.15), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, EnvironmentalRecord)
        assert abs(rec.water_temp_c - 20.0) < 1e-6

    def test_rudder_angle_rad_to_deg(self) -> None:
        buf: dict[str, float] = {}
        # 0.1745 rad ≈ 10° starboard
        records = process_delta(_delta("steering.rudderAngle", 0.1745), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, RudderRecord)
        assert abs(rec.rudder_angle_deg - 10.0) < 0.1
        assert rec.source_addr == SK_SOURCE_ADDR
        assert rec.timestamp == _TS_DT

    def test_attitude_rad_to_deg(self) -> None:
        buf: dict[str, float] = {}
        # heel = 15°, trim = -2° (bow down)
        value = {"roll": math.radians(15.0), "pitch": math.radians(-2.0), "yaw": 0.0}
        records = process_delta(_delta("navigation.attitude", value), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, AttitudeRecord)
        assert abs(rec.heel_deg - 15.0) < 1e-6
        assert abs(rec.trim_deg - -2.0) < 1e-6
        assert rec.source_addr == SK_SOURCE_ADDR
        assert rec.timestamp == _TS_DT

    def test_attitude_missing_yaw_ok(self) -> None:
        buf: dict[str, float] = {}
        # Some SK sources omit yaw — should still emit a record
        records = process_delta(_delta("navigation.attitude", {"roll": 0.1, "pitch": 0.05}), buf)
        assert len(records) == 1
        assert isinstance(records[0], AttitudeRecord)

    def test_attitude_partial_dropped(self) -> None:
        buf: dict[str, float] = {}
        # Missing either roll or pitch → no record (avoid partial writes)
        records = process_delta(_delta("navigation.attitude", {"roll": 0.1}), buf)
        assert records == []
        records = process_delta(_delta("navigation.attitude", {"pitch": 0.1}), buf)
        assert records == []

    def test_true_wind_ref_zero(self) -> None:
        buf: dict[str, float] = {}
        # Speed arrives first — no record yet
        r1 = process_delta(_delta("environment.wind.speedTrue", 5.144), buf)
        assert r1 == []
        # Angle arrives — combined record emitted with ref=0
        r2 = process_delta(_delta("environment.wind.angleTrue", math.pi / 4), buf)
        assert len(r2) == 1
        rec = r2[0]
        assert isinstance(rec, WindRecord)
        assert rec.reference == 0
        assert abs(rec.wind_speed_kts - 5.144 * 1.94384449) < 1e-4
        assert abs(rec.wind_angle_deg - 45.0) < 1e-4

    def test_apparent_wind_ref_two(self) -> None:
        buf: dict[str, float] = {}
        process_delta(_delta("environment.wind.speedApparent", 3.0), buf)
        r = process_delta(_delta("environment.wind.angleApparent", math.pi / 2), buf)
        assert len(r) == 1
        rec = r[0]
        assert isinstance(rec, WindRecord)
        assert rec.reference == 2
        assert abs(rec.wind_angle_deg - 90.0) < 1e-4

    def test_cog_rad_to_deg(self) -> None:
        buf: dict[str, float] = {}
        process_delta(_delta("navigation.courseOverGroundTrue", math.pi), buf)
        r = process_delta(_delta("navigation.speedOverGround", 2.0), buf)
        assert len(r) == 1
        rec = r[0]
        assert isinstance(rec, COGSOGRecord)
        assert abs(rec.cog_deg - 180.0) < 1e-6
        assert abs(rec.sog_kts - 2.0 * 1.94384449) < 1e-4


# ---------------------------------------------------------------------------
# TestSKReaderDelta — delta parsing behaviour
# ---------------------------------------------------------------------------


class TestSKReaderDelta:
    """Test process_delta message handling rules."""

    def test_single_path_emits_immediately(self) -> None:
        buf: dict[str, float] = {}
        records = process_delta(_delta("navigation.headingTrue", 0.5), buf)
        assert len(records) == 1
        assert isinstance(records[0], HeadingRecord)

    def test_cogsog_buffers_until_both_seen(self) -> None:
        buf: dict[str, float] = {}
        # First field — no record
        r1 = process_delta(_delta("navigation.courseOverGroundTrue", 1.0), buf)
        assert r1 == []
        # Second field — record emitted
        r2 = process_delta(_delta("navigation.speedOverGround", 1.0), buf)
        assert len(r2) == 1
        assert isinstance(r2[0], COGSOGRecord)

    def test_wind_buffers_until_both_seen(self) -> None:
        buf: dict[str, float] = {}
        r1 = process_delta(_delta("environment.wind.speedTrue", 5.0), buf)
        assert r1 == []
        r2 = process_delta(_delta("environment.wind.angleTrue", 1.0), buf)
        assert len(r2) == 1
        assert isinstance(r2[0], WindRecord)

    def test_unknown_path_ignored_no_error(self) -> None:
        buf: dict[str, float] = {}
        records = process_delta(_delta("navigation.someFutureField", 42.0), buf)
        assert records == []

    def test_malformed_numeric_value_warns_no_record(self) -> None:
        buf: dict[str, float] = {}
        with patch("helmlog.sk_reader.logger") as mock_log:
            records = process_delta(_delta("navigation.headingTrue", "not-a-number"), buf)
        assert records == []
        mock_log.warning.assert_called_once()
        assert "non-numeric" in str(mock_log.warning.call_args)

    def test_malformed_json_warns_no_record(self) -> None:
        buf: dict[str, float] = {}
        with patch("helmlog.sk_reader.logger") as mock_log:
            records = process_delta("{bad json}", buf)
        assert records == []
        mock_log.warning.assert_called_once()
        assert "malformed JSON" in str(mock_log.warning.call_args)

    def test_bad_position_value_warns(self) -> None:
        buf: dict[str, float] = {}
        with patch("helmlog.sk_reader.logger") as mock_log:
            records = process_delta(_delta("navigation.position", "not-a-dict"), buf)
        assert records == []
        mock_log.warning.assert_called_once()
        assert "bad position" in str(mock_log.warning.call_args)

    def test_multiple_updates_in_one_delta(self) -> None:
        buf: dict[str, float] = {}
        msg = json.dumps(
            {
                "context": "vessels.self",
                "updates": [
                    {
                        "timestamp": _TS,
                        "values": [
                            {"path": "navigation.headingTrue", "value": 1.0},
                            {"path": "navigation.speedThroughWater", "value": 2.0},
                        ],
                    }
                ],
            }
        )
        records = process_delta(msg, buf)
        assert len(records) == 2
        assert isinstance(records[0], HeadingRecord)
        assert isinstance(records[1], SpeedRecord)

    def test_true_apparent_wind_buffered_independently(self) -> None:
        """True and apparent wind use separate buffer slots."""
        buf: dict[str, float] = {}
        process_delta(_delta("environment.wind.speedTrue", 5.0), buf)
        # Apparent speed should not contaminate true wind
        r = process_delta(_delta("environment.wind.speedApparent", 4.0), buf)
        assert r == []  # angle not yet buffered for either type

    def test_timestamp_fallback_to_now_on_bad_ts(self) -> None:
        buf: dict[str, float] = {}
        before = datetime.now(UTC)
        records = process_delta(_delta("navigation.headingTrue", 1.0, ts="invalid-ts"), buf)
        after = datetime.now(UTC)
        assert len(records) == 1
        assert before <= records[0].timestamp <= after


# ---------------------------------------------------------------------------
# TestSelfVesselFilter — context matching for self-vessel deltas (#208)
# ---------------------------------------------------------------------------


class TestSelfVesselFilter:
    """Self-vessel context filter accepts UUID-style and literal 'vessels.self'."""

    def test_literal_vessels_self_accepted(self) -> None:
        """The literal context 'vessels.self' is always accepted."""
        buf: dict[str, float] = {}
        records = process_delta(_delta("navigation.headingTrue", 1.0), buf)
        assert len(records) == 1

    def test_uuid_context_accepted_when_matching_self(self) -> None:
        """A UUID context matching the resolved self identity is accepted."""
        buf: dict[str, float] = {}
        uuid_ctx = "vessels.urn:mrn:signalk:uuid:26bf9a06-2956-41c4-976c-db16b80c9334"
        msg = json.dumps(
            {
                "context": uuid_ctx,
                "updates": [
                    {
                        "timestamp": _TS,
                        "values": [
                            {"path": "navigation.headingTrue", "value": 1.0},
                        ],
                    }
                ],
            }
        )
        records = process_delta(msg, buf, self_context=uuid_ctx)
        assert len(records) == 1
        assert isinstance(records[0], HeadingRecord)

    def test_uuid_context_rejected_when_not_matching_self(self) -> None:
        """A UUID context that doesn't match the resolved self is rejected."""
        buf: dict[str, float] = {}
        self_ctx = "vessels.urn:mrn:signalk:uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        other_ctx = "vessels.urn:mrn:signalk:uuid:11111111-2222-3333-4444-555555555555"
        msg = json.dumps(
            {
                "context": other_ctx,
                "updates": [
                    {
                        "timestamp": _TS,
                        "values": [
                            {"path": "navigation.headingTrue", "value": 1.0},
                        ],
                    }
                ],
            }
        )
        records = process_delta(msg, buf, self_context=self_ctx)
        assert records == []

    def test_uuid_context_rejected_when_no_self_context_provided(self) -> None:
        """Without self_context, UUID contexts are still rejected (backward compat)."""
        buf: dict[str, float] = {}
        msg = json.dumps(
            {
                "context": "vessels.urn:mrn:signalk:uuid:26bf9a06-2956-41c4-976c-db16b80c9334",
                "updates": [
                    {
                        "timestamp": _TS,
                        "values": [
                            {"path": "navigation.headingTrue", "value": 1.0},
                        ],
                    }
                ],
            }
        )
        records = process_delta(msg, buf)
        assert records == []

    def test_no_context_field_defaults_to_self(self) -> None:
        """Deltas without a context field are treated as self-vessel."""
        buf: dict[str, float] = {}
        msg = json.dumps(
            {
                "updates": [
                    {
                        "timestamp": _TS,
                        "values": [
                            {"path": "navigation.headingTrue", "value": 1.0},
                        ],
                    }
                ],
            }
        )
        records = process_delta(msg, buf)
        assert len(records) == 1


# ---------------------------------------------------------------------------
# TestSKReaderReconnect — reconnect and cancellation behaviour
# ---------------------------------------------------------------------------


class TestSKReaderReconnect:
    """Test SKReader reconnect and CancelledError propagation."""

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError from the outer task propagates through the reader."""
        entered = asyncio.Event()

        class FakeWS:
            """WebSocket that blocks forever until cancelled."""

            async def __aenter__(self) -> FakeWS:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            def __aiter__(self) -> AsyncGenerator[str, None]:
                return self._gen()

            async def _gen(self) -> AsyncGenerator[str, None]:
                entered.set()
                await asyncio.sleep(3600)  # block until cancelled
                yield ""  # never reached

        async def _noop(self: object) -> None:
            return None

        with (
            patch("helmlog.sk_reader._ws_connect", lambda _uri: FakeWS()),
            patch.object(SKReader, "_resolve_self_context", _noop),
        ):
            reader = SKReader(SKReaderConfig())
            task = asyncio.create_task(self._collect_one(reader))
            await entered.wait()  # deterministic: task is inside the WS generator
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def _collect_one(self, reader: SKReader) -> None:
        async for _ in reader:
            break

    async def test_reconnects_after_normal_disconnect(self) -> None:
        """Reader reconnects immediately after a graceful WebSocket close."""
        connect_calls: list[int] = [0]
        reconnected = asyncio.Event()

        class FakeWS:
            def __init__(self) -> None:
                self._call = connect_calls[0]
                connect_calls[0] += 1
                if self._call >= 1:
                    reconnected.set()

            async def __aenter__(self) -> FakeWS:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            def __aiter__(self) -> AsyncGenerator[str, None]:
                return self._gen()

            async def _gen(self) -> AsyncGenerator[str, None]:
                if self._call == 0:
                    # First connection: yield one record then close gracefully
                    yield _delta("navigation.headingTrue", math.pi / 2)
                else:
                    # Second connection: block until test task is cancelled
                    await asyncio.sleep(3600)
                    yield ""  # never reached

        async def _noop(self: object) -> None:
            return None

        with (
            patch("helmlog.sk_reader._ws_connect", lambda _uri: FakeWS()),
            patch.object(SKReader, "_resolve_self_context", _noop),
        ):
            reader = SKReader(SKReaderConfig())

            async def collect_until_reconnect() -> None:
                async for _ in reader:
                    pass  # keep iterating through disconnect/reconnect

            task = asyncio.create_task(collect_until_reconnect())
            # Wait for the second connection attempt — deterministic, no sleep
            await asyncio.wait_for(reconnected.wait(), timeout=5.0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert connect_calls[0] >= 2, "Expected at least 2 connection attempts"

    async def test_reconnects_with_backoff_on_exception(self) -> None:
        """Reader applies backoff delay when connection raises an exception."""
        connect_calls: list[int] = [0]
        reached_target = asyncio.Event()

        class FailWS:
            async def __aenter__(self) -> FailWS:
                connect_calls[0] += 1
                if connect_calls[0] >= 2:
                    reached_target.set()
                raise ConnectionRefusedError("no server")

            async def __aexit__(self, *_: object) -> None:
                pass

            def __aiter__(self) -> AsyncGenerator[str, None]:
                return self._gen()

            async def _gen(self) -> AsyncGenerator[str, None]:
                yield ""  # never reached

        async def _noop(self: object) -> None:
            return None

        with (
            patch("helmlog.sk_reader._ws_connect", lambda _uri: FailWS()),
            patch.object(SKReader, "_resolve_self_context", _noop),
        ):
            reader = SKReader(SKReaderConfig(reconnect_delay_s=0.01))
            task = asyncio.create_task(self._collect_one(reader))
            # Wait for at least 2 retry attempts — deterministic, no sleep
            await asyncio.wait_for(reached_target.wait(), timeout=5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert connect_calls[0] >= 2


# ---------------------------------------------------------------------------
# TestSKReaderConfigAuth — auth fields on SKReaderConfig
# ---------------------------------------------------------------------------


class TestSKReaderConfigAuth:
    """Auth-related fields on SKReaderConfig."""

    def test_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK_TOKEN", "abc123")
        cfg = SKReaderConfig()
        assert cfg.token == "abc123"

    def test_username_password_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK_USERNAME", "admin")
        monkeypatch.setenv("SK_PASSWORD", "secret")
        cfg = SKReaderConfig()
        assert cfg.username == "admin"
        assert cfg.password == "secret"

    def test_defaults_none_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SK_TOKEN", raising=False)
        monkeypatch.delenv("SK_USERNAME", raising=False)
        monkeypatch.delenv("SK_PASSWORD", raising=False)
        cfg = SKReaderConfig()
        assert cfg.token is None
        assert cfg.username is None
        assert cfg.password is None


# ---------------------------------------------------------------------------
# TestTokenResolution — _resolve_token waterfall
# ---------------------------------------------------------------------------


class TestTokenResolution:
    """Token resolution waterfall: explicit token → login → password file → None."""

    async def test_uses_explicit_token(self) -> None:
        """SK_TOKEN takes priority — no HTTP call made."""
        reader = SKReader(SKReaderConfig(token="pre-existing"))
        token = await reader._resolve_token()
        assert token == "pre-existing"

    async def test_login_with_username_password(self) -> None:
        """Auto-login via SK REST API when credentials provided."""
        cfg = SKReaderConfig(username="admin", password="secret")
        reader = SKReader(cfg)

        async def mock_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"token": "jwt-from-login"}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", mock_post):
            token = await reader._resolve_token()
        assert token == "jwt-from-login"

    async def test_login_fallback_to_password_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reads ~/.signalk-admin-pass.txt when no env vars set."""
        pass_file = tmp_path / ".signalk-admin-pass.txt"
        pass_file.write_text("file-password\n")
        monkeypatch.setattr("helmlog.sk_reader.Path.home", staticmethod(lambda: tmp_path))

        cfg = SKReaderConfig()
        reader = SKReader(cfg)

        async def mock_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"token": "jwt-from-file"}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", mock_post):
            token = await reader._resolve_token()
        assert token == "jwt-from-file"

    async def test_no_credentials_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token, no creds, no file — returns None."""
        monkeypatch.setattr("helmlog.sk_reader.Path.home", staticmethod(lambda: tmp_path))
        cfg = SKReaderConfig()
        reader = SKReader(cfg)
        token = await reader._resolve_token()
        assert token is None

    async def test_login_failure_returns_none(self) -> None:
        """Bad credentials log a warning and return None."""
        cfg = SKReaderConfig(username="admin", password="wrong")
        reader = SKReader(cfg)

        async def mock_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized", request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", mock_post):
            token = await reader._resolve_token()
        assert token is None

    async def test_password_file_missing_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing password file is not an error — returns None."""
        monkeypatch.setattr("helmlog.sk_reader.Path.home", staticmethod(lambda: tmp_path))
        cfg = SKReaderConfig()
        reader = SKReader(cfg)
        token = await reader._resolve_token()
        assert token is None

    async def test_password_file_env_overrides_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SK_PASSWORD_FILE env var overrides Path.home() lookup."""
        # Put password file in a non-home location
        pass_file = tmp_path / "custom" / ".signalk-admin-pass.txt"
        pass_file.parent.mkdir()
        pass_file.write_text("custom-password\n")
        monkeypatch.setenv("SK_PASSWORD_FILE", str(pass_file))

        # Point Path.home() somewhere without the file — should not matter
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        monkeypatch.setattr("helmlog.sk_reader.Path.home", staticmethod(lambda: empty_home))

        cfg = SKReaderConfig()
        assert cfg.password_file == str(pass_file)
        reader = SKReader(cfg)

        async def mock_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            body = kwargs.get("json", {})
            assert body.get("password") == "custom-password"  # type: ignore[union-attr]
            return httpx.Response(200, json={"token": "jwt-custom"}, request=_FAKE_REQUEST)

        with patch.object(httpx.AsyncClient, "post", mock_post):
            token = await reader._resolve_token()
        assert token == "jwt-custom"

    async def test_password_file_default_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without SK_PASSWORD_FILE env var, password_file defaults to None."""
        monkeypatch.delenv("SK_PASSWORD_FILE", raising=False)
        cfg = SKReaderConfig()
        assert cfg.password_file is None


# ---------------------------------------------------------------------------
# TestAuthPlumbing — token passed to HTTP and WebSocket connections
# ---------------------------------------------------------------------------


class TestAuthPlumbing:
    """Token is plumbed to HTTP headers and WebSocket URI."""

    async def test_resolve_self_context_sends_bearer_header(self) -> None:
        """HTTP GET /api/self includes Authorization header when token is set."""
        reader = SKReader(SKReaderConfig(token="my-jwt"))
        reader._token = "my-jwt"
        captured_headers: dict[str, str] = {}

        async def mock_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            headers = kwargs.get("headers", {})
            assert isinstance(headers, dict)
            captured_headers.update(headers)
            return httpx.Response(200, json="vessels.self")

        with patch.object(httpx.AsyncClient, "get", mock_get):
            await reader._resolve_self_context()

        assert captured_headers.get("Authorization") == "Bearer my-jwt"

    async def test_resolve_self_context_no_header_when_no_token(self) -> None:
        """No auth header sent when token is None."""
        reader = SKReader(SKReaderConfig())
        reader._token = None
        captured_headers: dict[str, str] = {}

        async def mock_get(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            headers = kwargs.get("headers", {})
            assert isinstance(headers, dict)
            captured_headers.update(headers)
            return httpx.Response(200, json="vessels.self")

        with patch.object(httpx.AsyncClient, "get", mock_get):
            await reader._resolve_self_context()

        assert "Authorization" not in captured_headers

    async def test_websocket_uri_includes_token_param(self) -> None:
        """WS URI has &token=<jwt> when authenticated."""
        reader = SKReader(SKReaderConfig())
        reader._token = "ws-jwt"

        captured_uris: list[str] = []

        class FakeWS:
            def __init__(self, uri: str) -> None:
                captured_uris.append(uri)

            async def __aenter__(self) -> FakeWS:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            def __aiter__(self) -> AsyncGenerator[str, None]:
                return self._gen()

            async def _gen(self) -> AsyncGenerator[str, None]:
                yield _delta("navigation.headingTrue", 1.0)

        with (
            patch("helmlog.sk_reader._ws_connect", lambda uri, **kw: FakeWS(uri)),
            patch.object(SKReader, "_resolve_self_context", AsyncMock(return_value=None)),
            patch.object(SKReader, "_resolve_token", AsyncMock(return_value="ws-jwt")),
        ):
            async for _ in reader:
                break

        assert len(captured_uris) >= 1
        assert "&token=ws-jwt" in captured_uris[0]

    async def test_websocket_uri_no_token_when_none(self) -> None:
        """WS URI is unchanged when no token."""
        reader = SKReader(SKReaderConfig())
        reader._token = None

        captured_uris: list[str] = []

        class FakeWS:
            def __init__(self, uri: str) -> None:
                captured_uris.append(uri)

            async def __aenter__(self) -> FakeWS:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            def __aiter__(self) -> AsyncGenerator[str, None]:
                return self._gen()

            async def _gen(self) -> AsyncGenerator[str, None]:
                yield _delta("navigation.headingTrue", 1.0)

        with (
            patch("helmlog.sk_reader._ws_connect", lambda uri, **kw: FakeWS(uri)),
            patch.object(SKReader, "_resolve_self_context", AsyncMock(return_value=None)),
            patch.object(SKReader, "_resolve_token", AsyncMock(return_value=None)),
        ):
            async for _ in reader:
                break

        assert len(captured_uris) >= 1
        assert "token=" not in captured_uris[0]
        assert captured_uris[0].endswith("subscribe=all")


# ---------------------------------------------------------------------------
# TestClockDiscipline — GPS-disciplined record timestamps (#794)
# ---------------------------------------------------------------------------


class TestClockDiscipline:
    """process_delta routes record timestamps through a ClockDiscipliner."""

    @staticmethod
    def _gps_pos_delta(host_ts: str, gps_dt: str, pos: dict[str, float]) -> str:
        # One delta carrying GPS time and a position fix at the same host time.
        return json.dumps(
            {
                "context": "vessels.self",
                "updates": [
                    {
                        "timestamp": host_ts,
                        "values": [
                            {"path": "navigation.datetime", "value": gps_dt},
                            {"path": "navigation.position", "value": pos},
                        ],
                    }
                ],
            }
        )

    def test_no_clock_uses_host_timestamp(self) -> None:
        # Without a discipliner, behaviour is unchanged: host timestamp wins.
        recs = process_delta(_delta("navigation.headingTrue", math.pi), {})
        assert recs[0].timestamp == _TS_DT

    def test_host_clock_slow_records_disciplined_to_gps(self) -> None:
        # Host clock 1h slow vs GPS (the race-201 condition).
        from helmlog.clock_sync import ClockFlag
        from helmlog.sk_reader import ClockDiscipliner

        clock = ClockDiscipliner()
        pos = {"latitude": 47.68, "longitude": -122.41}
        rec = None
        for i in range(6):
            host = datetime(2026, 6, 16, 0, 27, i, tzinfo=UTC)
            gps = host + timedelta(seconds=3600)
            out = process_delta(
                self._gps_pos_delta(host.isoformat(), gps.isoformat(), pos),
                {},
                clock=clock,
            )
            rec = next(r for r in out if isinstance(r, PositionRecord))
        assert clock.flag is ClockFlag.CORRECTED
        # Position is stamped at true (GPS) time, ~1h ahead of the host clock.
        assert rec is not None
        assert rec.timestamp.hour == 1  # 00:27 host -> 01:27 GPS-disciplined

    def test_synced_clock_leaves_timestamp_untouched(self) -> None:
        from helmlog.clock_sync import ClockFlag
        from helmlog.sk_reader import ClockDiscipliner

        clock = ClockDiscipliner()
        pos = {"latitude": 47.68, "longitude": -122.41}
        for i in range(6):
            host = datetime(2026, 6, 16, 1, 30, i, tzinfo=UTC)
            gps = host + timedelta(seconds=1)  # within SKEW_OK
            process_delta(
                self._gps_pos_delta(host.isoformat(), gps.isoformat(), pos),
                {},
                clock=clock,
            )
        assert clock.flag is ClockFlag.SYNCED
