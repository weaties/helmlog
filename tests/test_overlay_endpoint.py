"""Tests for the /api/maneuvers/overlay endpoint and its series builder (#619)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from httpx import ASGITransport

from helmlog.analysis.maneuvers import (
    ENRICH_CACHE_VERSION,
    build_maneuvers_overlay,
    enrich_session_maneuvers,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _seed_session(
    storage: Storage,
    *,
    session_id: int,
    name: str,
    htw_offsets_s: list[int],
    type_: str = "tack",
) -> None:
    """Seed a session with evenly-spaced maneuvers whose HTW timestamps
    land at ``session_start + offset`` seconds for each entry in
    ``htw_offsets_s``. Also seeds 180 s of 1 Hz headings/speeds/winds/
    positions around each HTW so the overlay loader finds real data to
    align.
    """
    db = storage._conn()
    start = datetime(2026, 4, 20, 14, 0, 0, tzinfo=UTC)
    end = start + timedelta(seconds=max(htw_offsets_s) + 120)

    await db.execute(
        "INSERT INTO races"
        " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, 'e', ?, ?, 'race', ?, ?)",
        (
            session_id,
            name,
            session_id,
            start.date().isoformat(),
            start.isoformat(),
            end.isoformat(),
        ),
    )

    # Seed instruments across the whole window (1 Hz). We just write
    # steady values — the overlay doesn't care about the *content*, it
    # cares about alignment, length, and null handling.
    total_s = max(htw_offsets_s) + 120
    for i in range(total_s + 1):
        ts = (start + timedelta(seconds=i)).isoformat()
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
            (ts, 0x05, 45.0 + i * 0.1),  # slowly changing hdg → non-zero rate
        )
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
            (ts, 0x05, 6.0),
        )
        await db.execute(
            "INSERT INTO winds"
            " (ts, source_addr, wind_speed_kts, wind_angle_deg, reference) VALUES (?, ?, ?, ?, 0)",
            (ts, 0x05, 12.0, 40.0),
        )
        await db.execute(
            "INSERT INTO positions"
            " (ts, source_addr, latitude_deg, longitude_deg) VALUES (?, ?, ?, ?)",
            (ts, 0x05, 37.0 + i * 1e-5, -122.0),
        )

    # One maneuver per requested offset. head_to_wind_ts lands on that
    # offset; ts and end_ts wrap it symmetrically so the enrichment
    # payload passes its preconditions.
    for mid, off in enumerate(htw_offsets_s, start=session_id * 100):
        htw = start + timedelta(seconds=off)
        await db.execute(
            "INSERT INTO maneuvers"
            " (id, session_id, type, ts, end_ts, head_to_wind_ts,"
            "  duration_sec, loss_kts, vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (?, ?, ?, ?, ?, ?, 10.0, 3.0, NULL, 12, 40, NULL)",
            (
                mid,
                session_id,
                type_,
                (htw - timedelta(seconds=5)).isoformat(),
                (htw + timedelta(seconds=5)).isoformat(),
                htw.isoformat(),
            ),
        )
    await db.commit()


class TestBuildManeuversOverlay:
    @pytest.mark.asyncio
    async def test_three_maneuvers_return_length_51_series(self, storage: Storage) -> None:
        await _seed_session(storage, session_id=1, name="s1", htw_offsets_s=[60, 120, 180])
        pairs = [(1, 100), (1, 101), (1, 102)]
        payload = await build_maneuvers_overlay(storage, pairs)

        assert payload["channels"] == ["bsp", "heading_rate_deg_s", "twa"]
        assert len(payload["axis_s"]) == 51
        assert payload["axis_s"][0] == -20
        assert payload["axis_s"][-1] == 30
        assert len(payload["maneuvers"]) == 3
        for m in payload["maneuvers"]:
            assert len(m["bsp"]) == 51
            assert len(m["heading_rate_deg_s"]) == 51
            assert len(m["twa"]) == 51
            # Full-payload parity with the session page: every key
            # the UI reads to render the wind-up track SVG or the
            # maneuver table must be present on each maneuver.
            for field in (
                "track",
                "ghost_m",
                "twd_deg",
                "ts",
                "duration_sec",
                "turn_angle_deg",
                "distance_loss_m",
                "loss_kts",
                "entry_bsp",
                "exit_bsp",
                "min_bsp",
                "entry_twa",
                "entry_tws",
                "time_to_head_to_wind_s",
                "time_to_recover_s",
                "youtube_url",
            ):
                assert field in m, f"missing {field!r} in maneuver payload"
        assert payload["excluded_ids"] == []

    @pytest.mark.asyncio
    async def test_null_head_to_wind_excluded_with_notice(self, storage: Storage) -> None:
        # Seed one tack and one rounding. Roundings get head_to_wind_ts
        # = None from the enrichment pass (#613's contract), so they're
        # the natural real-world case the exclusion branch catches.
        await _seed_session(storage, session_id=2, name="s2", htw_offsets_s=[60])
        db = storage._conn()
        # Second maneuver: a rounding, which will not get a HTW
        # assigned by enrichment even if signed TWA is present.
        rounding_ts = (
            datetime(2026, 4, 20, 14, 0, 0, tzinfo=UTC) + timedelta(seconds=120)
        ).isoformat()
        await db.execute(
            "INSERT INTO maneuvers"
            " (id, session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (299, 2, 'rounding', ?, ?, 10.0, 3.0, NULL, 12, 40, NULL)",
            (rounding_ts, rounding_ts),
        )
        await db.commit()
        await storage.invalidate_session_maneuver_cache(2)

        pairs = [(2, 200), (2, 299)]
        payload = await build_maneuvers_overlay(storage, pairs)

        assert len(payload["maneuvers"]) == 1
        assert payload["maneuvers"][0]["maneuver_id"] == 200
        assert "2:299" in payload["excluded_ids"]

    @pytest.mark.asyncio
    async def test_cross_session_selection_returns_all(self, storage: Storage) -> None:
        await _seed_session(storage, session_id=3, name="a", htw_offsets_s=[60])
        await _seed_session(storage, session_id=4, name="b", htw_offsets_s=[60])
        pairs = [(3, 300), (4, 400)]
        payload = await build_maneuvers_overlay(storage, pairs)
        names = sorted(m["session_name"] for m in payload["maneuvers"])
        assert names == ["a", "b"]

    @pytest.mark.asyncio
    async def test_empty_pairs_yields_empty_payload(self, storage: Storage) -> None:
        payload = await build_maneuvers_overlay(storage, [])
        assert payload["maneuvers"] == []
        assert payload["excluded_ids"] == []

    @pytest.mark.asyncio
    async def test_series_values_align_at_zero(self, storage: Storage) -> None:
        # The BSP series is steady 6.0 at every sample, so the overlay
        # window should be fully populated (no nulls) for a maneuver
        # whose HTW is well inside the seeded range.
        await _seed_session(storage, session_id=5, name="s5", htw_offsets_s=[120])
        payload = await build_maneuvers_overlay(storage, [(5, 500)])
        assert len(payload["maneuvers"]) == 1
        bsp = payload["maneuvers"][0]["bsp"]
        # t=0 index is 20 (position of 0 in [-20..30]).
        assert bsp[20] is not None
        assert abs(bsp[20] - 6.0) < 0.01

    @pytest.mark.asyncio
    async def test_overlay_served_from_cache_without_instrument_rescan(
        self, storage: Storage
    ) -> None:
        """The aligned series live on the cached enriched payload, so
        ``build_maneuvers_overlay`` must keep working after the
        instrument tables are wiped — proving it does not re-scan them
        per request. Regression guard for the slow cold path that
        rebuilt headings/speeds/winds for every overlay request."""
        await _seed_session(storage, session_id=10, name="cached", htw_offsets_s=[60])
        # Prime the maneuver_cache with v8 enrichment.
        await enrich_session_maneuvers(storage, 10)

        db = storage._conn()
        for table in ("headings", "speeds", "winds", "positions"):
            await db.execute(f"DELETE FROM {table}")  # noqa: S608
        await db.commit()

        payload = await build_maneuvers_overlay(storage, [(10, 1000)])
        assert len(payload["maneuvers"]) == 1
        m = payload["maneuvers"][0]
        assert len(m["bsp"]) == 51
        # Steady BSP=6.0 was seeded; the cache holds the aligned values.
        assert m["bsp"][20] is not None
        assert abs(m["bsp"][20] - 6.0) < 0.01

    @pytest.mark.asyncio
    async def test_enriched_payload_contains_overlay_arrays(self, storage: Storage) -> None:
        """The enrichment step is the single producer of the aligned
        overlay arrays; ``build_maneuvers_overlay`` only forwards them.
        Verify the contract directly so the producer can't quietly
        drop fields without breaking this test."""
        await _seed_session(storage, session_id=11, name="enr", htw_offsets_s=[60])
        enriched, _ = await enrich_session_maneuvers(storage, 11)
        assert len(enriched) == 1
        m = enriched[0]
        assert isinstance(m["overlay_bsp"], list) and len(m["overlay_bsp"]) == 51
        assert (
            isinstance(m["overlay_heading_rate_deg_s"], list)
            and len(m["overlay_heading_rate_deg_s"]) == 51
        )
        assert isinstance(m["overlay_twa"], list) and len(m["overlay_twa"]) == 51


class TestOverlayEndpoint:
    """HTTP-level tests going through the route + (disabled) cache."""

    @pytest.mark.asyncio
    async def test_endpoint_returns_expected_shape(self, storage: Storage) -> None:
        await _seed_session(storage, session_id=6, name="s6", htw_offsets_s=[60, 120])
        from helmlog.web import create_app

        # Auth is bypassed when this env is unset by tests; create_app
        # wires a storage into app.state.storage from the arg.
        app = create_app(storage)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/maneuvers/overlay", params={"ids": "6:600,6:601"})
            assert r.status_code == 200
            body = r.json()
            assert body["channels"] == ["bsp", "heading_rate_deg_s", "twa"]
            assert len(body["axis_s"]) == 51
            assert len(body["maneuvers"]) == 2
            assert body["excluded_ids"] == []

    @pytest.mark.asyncio
    async def test_endpoint_rejects_malformed_ids(self, storage: Storage) -> None:
        from helmlog.web import create_app

        app = create_app(storage)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/maneuvers/overlay", params={"ids": "garbage"})
            assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_single_session_and_cross_session_produce_same_shape(
        self, storage: Storage
    ) -> None:
        """The AC requires that a single-session entry URL yields the same
        JSON payload shape as a cross-session entry URL with equivalent
        ids — one code path, one endpoint."""
        await _seed_session(storage, session_id=7, name="ss", htw_offsets_s=[60, 120, 180])
        await _seed_session(storage, session_id=8, name="xs", htw_offsets_s=[60])

        from helmlog.web import create_app

        app = create_app(storage)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            ss = await c.get("/api/maneuvers/overlay", params={"ids": "7:700,7:701,7:702"})
            xs = await c.get("/api/maneuvers/overlay", params={"ids": "7:700,7:701,7:702,8:800"})
        # Same keys on every maneuver object regardless of entry-path.
        ss_keys = {frozenset(m.keys()) for m in ss.json()["maneuvers"]}
        xs_keys = {frozenset(m.keys()) for m in xs.json()["maneuvers"]}
        assert ss_keys == xs_keys

    @pytest.mark.asyncio
    async def test_cache_hit_returns_identical_payload(self, storage: Storage) -> None:
        from helmlog.cache import WebCache
        from helmlog.web import create_app

        app = create_app(storage)
        app.state.web_cache = WebCache(storage)

        await _seed_session(storage, session_id=9, name="cache", htw_offsets_s=[60])

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            first = await c.get("/api/maneuvers/overlay", params={"ids": "9:900"})
            second = await c.get("/api/maneuvers/overlay", params={"ids": "9:900"})
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json()

    @pytest.mark.asyncio
    async def test_enrich_version_in_cache_hash(self, storage: Storage) -> None:
        """Sanity: ENRICH_CACHE_VERSION is referenced from the endpoint
        (via build_maneuvers_overlay and the route's hash-input dict).
        If it's ever dropped from the hash input, bumping versions
        would show stale overlay payloads until TTL."""
        # Import site verifies the endpoint's hash-input includes it.
        import helmlog.routes.sessions as sessions_mod

        src = sessions_mod.__file__ or ""
        with open(src, encoding="utf-8") as f:
            text = f.read()
        assert "ENRICH_CACHE_VERSION" in text
        assert "enrich_version" in text  # hash-input key name
        # And the constant actually has a current value.
        assert ENRICH_CACHE_VERSION >= 6
