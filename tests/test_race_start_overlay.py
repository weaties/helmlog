"""HelmLog start-line overlay endpoint for the session detail page.

Exposes ping history + computed line so the frontend can draw boat/pin
markers and a time-synced bias indicator that follows the scrubber.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


_T = datetime(2026, 4, 30, 1, 25, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_overlay_404_for_unknown_race(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/sessions/9999/race-start-overlay")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_overlay_empty_when_no_pings(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Race exists but has no pings — line is null, pings list empty."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race = await storage.start_race(
        event="CYC", start_utc=_T, date_str="2026-04-30", race_num=1, name="R"
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/api/sessions/{race.id}/race-start-overlay")
    assert r.status_code == 200
    body = r.json()
    assert body["pings"] == []
    assert body["line"] is None


@pytest.mark.asyncio
async def test_overlay_line_geometry(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """Line returns boat/pin coords plus bearing + length to GPS precision."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    race = await storage.start_race(
        event="CYC", start_utc=_T, date_str="2026-04-30", race_num=1, name="R"
    )
    # ~100 m east-west line at lat 47.65.
    await storage.add_start_line_ping(
        race_id=race.id,
        end_kind="boat",
        latitude_deg=47.65,
        longitude_deg=-122.4000,
        captured_at=_T,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=race.id,
        end_kind="pin",
        latitude_deg=47.65,
        longitude_deg=-122.39866,  # ~100 m east
        captured_at=_T + timedelta(seconds=10),
        captured_by=None,
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/api/sessions/{race.id}/race-start-overlay")
    body = r.json()
    assert len(body["pings"]) == 2
    line = body["line"]
    assert line is not None
    # ~100 m line, due east → bearing ~90°.
    assert line["length_m"] == pytest.approx(100.0, abs=2.0)
    assert line["bearing_deg"] == pytest.approx(90.0, abs=1.0)
    assert line["boat_end_carried_over_from_race_id"] is None
    assert line["pin_end_carried_over_from_race_id"] is None


@pytest.mark.asyncio
async def test_overlay_flags_carried_over_ends(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the line is carried over from a prior race, surface that
    race_id so the UI can flag the line as stale."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    date = "2026-04-30"
    r1 = await storage.start_race("CYC", _T, date, 1, "R1")
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="boat",
        latitude_deg=47.65,
        longitude_deg=-122.40,
        captured_at=_T,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="pin",
        latitude_deg=47.6510,
        longitude_deg=-122.40,
        captured_at=_T,
        captured_by=None,
    )
    r2 = await storage.start_race("CYC", _T + timedelta(minutes=30), date, 2, "R2")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(f"/api/sessions/{r2.id}/race-start-overlay")
    body = r.json()
    line = body["line"]
    assert line is not None
    assert line["boat_end_carried_over_from_race_id"] == r1.id
    assert line["pin_end_carried_over_from_race_id"] == r1.id
