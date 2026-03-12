"""Tests for GET /api/sessions/{id}/polar — per-session polar performance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.nmea2000 import (
    PGN_SPEED_THROUGH_WATER,
    PGN_WIND_DATA,
    SpeedRecord,
    WindRecord,
)
from helmlog.polar import _twa_bin, _tws_bin, build_polar_baseline
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_BASE_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_polar_build_endpoint(storage: Storage) -> None:
    """POST /api/polar/build rebuilds baseline and returns bin count."""
    for i in range(1, 4):
        await _seed_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/polar/build")

    assert resp.status_code == 200
    data = resp.json()
    assert data["bins_written"] >= 1


@pytest.mark.asyncio
async def test_polar_build_then_session_polar(storage: Storage) -> None:
    """Build baseline via API, then session polar returns data."""
    for i in range(1, 4):
        await _seed_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    target_id = await _seed_session(storage, 4, bsp=7.0, tws=10.0, twa=45.0)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Build baseline via API
        build_resp = await client.post("/api/polar/build")
        assert build_resp.status_code == 200

        # Now session polar should have data
        polar_resp = await client.get(f"/api/sessions/{target_id}/polar")

    assert polar_resp.status_code == 200
    data = polar_resp.json()
    assert len(data["bins"]) >= 1
    assert data["summary"] is not None


async def _seed_session(
    storage: Storage,
    race_num: int,
    bsp: float,
    tws: float,
    twa: float,
    *,
    n_samples: int = 10,
) -> int:
    """Insert a completed race + matching speed and wind records. Returns race id."""
    start = _BASE_TS + timedelta(hours=race_num)
    end = start + timedelta(seconds=n_samples)
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES (?, 'TestEvent', ?, ?, ?, ?)",
        (
            f"TestEvent-R{race_num}",
            race_num,
            start.date().isoformat(),
            start.isoformat(),
            end.isoformat(),
        ),
    )
    race_id = cur.lastrowid
    await db.commit()

    for i in range(n_samples):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, bsp))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, tws, twa, 0))

    return int(race_id) if race_id else 0


@pytest.mark.asyncio
async def test_session_polar_endpoint_empty(storage: Storage) -> None:
    """No baseline data → empty bins, null summary."""
    race_id = await _seed_session(storage, 1, bsp=6.0, tws=10.0, twa=45.0)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/polar")

    assert resp.status_code == 200
    data = resp.json()
    assert data["bins"] == []
    assert data["summary"] is None


@pytest.mark.asyncio
async def test_session_polar_endpoint_with_data(storage: Storage) -> None:
    """Session with wind+speed readings and baseline → correct bins and deltas."""
    # Seed 3 races at BSP=6.0 to build baseline, then a 4th at BSP=7.0 to query
    for i in range(1, 4):
        await _seed_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    target_id = await _seed_session(storage, 4, bsp=7.0, tws=10.0, twa=45.0)

    await build_polar_baseline(storage)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{target_id}/polar")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["bins"]) == 1

    b = data["bins"][0]
    assert b["tws_bin"] == _tws_bin(10.0)
    assert b["twa_bin"] == _twa_bin(45.0)
    assert b["session_mean_bsp"] == pytest.approx(7.0, rel=1e-3)
    assert b["baseline_mean_bsp"] == pytest.approx(6.0, rel=0.1)
    assert b["delta"] == pytest.approx(1.0, abs=0.7)
    assert b["sample_count"] == 10

    summary = data["summary"]
    assert summary is not None
    assert summary["bins_above"] == 1
    assert summary["bins_below"] == 0


@pytest.mark.asyncio
async def test_session_polar_bins_fold_symmetry(storage: Storage) -> None:
    """Port and starboard TWA fold into the same bin."""
    # Build baseline with TWA=+45
    for i in range(1, 4):
        await _seed_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    # Query session uses TWA=-45 (port tack)
    target_id = await _seed_session(storage, 4, bsp=6.5, tws=10.0, twa=-45.0)

    await build_polar_baseline(storage)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{target_id}/polar")

    data = resp.json()
    assert len(data["bins"]) == 1
    assert data["bins"][0]["twa_bin"] == _twa_bin(45.0)


@pytest.mark.asyncio
async def test_session_polar_404_unknown_session(storage: Storage) -> None:
    """Unknown session → 404."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/9999/polar")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_polar_multiple_bins(storage: Storage) -> None:
    """Session spanning two wind conditions → two bins returned."""
    # Build baseline from 3 sessions with both conditions
    for i in range(1, 4):
        await _seed_session(storage, i, bsp=6.0, tws=10.0, twa=45.0)
    for i in range(4, 7):
        await _seed_session(storage, i, bsp=5.0, tws=8.0, twa=90.0)

    await build_polar_baseline(storage)

    # Create a target session with BOTH conditions (manual — mix of wind records)
    start = _BASE_TS + timedelta(hours=10)
    end = start + timedelta(seconds=20)
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc)"
        " VALUES (?, 'TestEvent', 10, ?, ?, ?)",
        ("TestEvent-R10", start.date().isoformat(), start.isoformat(), end.isoformat()),
    )
    target_id = int(cur.lastrowid) if cur.lastrowid else 0
    await db.commit()

    for i in range(10):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 6.5))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 10.0, 45.0, 0))
    for i in range(10, 20):
        ts = start + timedelta(seconds=i)
        await storage.write(SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, ts, 5.5))
        await storage.write(WindRecord(PGN_WIND_DATA, 5, ts, 8.0, 90.0, 0))

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{target_id}/polar")

    data = resp.json()
    assert len(data["bins"]) == 2
    assert data["summary"]["dominant_tws"] in (8, 10)
