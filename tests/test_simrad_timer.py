"""Tests for the HelmLogPublisher in scripts/simrad_timer_sk.py.

Decoder logic has moved to helmlog.nmea2000 — see test_nmea2000.py for
FastPacketBuffer and SimradTimerRecord decode tests.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import simrad_timer_sk as sut  # noqa: E402

from helmlog.nmea2000 import (  # noqa: E402
    PGN_SIMRAD_SET_TIMER,
    PGN_SIMRAD_START_STOP,
    SimradTimerRecord,
)


def _record(action: str, minutes: int | None = None) -> SimradTimerRecord:
    pgn = PGN_SIMRAD_SET_TIMER if action == "set" else PGN_SIMRAD_START_STOP
    return SimradTimerRecord(
        pgn=pgn,
        source_addr=0x09,
        timestamp=datetime(2026, 5, 18, 17, 0, 0, tzinfo=UTC),
        action=action,
        minutes=minutes,
    )


def _make_publisher() -> tuple[sut.HelmLogPublisher, AsyncMock]:
    pub = sut.HelmLogPublisher.__new__(sut.HelmLogPublisher)
    pub._url = "http://test/api/internal/timer-event"  # type: ignore[attr-defined]
    pub._headers = {}  # type: ignore[attr-defined]
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.elapsed = MagicMock()
    mock_response.elapsed.microseconds = 0
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    pub._client = mock_client  # type: ignore[attr-defined]
    return pub, mock_client


def _posted_body(mock_client: AsyncMock) -> dict:
    return mock_client.post.call_args.kwargs["json"]


@pytest.mark.asyncio
async def test_publish_start_posts_running() -> None:
    pub, client = _make_publisher()
    await pub.publish(_record("start"))
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_STATE
    assert body["value"] == "running"


@pytest.mark.asyncio
async def test_publish_stop_posts_stopped() -> None:
    pub, client = _make_publisher()
    await pub.publish(_record("stop"))
    body = _posted_body(client)
    assert body["value"] == "stopped"


@pytest.mark.asyncio
async def test_publish_reset_posts_reset() -> None:
    pub, client = _make_publisher()
    await pub.publish(_record("reset"))
    body = _posted_body(client)
    assert body["value"] == "reset"


@pytest.mark.asyncio
async def test_publish_nearest_minute_posts_state() -> None:
    pub, client = _make_publisher()
    await pub.publish(_record("nearest_minute"))
    body = _posted_body(client)
    assert body["value"] == "nearest-minute"


@pytest.mark.asyncio
async def test_publish_set_posts_duration_seconds() -> None:
    pub, client = _make_publisher()
    await pub.publish(_record("set", minutes=5))
    body = _posted_body(client)
    assert body["path"] == sut.SK_PATH_DURATION
    assert body["value"] == 300  # 5 min × 60


@pytest.mark.asyncio
async def test_publish_set_6_minutes() -> None:
    pub, client = _make_publisher()
    await pub.publish(_record("set", minutes=6))
    body = _posted_body(client)
    assert body["value"] == 360
