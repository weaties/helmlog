"""Tests for sk_reader: Signal K delta parsing and SKReader reconnect behaviour."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from helmlog.nmea2000 import (
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PositionRecord,
    SpeedRecord,
    WindRecord,
)
from helmlog.sk_reader import SK_SOURCE_ADDR, SKReader, SKReaderConfig, process_delta

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2025-08-10T14:05:30.000Z"
_TS_DT = datetime(2025, 8, 10, 14, 5, 30, tzinfo=UTC)


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

    def test_temperature_kelvin_to_celsius(self) -> None:
        buf: dict[str, float] = {}
        records = process_delta(_delta("environment.water.temperature", 293.15), buf)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, EnvironmentalRecord)
        assert abs(rec.water_temp_c - 20.0) < 1e-6

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
# TestSKReaderReconnect — reconnect and cancellation behaviour
# ---------------------------------------------------------------------------


class TestSKReaderReconnect:
    """Test SKReader reconnect and CancelledError propagation."""

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError from the outer task propagates through the reader."""

        class FakeWS:
            """WebSocket that blocks forever until cancelled."""

            async def __aenter__(self) -> FakeWS:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

            def __aiter__(self) -> AsyncGenerator[str, None]:
                return self._gen()

            async def _gen(self) -> AsyncGenerator[str, None]:
                await asyncio.sleep(3600)  # block until cancelled
                yield ""  # never reached

        with patch("helmlog.sk_reader._ws_connect", FakeWS):
            reader = SKReader(SKReaderConfig())
            task = asyncio.create_task(self._collect_one(reader))
            await asyncio.sleep(0)  # let task start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def _collect_one(self, reader: SKReader) -> None:
        async for _ in reader:
            break

    async def test_reconnects_after_normal_disconnect(self) -> None:
        """Reader reconnects immediately after a graceful WebSocket close."""
        connect_calls: list[int] = [0]

        class FakeWS:
            def __init__(self) -> None:
                self._call = connect_calls[0]
                connect_calls[0] += 1

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

        with patch("helmlog.sk_reader._ws_connect", lambda _uri: FakeWS()):
            reader = SKReader(SKReaderConfig())

            async def collect() -> list[object]:
                results: list[object] = []
                async for record in reader:
                    results.append(record)
                    # Stop once we've confirmed a second connect was attempted
                    if connect_calls[0] >= 2:
                        return results
                return results

            task = asyncio.create_task(collect())
            # Give ample time for the first WS to close and reconnect to fire
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await task

        assert connect_calls[0] >= 2, "Expected at least 2 connection attempts"

    async def test_reconnects_with_backoff_on_exception(self) -> None:
        """Reader applies backoff delay when connection raises an exception."""
        connect_calls: list[int] = [0]

        class FailWS:
            async def __aenter__(self) -> FailWS:
                connect_calls[0] += 1
                raise ConnectionRefusedError("no server")

            async def __aexit__(self, *_: object) -> None:
                pass

            def __aiter__(self) -> AsyncGenerator[str, None]:
                return self._gen()

            async def _gen(self) -> AsyncGenerator[str, None]:
                yield ""  # never reached

        with patch("helmlog.sk_reader._ws_connect", lambda _uri: FailWS()):
            reader = SKReader(SKReaderConfig(reconnect_delay_s=0.05))
            task = asyncio.create_task(self._collect_one(reader))
            await asyncio.sleep(0.15)  # let at least 2 retries fire
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert connect_calls[0] >= 1
