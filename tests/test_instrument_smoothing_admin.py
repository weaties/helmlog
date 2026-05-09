"""Admin API + storage integration for instrument smoothing (#727)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.nmea2000 import HeadingRecord, SpeedRecord, WindRecord
from helmlog.smoothing import DEFAULT_TAUS
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest_asyncio.fixture
async def admin_client(  # type: ignore[misc]
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> httpx.AsyncClient:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_get_returns_defaults_when_unset(admin_client: httpx.AsyncClient) -> None:
    """Fresh DB → endpoint reports DEFAULT_TAUS for every channel."""
    r = await admin_client.get("/api/admin/instrument-smoothing")
    assert r.status_code == 200
    body = r.json()
    for channel, default in DEFAULT_TAUS.items():
        assert channel in body
        assert body[channel]["tau_s"] == default
        assert body[channel]["default"] == default


@pytest.mark.asyncio
async def test_put_persists_and_get_reflects(admin_client: httpx.AsyncClient) -> None:
    """Setting a channel via PUT → GET returns the new value."""
    r = await admin_client.put(
        "/api/admin/instrument-smoothing",
        json={"tws_kts": 8.0, "twa_deg": 7.5},
    )
    assert r.status_code == 204
    body = (await admin_client.get("/api/admin/instrument-smoothing")).json()
    assert body["tws_kts"]["tau_s"] == 8.0
    assert body["twa_deg"]["tau_s"] == 7.5
    # Untouched channels stay at default.
    assert body["sog_kts"]["tau_s"] == DEFAULT_TAUS["sog_kts"]


@pytest.mark.asyncio
async def test_put_rejects_unknown_channel(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.put("/api/admin/instrument-smoothing", json={"bogus_channel": 5.0})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_rejects_invalid_tau(admin_client: httpx.AsyncClient) -> None:
    """tau<=0 and non-numeric strings fail with 400. NaN is exercised by
    the unit tests in test_smoothing.py — httpx itself rejects
    out-of-range floats during JSON encode, so we can't drive that path
    through a real HTTP call here."""
    for bad in [0, -1, "not-a-number"]:
        r = await admin_client.put("/api/admin/instrument-smoothing", json={"tws_kts": bad})
        assert r.status_code == 400, f"tau={bad!r} should have been rejected"


@pytest.mark.asyncio
async def test_put_applies_to_live_storage(
    admin_client: httpx.AsyncClient, storage: Storage
) -> None:
    """After PUT, ``storage.update_live`` uses the new tau immediately —
    no service restart, no manual reload."""
    await admin_client.put("/api/admin/instrument-smoothing", json={"tws_kts": 0.5})
    # The smoother for tws_kts now has tau=0.5.
    sm = storage._smoothing.smoothers["tws_kts"]
    assert sm.tau_s == 0.5


@pytest.mark.asyncio
async def test_smoothed_value_is_blend_of_history(storage: Storage) -> None:
    """update_live(WindRecord) feeds raw through the EMA — successive calls
    blend rather than slamming to the latest raw value."""
    await storage.refresh_smoothing()
    storage._smoothing.set_tau("tws_kts", 5.0)
    base_ts = datetime(2026, 5, 2, 16, 0, 0, tzinfo=UTC)
    storage.update_live(
        WindRecord(
            pgn=130306,
            source_addr=0,
            timestamp=base_ts,
            wind_speed_kts=10.0,
            wind_angle_deg=45.0,
            reference=4,
        )
    )
    # First sample seeds the smoother at 10.0.
    assert storage._live["tws_kts"] == 10.0
    storage.update_live(
        WindRecord(
            pgn=130306,
            source_addr=0,
            timestamp=base_ts,
            wind_speed_kts=20.0,
            wind_angle_deg=45.0,
            reference=4,
        )
    )
    # Second sample at near-zero dt → smoothed value barely moves.
    assert storage._live["tws_kts"] is not None
    assert storage._live["tws_kts"] < 15.0  # not slammed to 20


@pytest.mark.asyncio
async def test_heading_smoothing_is_angle_aware(storage: Storage) -> None:
    """Heading smoother uses vector EMA — successive 359° → 1° samples
    don't swing the smoothed value through 180°."""
    await storage.refresh_smoothing()
    storage._smoothing.set_tau("heading_deg", 1.0)
    base_ts = datetime(2026, 5, 2, 16, 0, 0, tzinfo=UTC)
    storage.update_live(
        HeadingRecord(
            pgn=127250,
            source_addr=0,
            timestamp=base_ts,
            heading_deg=359.0,
            deviation_deg=None,
            variation_deg=None,
        )
    )
    storage.update_live(
        HeadingRecord(
            pgn=127250,
            source_addr=0,
            timestamp=base_ts,
            heading_deg=1.0,
            deviation_deg=None,
            variation_deg=None,
        )
    )
    # Result should be close to 0° / 360°, not anywhere near 180°.
    val = storage._live["heading_deg"]
    assert val is not None
    norm = ((val + 180) % 360) - 180
    assert abs(norm) < 30, f"heading EMA crossed 360 the long way: got {val}°"


@pytest.mark.asyncio
async def test_create_boat_settings_hot_refreshes_leeway_k(storage: Storage) -> None:
    """Writing leeway_coefficient via create_boat_settings should
    immediately update the cached storage._leeway_k — admin tunes the
    coefficient and the next live broadcast picks it up, no restart."""
    from datetime import UTC, datetime

    # Default after connect is 10.0 (no boat_settings rows yet).
    assert storage._leeway_k == 10.0
    ts = datetime.now(UTC).isoformat()
    await storage.create_boat_settings(
        race_id=None,
        entries=[{"ts": ts, "parameter": "leeway_coefficient", "value": "13.5"}],
        source="manual",
    )
    assert storage._leeway_k == 13.5


@pytest.mark.asyncio
async def test_set_drift_derived_from_smoothed_inputs(storage: Storage) -> None:
    """Set / drift are computed server-side from the (already-smoothed)
    sog / cog / stw / hdg in self._live, then put through their own EMA
    so the gauge doesn't twitch on every input. Checks that update_live
    populates set_deg + drift_kts in the broadcast snapshot."""
    from helmlog.nmea2000 import COGSOGRecord

    await storage.refresh_smoothing()
    # Make every smoother near-pass-through so this test can reason
    # about steady-state values without waiting many seconds.
    for ch in ("sog_kts", "cog_deg", "bsp_kts", "heading_deg", "set_deg", "drift_kts"):
        storage._smoothing.set_tau(ch, 0.05)
    base_ts = datetime(2026, 5, 2, 16, 0, 0, tzinfo=UTC)
    # HDG = 0°, COG = 0°, STW = 5, SOG = 5 → no current.
    storage.update_live(
        HeadingRecord(
            pgn=127250,
            source_addr=0,
            timestamp=base_ts,
            heading_deg=0.0,
            deviation_deg=None,
            variation_deg=None,
        )
    )
    storage.update_live(SpeedRecord(pgn=128259, source_addr=0, timestamp=base_ts, speed_kts=5.0))
    storage.update_live(
        COGSOGRecord(pgn=129026, source_addr=0, timestamp=base_ts, cog_deg=0.0, sog_kts=5.0)
    )
    drift_no_current = storage._live["drift_kts"]
    assert drift_no_current is not None
    assert drift_no_current < 0.5  # essentially zero


@pytest.mark.asyncio
async def test_speed_record_is_smoothed(storage: Storage) -> None:
    """Confirms SpeedRecord (boat speed through water) is routed through
    the bsp_kts smoother — first sample seeds, second blends."""
    await storage.refresh_smoothing()
    storage._smoothing.set_tau("bsp_kts", 5.0)
    base_ts = datetime(2026, 5, 2, 16, 0, 0, tzinfo=UTC)
    storage.update_live(SpeedRecord(pgn=128259, source_addr=0, timestamp=base_ts, speed_kts=5.0))
    assert storage._live["bsp_kts"] == 5.0
    storage.update_live(SpeedRecord(pgn=128259, source_addr=0, timestamp=base_ts, speed_kts=15.0))
    val = storage._live["bsp_kts"]
    assert val is not None
    assert val < 10.0  # blended, not slammed
