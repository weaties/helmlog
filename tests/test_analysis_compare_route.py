"""Tests for the /api/analysis/maneuver-compare HTTP route (#741)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest_asyncio.fixture
async def client(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:  # type: ignore[misc]
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


_BASE_TS = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_session(
    storage: Storage,
    *,
    session_id: int,
    name: str,
    base_offset_h: int,
    distance_loss_m: float,
) -> None:
    """Seed one session with one tack — distance_loss_m derived from the
    geometry of the seeded positions, not directly stored. We approximate
    by adjusting the position drift so the enriched value lands near the
    target. Good enough for ordering tests.
    """
    db = storage._conn()
    start = _BASE_TS + timedelta(hours=base_offset_h)
    end = start + timedelta(seconds=180)
    await db.execute(
        "INSERT INTO races"
        " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            name,
            "test-event",
            session_id,
            start.date().isoformat(),
            "race",
            start.isoformat(),
            end.isoformat(),
        ),
    )

    # 180s of 1Hz instrument data with a tack at t=90.
    for i in range(181):
        ts = (start + timedelta(seconds=i)).isoformat()
        hdg = 10.0 if i < 90 else 280.0
        bsp = 6.0 if i < 85 or i > 100 else 3.0
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
            (ts, 0x05, hdg),
        )
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (ts, 0x05, bsp),
        )
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, 0)",
            (ts, 0x05, 12.0, 40.0),
        )
        # Position drift roughly along entry COG so enrichment can compute loss.
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 0x05, 37.0 + i * 1e-5, -122.0),
        )

    # Stored maneuver row at t=90.
    await db.execute(
        "INSERT INTO maneuvers"
        " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
        "  vmg_loss_kts, tws_bin, twa_bin, details)"
        " VALUES (?, ?, ?, ?, 10.0, ?, NULL, 12, 40, NULL)",
        (
            session_id,
            "tack",
            (start + timedelta(seconds=90)).isoformat(),
            (start + timedelta(seconds=100)).isoformat(),
            distance_loss_m,  # piggyback as loss_kts so our metric ranking can fire
        ),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_compare_returns_best_and_median_disjoint(
    client: httpx.AsyncClient, storage: Storage
) -> None:
    # Seven sessions with monotonically increasing loss_kts.
    for i, loss in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0], start=1):
        await _seed_session(
            storage,
            session_id=i,
            name=f"s{i}",
            base_offset_h=i * 2,
            distance_loss_m=loss,
        )

    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "tack", "metric": "loss_kts", "n": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filters"]["type"] == "tack"
    assert data["filters"]["metric"] == "loss_kts"
    assert data["pool_size"] == 7
    assert len(data["best"]) == 3
    assert len(data["median"]) == 3
    best_ids = {m["id"] for m in data["best"]}
    median_ids = {m["id"] for m in data["median"]}
    assert best_ids.isdisjoint(median_ids)
    # Best 3 should be the 3 lowest loss values.
    best_losses = sorted(m["loss_kts"] for m in data["best"])
    assert best_losses == [1.0, 2.0, 3.0]
    # Each cell carries the fields the UI deep-link needs to build
    # /session/{id}/{slug}?t=<offset>: session_id, session_start_utc, ts.
    for m in data["best"]:
        assert m.get("session_id") is not None
        assert m.get("session_start_utc") is not None
        assert m.get("ts") is not None


@pytest.mark.asyncio
async def test_compare_rejects_unknown_type(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "loop-de-loop", "metric": "distance_loss_m"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_compare_rejects_unknown_metric(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "tack", "metric": "vibe_score"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_compare_rejects_inverted_tws_range(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "tack", "metric": "loss_kts", "tws_min": 12, "tws_max": 8},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_compare_empty_pool_returns_zero_counts(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "gybe", "metric": "loss_kts"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pool_size"] == 0
    assert data["best"] == []
    assert data["median"] == []


@pytest.mark.asyncio
async def test_compare_rejects_unknown_direction(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "tack", "metric": "loss_kts", "direction": "leftways"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_compare_post_start_excludes_pre_gun(
    client: httpx.AsyncClient, storage: Storage
) -> None:
    """Two tacks in one session — one before the race start_utc, one after.
    With post_start=1 only the post-gun tack survives.
    """
    db = storage._conn()
    start = _BASE_TS
    end = start + timedelta(minutes=30)
    await db.execute(
        "INSERT INTO races"
        " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "race1",
            "evt",
            1,
            start.date().isoformat(),
            "race",
            start.isoformat(),
            end.isoformat(),
        ),
    )
    # Two maneuver rows: one 5min before start_utc, one 5min after.
    await db.execute(
        "INSERT INTO maneuvers"
        " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
        "  vmg_loss_kts, tws_bin, twa_bin, details)"
        " VALUES (?, ?, ?, ?, 10.0, 1.0, NULL, 12, 40, NULL)",
        (
            1,
            "tack",
            (start - timedelta(minutes=5)).isoformat(),
            (start - timedelta(minutes=4)).isoformat(),
        ),
    )
    await db.execute(
        "INSERT INTO maneuvers"
        " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
        "  vmg_loss_kts, tws_bin, twa_bin, details)"
        " VALUES (?, ?, ?, ?, 10.0, 2.0, NULL, 12, 40, NULL)",
        (
            1,
            "tack",
            (start + timedelta(minutes=5)).isoformat(),
            (start + timedelta(minutes=6)).isoformat(),
        ),
    )
    await db.commit()

    # Without post_start: both maneuvers in pool.
    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "tack", "metric": "loss_kts"},
    )
    assert resp.json()["pool_size"] == 2

    # With post_start=1: only the post-gun tack.
    resp = await client.get(
        "/api/analysis/maneuver-compare",
        params={"type": "tack", "metric": "loss_kts", "post_start": "1"},
    )
    data = resp.json()
    assert data["pool_size"] == 1
    assert data["filters"]["post_start"] is True


@pytest.mark.asyncio
async def test_compare_page_renders(client: httpx.AsyncClient) -> None:
    """The /analysis/maneuver-compare page renders the filter form."""
    resp = await client.get("/analysis/maneuver-compare")
    assert resp.status_code == 200
    body = resp.text
    assert "Best vs Median Maneuvers" in body
    assert 'id="mc-form"' in body
    # All four maneuver types are filter options.
    assert 'value="tack"' in body
    assert 'value="gybe"' in body
    assert 'value="weather_rounding"' in body
    assert 'value="leeward_rounding"' in body
    # Direction + post-start filters are present.
    assert 'id="mc-direction"' in body
    assert 'id="mc-post-start"' in body


@pytest.mark.asyncio
async def test_analysis_hub_page_renders(client: httpx.AsyncClient) -> None:
    """The /analysis hub lists the available coach analyses."""
    resp = await client.get("/analysis")
    assert resp.status_code == 200
    body = resp.text
    assert "Analysis" in body
    # Both tools should be linked.
    assert 'href="/analysis/maneuver-compare"' in body
    assert 'href="/maneuvers"' in body
