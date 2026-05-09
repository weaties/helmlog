"""Tests for WebSocket live push (/ws/live)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from helmlog.nmea2000 import HeadingRecord, PositionRecord
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest.mark.asyncio
async def test_ws_connect_and_receive_snapshot(storage: Storage) -> None:
    """WebSocket client receives an initial instrument snapshot on connect."""
    app = create_app(storage)
    with TestClient(app) as client, client.websocket_connect("/ws/live") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "instruments"
        assert "data" in msg


@pytest.mark.asyncio
async def test_ws_disconnect_cleanup(storage: Storage) -> None:
    """Disconnected clients are removed from the broadcast set."""
    app = create_app(storage)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.receive_json()  # snapshot
        # After disconnect, broadcast set should be empty
        assert len(app.state.ws_clients) == 0


@pytest.mark.asyncio
async def test_live_callback_is_registered(storage: Storage) -> None:
    """create_app() registers a live update callback on storage."""
    create_app(storage)
    assert storage._on_live_update is not None


@pytest.mark.asyncio
async def test_live_callback_fires_on_update(storage: Storage) -> None:
    """Storage.update_live() invokes the registered callback with instrument data."""
    received: list[dict] = []  # type: ignore[type-arg]

    def cb(data: dict) -> None:  # type: ignore[type-arg]
        received.append(data)

    storage.set_live_callback(cb)
    record = HeadingRecord(
        pgn=127250,
        source_addr=0,
        timestamp=datetime.now(UTC),
        heading_deg=245.3,
        deviation_deg=None,
        variation_deg=None,
    )
    storage.update_live(record)

    assert len(received) == 1
    assert received[0]["heading_deg"] == 245.3


@pytest.mark.asyncio
async def test_position_broadcast_buckets_per_second_and_emits_mean(
    storage: Storage,
) -> None:
    """Same-second fixes accumulate; the bucket emits its mean only when
    the next second arrives. Mirrors the per-second mean averaging in
    routes.sessions._compute_session_track so the live polyline reads
    the same as the historical track (#732)."""
    received: list[dict] = []  # type: ignore[type-arg]
    storage.set_position_callback(received.append)

    # 5 fixes in the same second, alternating between two GPS antennas
    # 3 m apart in latitude (the dual-antenna zig-zag this fix targets).
    base = datetime(2026, 5, 2, 16, 30, 0, tzinfo=UTC)
    lats = [47.6500, 47.6500 + 3e-5, 47.6500, 47.6500 + 3e-5, 47.6500]
    for i, lat in enumerate(lats):
        storage.update_live(
            PositionRecord(
                pgn=129025,
                source_addr=0,
                timestamp=base.replace(microsecond=i * 100_000),
                latitude_deg=lat,
                longitude_deg=-122.40,
            )
        )
    # No broadcast yet — the second hasn't rolled over.
    assert received == []

    # Tick into the next whole second → the previous bucket's mean fires.
    storage.update_live(
        PositionRecord(
            pgn=129025,
            source_addr=0,
            timestamp=base.replace(second=1),
            latitude_deg=47.66,
            longitude_deg=-122.40,
        )
    )
    assert len(received) == 1
    # Mean of the 5 alternating-antenna fixes lands midway between them.
    assert received[0]["lat"] == pytest.approx(47.6500 + 1.2e-5, abs=1e-9)
    assert received[0]["lon"] == pytest.approx(-122.40, abs=1e-9)
    # Timestamp is the first fix of the bucket.
    assert received[0]["ts"].startswith("2026-05-02T16:30:00")


@pytest.mark.asyncio
async def test_position_broadcast_one_message_per_second(storage: Storage) -> None:
    """Three buckets across three seconds → exactly two broadcasts (the
    first two; the third is still accumulating). Confirms cadence."""
    received: list[dict] = []  # type: ignore[type-arg]
    storage.set_position_callback(received.append)

    base = datetime(2026, 5, 2, 16, 30, 0, tzinfo=UTC)
    for sec in range(3):
        for sub in range(4):
            storage.update_live(
                PositionRecord(
                    pgn=129025,
                    source_addr=0,
                    timestamp=base.replace(second=sec, microsecond=sub * 100_000),
                    latitude_deg=47.65 + sec * 0.001,
                    longitude_deg=-122.40,
                )
            )
    # Two complete buckets + one in-flight bucket.
    assert len(received) == 2


@pytest.mark.asyncio
async def test_position_record_does_not_fire_instrument_callback(storage: Storage) -> None:
    """A PositionRecord must not trigger the instruments broadcast — it
    has no instrument fields and would just spam an unchanged snapshot."""
    inst_calls: list[dict] = []  # type: ignore[type-arg]
    pos_calls: list[dict] = []  # type: ignore[type-arg]

    storage.set_live_callback(lambda d: inst_calls.append(d))
    storage.set_position_callback(lambda p: pos_calls.append(p))

    base = datetime(2026, 5, 2, 16, 31, 0, tzinfo=UTC)
    # First fix seeds the bucket — no broadcast yet.
    storage.update_live(
        PositionRecord(
            pgn=129025,
            source_addr=0,
            timestamp=base,
            latitude_deg=47.65,
            longitude_deg=-122.40,
        )
    )
    # Second-second fix flushes the previous bucket.
    storage.update_live(
        PositionRecord(
            pgn=129025,
            source_addr=0,
            timestamp=base.replace(second=1),
            latitude_deg=47.66,
            longitude_deg=-122.40,
        )
    )
    assert len(pos_calls) == 1
    assert len(inst_calls) == 0


@pytest.mark.asyncio
async def test_polling_endpoints_still_work(storage: Storage) -> None:
    """Existing HTTP polling endpoints continue to work alongside WebSocket."""
    import httpx

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/instruments")
        assert resp.status_code == 200
        data = resp.json()
        assert "heading_deg" in data
