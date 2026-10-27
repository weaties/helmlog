"""Web routes for the manual "trim non-race data" feature (#11):
GET /api/races/{id}/trim-preview, POST /api/races/{id}/trim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_START_LAT = 37.80
_START_LON = -122.27


async def _insert_track_point(
    storage: Storage,
    race_id: int | None,
    ts: datetime,
    *,
    near: bool,
    close_hauled: bool,
) -> None:
    lat = _START_LAT + (0.001 if near else 0.03)
    twa = 40.0 if close_hauled else 140.0
    db = storage._conn()  # noqa: SLF001
    await db.execute(
        "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (ts.isoformat(), 1, lat, _START_LON + 0.001, race_id),
    )
    await db.execute(
        "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference, race_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts.isoformat(), 1, 10.0, twa, 0, race_id),
    )
    await db.commit()


async def _seed_open_race_with_tail(storage: Storage) -> tuple[int, datetime]:
    gun = datetime.now(UTC) - timedelta(hours=3)
    race = await storage.start_race("CYC", gun, gun.date().isoformat(), 1, "R1")
    for t in range(0, 201, 10):
        await _insert_track_point(
            storage, race.id, gun + timedelta(seconds=t), near=True, close_hauled=True
        )
    for t in range(210, 3 * 3600, 600):
        await _insert_track_point(
            storage, race.id, gun + timedelta(seconds=t), near=False, close_hauled=False
        )
    return race.id, gun + timedelta(seconds=200)


@pytest.mark.asyncio
async def test_trim_preview_detects_cutoff(storage: Storage) -> None:
    race_id, expected_cutoff = await _seed_open_race_with_tail(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/races/{race_id}/trim-preview")

    assert resp.status_code == 200
    data = resp.json()
    assert data["detected"] is True
    assert data["cutoff_utc"] == expected_cutoff.isoformat()
    assert data["rows_by_table"]["positions"] > 0


@pytest.mark.asyncio
async def test_trim_preview_not_detected(storage: Storage) -> None:
    now = datetime.now(UTC)
    race = await storage.start_race("CYC", now - timedelta(minutes=5), "2026-06-09", 1, "R1")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/races/{race.id}/trim-preview")

    assert resp.status_code == 200
    assert resp.json()["detected"] is False


@pytest.mark.asyncio
async def test_trim_preview_missing_race_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/races/99999/trim-preview")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trim_apply_closes_race_and_detaches(storage: Storage) -> None:
    race_id, cutoff = await _seed_open_race_with_tail(storage)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/races/{race_id}/trim",
            json={"cutoff_utc": cutoff.isoformat(), "source": "heuristic"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["rows_detached"] > 0

    race = await storage.get_race(race_id)
    assert race is not None and race.end_utc == cutoff

    audit_log = await storage.list_audit_log(limit=10)
    assert any(e["action"] == "race.trim" for e in audit_log)


@pytest.mark.asyncio
async def test_trim_apply_missing_race_404(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/races/99999/trim",
            json={"cutoff_utc": datetime.now(UTC).isoformat(), "source": "manual"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trim_apply_noop_returns_zero(storage: Storage) -> None:
    now = datetime.now(UTC)
    race = await storage.start_race("CYC", now - timedelta(hours=1), "2026-06-09", 1, "R1")
    await storage.end_race(race.id, now - timedelta(minutes=1))
    race_before = await storage.get_race(race.id)
    assert race_before is not None and race_before.end_utc is not None
    late_cutoff = race_before.end_utc + timedelta(minutes=5)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/races/{race.id}/trim",
            json={"cutoff_utc": late_cutoff.isoformat(), "source": "manual"},
        )

    assert resp.status_code == 200
    assert resp.json()["rows_detached"] == 0
