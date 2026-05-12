"""Tests for analysis/maneuvers.py — per-maneuver entry/exit metrics and loss calc."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

from helmlog.analysis.maneuvers import (
    ENRICH_CACHE_VERSION,
    backfill_stale_maneuver_cache,
    enrich_maneuver,
    enrich_session_maneuvers,
    extract_local_track,
    rank_maneuvers,
)

_BASE_TS = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)


def _series(values: list[float], offset_s: int = 0) -> list[tuple[datetime, float]]:
    return [(_BASE_TS + timedelta(seconds=offset_s + i), v) for i, v in enumerate(values)]


def _const(n: int, v: float, offset_s: int = 0) -> list[tuple[datetime, float]]:
    return _series([v] * n, offset_s=offset_s)


def _straight_positions(
    lat0: float, lon0: float, cog_deg: float, sog_kts: float, n: int, offset_s: int = 0
) -> list[tuple[datetime, float, float]]:
    """Generate positions moving at a constant SOG/COG from (lat0, lon0)."""
    out = []
    R = 6371000.0
    ms = sog_kts * 0.514444
    cog_rad = math.radians(cog_deg)
    for i in range(n):
        d = ms * i  # metres travelled
        dn = d * math.cos(cog_rad)
        de = d * math.sin(cog_rad)
        dlat = math.degrees(dn / R)
        dlon = math.degrees(de / (R * math.cos(math.radians(lat0))))
        out.append((_BASE_TS + timedelta(seconds=offset_s + i), lat0 + dlat, lon0 + dlon))
    return out


class TestEnrichManeuver:
    def test_entry_exit_averages_from_steady_windows(self) -> None:
        # 30s pre @ hdg=10, bsp=6.0, twa=40, tws=12
        # 10s turn (ignored for entry/exit)
        # 30s post @ hdg=280, bsp=5.5, twa=-40 → folded 40, tws=12
        turn_hdg = _series([10 + i * 27 for i in range(10)], 30)
        hdg = _const(30, 10.0) + turn_hdg + _const(30, 280.0, 40)
        bsp = _const(30, 6.0) + _const(10, 3.0, 30) + _const(30, 5.5, 40)
        twa = _const(30, 40.0) + _const(10, 0.0, 30) + _const(30, 40.0, 40)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 10.0, 6.0, 70)

        maneuver_ts = _BASE_TS + timedelta(seconds=30)
        exit_ts = _BASE_TS + timedelta(seconds=40)

        m = enrich_maneuver(
            maneuver_ts=maneuver_ts,
            exit_ts=exit_ts,
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )

        assert m.entry_bsp is not None and abs(m.entry_bsp - 6.0) < 0.01
        assert m.exit_bsp is not None and abs(m.exit_bsp - 5.5) < 0.01
        assert m.entry_hdg is not None and abs(m.entry_hdg - 10.0) < 1.0
        assert m.exit_hdg is not None and abs(m.exit_hdg - 280.0) < 1.0
        assert m.entry_twa is not None and abs(m.entry_twa - 40.0) < 0.1
        assert m.exit_twa is not None and abs(m.exit_twa - 40.0) < 0.1
        assert m.entry_tws is not None and abs(m.entry_tws - 12.0) < 0.01

    def test_min_bsp_captured_during_maneuver(self) -> None:
        hdg = _const(30, 10.0) + _const(10, 10.0, 30) + _const(30, 280.0, 40)
        bsp_during = [6.0, 5.0, 4.0, 3.0, 2.0, 1.5, 2.0, 3.0, 4.0, 5.0]
        bsp = _const(30, 6.0) + _series(bsp_during, 30) + _const(30, 5.5, 40)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 10.0, 6.0, 70)

        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.min_bsp is not None and abs(m.min_bsp - 1.5) < 0.01

    def test_turn_angle_approx_90_for_quarter_turn(self) -> None:
        hdg = _const(30, 0.0) + _const(10, 45.0, 30) + _const(30, 90.0, 40)
        bsp = _const(70, 5.0)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.turn_angle_deg is not None and 80.0 <= abs(m.turn_angle_deg) <= 100.0

    def test_turn_rate_is_angle_over_duration(self) -> None:
        hdg = _const(30, 0.0) + _const(10, 45.0, 30) + _const(30, 90.0, 40)
        bsp = _const(70, 5.0)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.duration_sec is not None and m.duration_sec == 10.0
        assert m.turn_rate_deg_s is not None and 7.0 <= m.turn_rate_deg_s <= 11.0

    def test_distance_loss_zero_for_straight_line(self) -> None:
        # No actual turn — boat keeps going straight at entry VMG, so
        # ladder loss (upwind-axis projection vs ideal) is ~0.
        hdg = _const(70, 0.0)
        bsp = _const(70, 6.0)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        # TWD 40° (wind from NE) matches hdg=0 on starboard tack.
        twd = _const(70, 40.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 6.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
            twd=twd,
        )
        assert m.distance_loss_m is not None
        assert abs(m.distance_loss_m) < 1.0

    def test_distance_loss_positive_for_real_tack(self) -> None:
        # Pre-window: heading 0°, wind from 40°, boat closing at 6 kt
        # upwind.
        # During maneuver: 10 s of near-zero position change (boat stalls
        # mid-tack). Ideal upwind progress for 10 s at entry VMG is
        # ~23.6 m; actual is ~0. Loss should show that full deficit.
        # Exit: heading 270°, boat moving west — cross-wind, zero upwind
        # progress.
        pre = _straight_positions(37.0, -122.0, 0.0, 6.0, 30)
        last_pre_ts, last_lat, last_lon = pre[-1]
        during = [(last_pre_ts + timedelta(seconds=i + 1), last_lat, last_lon) for i in range(10)]
        last_during = during[-1]
        post = _straight_positions(last_during[1], last_during[2], 270.0, 5.0, 30, offset_s=40)
        positions = pre + during + post

        hdg = _const(30, 0.0) + _const(10, 315.0, 30) + _const(30, 270.0, 40)
        bsp = _const(30, 6.0) + _const(10, 1.0, 30) + _const(30, 5.0, 40)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        twd = _const(70, 40.0)

        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
            twd=twd,
        )
        assert m.distance_loss_m is not None
        # Stall gives up ~23 m of upwind progress vs ideal VMG.
        assert m.distance_loss_m > 10.0

    def test_missing_data_returns_none_fields_no_crash(self) -> None:
        hdg = _const(70, 10.0)
        bsp = _const(70, 6.0)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=[],
            tws=[],
            positions=[],
        )
        assert m.entry_twa is None
        assert m.entry_tws is None
        assert m.distance_loss_m is None

    def test_exit_ts_none_uses_fallback_window(self) -> None:
        hdg = _const(30, 0.0) + _const(10, 45.0, 30) + _const(30, 90.0, 40)
        bsp = _const(70, 5.0)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=None,
            hdg=hdg,
            bsp=bsp,
            twa=[],
            tws=[],
            positions=[],
        )
        # Should still compute entry_bsp from pre-window
        assert m.entry_bsp is not None and abs(m.entry_bsp - 5.0) < 0.01


class TestHeadToWindTimestamp:
    """#613: enrich_maneuver populates head_to_wind_ts from signed TWA series."""

    def _basic_inputs(self) -> dict:
        """Common defaults — straight-line positions, constant BSP/TWS."""
        return {
            "maneuver_ts": _BASE_TS + timedelta(seconds=30),
            "exit_ts": _BASE_TS + timedelta(seconds=40),
            "hdg": _const(70, 0.0),
            "bsp": _const(70, 5.0),
            "twa": _const(70, 40.0),
            "tws": _const(70, 12.0),
            "positions": _straight_positions(37.0, -122.0, 0.0, 5.0, 70),
        }

    def test_tack_clean_zero_crossing(self) -> None:
        # Signed TWA: +40° pre, linear down to -40° across 10s (zero at t=35).
        vals: list[float] = []
        for i in range(70):
            if i < 30:
                vals.append(40.0)
            elif i < 40:
                vals.append(40.0 - 8.0 * (i - 30))
            else:
                vals.append(-40.0)
        signed_twa = _series(vals)
        inputs = self._basic_inputs()
        m = enrich_maneuver(
            **inputs,
            signed_twa=signed_twa,
            maneuver_type="tack",
        )
        assert m.head_to_wind_ts is not None
        # Crossing lands at t=35; allow ±1s tolerance for nearest-sample picking.
        delta = (m.head_to_wind_ts - (_BASE_TS + timedelta(seconds=35))).total_seconds()
        assert abs(delta) <= 1.0

    def test_tack_stall_no_crossing_picks_min_abs(self) -> None:
        # Helm stalls: signed TWA starts +40, dips to +5 at t=35, recovers to +40.
        # No true zero-crossing — should fall back to the min-abs sample.
        vals = []
        for i in range(70):
            if i < 30:
                vals.append(40.0)
            elif i <= 35:
                vals.append(40.0 - 7.0 * (i - 30))  # 40 → 5
            elif i <= 40:
                vals.append(5.0 + 7.0 * (i - 35))  # 5 → 40
            else:
                vals.append(40.0)
        signed_twa = _series(vals)
        inputs = self._basic_inputs()
        m = enrich_maneuver(
            **inputs,
            signed_twa=signed_twa,
            maneuver_type="tack",
        )
        assert m.head_to_wind_ts is not None
        delta = (m.head_to_wind_ts - (_BASE_TS + timedelta(seconds=35))).total_seconds()
        assert abs(delta) <= 1.0

    def test_gybe_180_wrap_crossing(self) -> None:
        # Signed TWA wraps through ±180 during a gybe: +140 → +179 → -179 → -140.
        vals = []
        for i in range(70):
            if i < 30:
                vals.append(140.0)
            elif i < 35:
                vals.append(140.0 + 8.0 * (i - 30))  # 140 → 180
            elif i < 40:
                # Wrap: 180 at i=35 flips to -180 continuing toward -140.
                vals.append(-180.0 + 8.0 * (i - 35))  # -180 → -140
            else:
                vals.append(-140.0)
        signed_twa = _series(vals)
        inputs = self._basic_inputs()
        m = enrich_maneuver(
            **inputs,
            signed_twa=signed_twa,
            maneuver_type="gybe",
        )
        assert m.head_to_wind_ts is not None
        delta = (m.head_to_wind_ts - (_BASE_TS + timedelta(seconds=35))).total_seconds()
        assert abs(delta) <= 1.0

    def test_rounding_returns_none(self) -> None:
        # Even with a clean zero-crossing, rounding/maneuver types stay NULL.
        vals = [40.0 - i for i in range(70)]  # crosses zero at i=40
        signed_twa = _series(vals)
        inputs = self._basic_inputs()
        m = enrich_maneuver(
            **inputs,
            signed_twa=signed_twa,
            maneuver_type="rounding",
        )
        assert m.head_to_wind_ts is None

    def test_missing_signed_twa_returns_none(self) -> None:
        inputs = self._basic_inputs()
        m = enrich_maneuver(
            **inputs,
            signed_twa=[],
            maneuver_type="tack",
        )
        assert m.head_to_wind_ts is None

    def test_too_few_samples_returns_none(self) -> None:
        # Only 2 samples in the maneuver window.
        signed_twa = [
            (_BASE_TS + timedelta(seconds=30), 10.0),
            (_BASE_TS + timedelta(seconds=40), -10.0),
        ]
        inputs = self._basic_inputs()
        m = enrich_maneuver(
            **inputs,
            signed_twa=signed_twa,
            maneuver_type="tack",
        )
        assert m.head_to_wind_ts is None

    def test_head_to_wind_ts_serializes_in_to_dict(self) -> None:
        vals: list[float] = []
        for i in range(70):
            if i < 30:
                vals.append(40.0)
            elif i < 40:
                vals.append(40.0 - 8.0 * (i - 30))
            else:
                vals.append(-40.0)
        signed_twa = _series(vals)
        inputs = self._basic_inputs()
        m = enrich_maneuver(
            **inputs,
            signed_twa=signed_twa,
            maneuver_type="tack",
        )
        d = m.to_dict()
        assert isinstance(d["head_to_wind_ts"], str)
        assert "T" in d["head_to_wind_ts"]


class TestExtractLocalTrack:
    def test_entry_aligned_track_has_forward_axis_along_entry_bearing(self) -> None:
        # Straight line heading 090° (due east) at 5 kt for 60s.
        positions = _straight_positions(37.0, -122.0, 90.0, 5.0, 60)
        bsp = _const(60, 5.0)
        track = extract_local_track(
            maneuver_ts=_BASE_TS + timedelta(seconds=20),
            exit_ts=_BASE_TS + timedelta(seconds=30),
            entry_bearing_deg=90.0,
            positions=positions,
            bsp=bsp,
        )
        assert len(track) > 0
        # All points should have forward (y) progress and ~0 cross (x).
        for p in track:
            assert abs(p["x"]) < 1.0  # cross-track < 1 m
        ys = [p["y"] for p in track]
        assert max(ys) > 0  # forward progress exists
        assert min(ys) < 0  # pre-window points have negative forward

    def test_origin_at_maneuver_start(self) -> None:
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 60)
        bsp = _const(60, 5.0)
        track = extract_local_track(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=35),
            entry_bearing_deg=0.0,
            positions=positions,
            bsp=bsp,
        )
        zero = [p for p in track if p["t"] == 0.0]
        assert len(zero) == 1
        assert abs(zero[0]["x"]) < 0.01 and abs(zero[0]["y"]) < 0.01

    def test_empty_positions_returns_empty(self) -> None:
        track = extract_local_track(
            maneuver_ts=_BASE_TS,
            exit_ts=None,
            entry_bearing_deg=0.0,
            positions=[],
            bsp=[],
        )
        assert track == []

    def test_bsp_attached_when_available(self) -> None:
        positions = _straight_positions(37.0, -122.0, 0.0, 5.0, 60)
        bsp = _const(60, 4.2)
        track = extract_local_track(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=35),
            entry_bearing_deg=0.0,
            positions=positions,
            bsp=bsp,
        )
        assert any("bsp" in p and abs(p["bsp"] - 4.2) < 0.01 for p in track)


class TestEnrichSessionManeuversWindRefZero:
    """Regression for a falsy-zero bug where ``reference=0`` wind rows were
    skipped by ``int(r.get("reference", -1) or -1)`` because ``0 or -1 == -1``,
    leaving ``entry_tws`` / ``entry_twa`` blank on every maneuver in a session
    whose B&G feed publishes boat-referenced true wind (the common case).
    """

    @pytest.mark.asyncio
    async def test_reference_zero_wind_rows_populate_entry_tws(self, storage: Storage) -> None:
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)

        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "test-session",
                "test-event",
                1,
                start.date().isoformat(),
                "race",
                start.isoformat(),
                end.isoformat(),
            ),
        )

        # Seed 120s of 1Hz instrument data across a 60-second tack.
        for i in range(121):
            ts = (start + timedelta(seconds=i)).isoformat()
            hdg = 10.0 if i < 60 else 280.0
            bsp = 6.0 if i < 55 or i > 70 else 3.0
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, bsp),
            )
            # Crucially: reference=0, the falsy value that used to be dropped.
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 12.5, 40.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )

        # Seed one stored maneuver at t=60.
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "tack",
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
                10.0,
                3.0,
                None,
                12,
                40,
                None,
            ),
        )
        await db.commit()

        enriched, _video = await enrich_session_maneuvers(storage, 1)

        assert len(enriched) == 1
        m = enriched[0]
        assert m["entry_tws"] is not None
        assert abs(m["entry_tws"] - 12.5) < 0.1
        assert m["entry_twa"] is not None
        assert abs(m["entry_twa"] - 40.0) < 1.0

    @pytest.mark.asyncio
    async def test_enrich_reclassifies_large_turn_gybe_as_rounding(self, storage: Storage) -> None:
        """A 'gybe' detected with a 176° turn is really a leeward (Mexican)
        rounding — the boat stays downwind on both sides but the leg
        direction has changed. Enrichment should upgrade the type and
        record the original classification in details."""
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (2, 'rounding-test', 'e', 1, ?, 'race', ?, ?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat()),
        )
        # 176° heading swing across the maneuver: 5° → 181°.
        for i in range(121):
            ts = (start + timedelta(seconds=i)).isoformat()
            hdg = 5.0 if i < 60 else 181.0
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, 5.0),
            )
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 10.0, 130.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )
        # Stored maneuver was classified by the detector as a 'gybe' because
        # pre/post TWA both > 90° (downwind both sides).
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (2, 'gybe', ?, ?, 10.0, 3.0, NULL, 10, 130, NULL)",
            (
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
            ),
        )
        await db.commit()

        enriched, _ = await enrich_session_maneuvers(storage, 2)
        assert len(enriched) == 1
        m = enriched[0]
        assert m["type"] == "rounding"
        assert isinstance(m.get("details"), dict)
        assert m["details"].get("original_type") == "gybe"

    @pytest.mark.asyncio
    async def test_enrich_does_not_upgrade_pre_start_gybe_to_rounding(
        self, storage: Storage
    ) -> None:
        """Pre-start warmup gybes (practice maneuvers, zig-zags, drills)
        can easily swing 130°+ without rounding anything. The rounding
        reclassification must only apply to events at or after the race
        start so pre-start debrief isn't flooded with false 'roundings'."""
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (5, 'pre-start-test', 'e', 1, ?, 'race', ?, ?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat()),
        )
        # Span of data covering both pre-start and post-start so enrichment
        # has enough samples around the maneuver ts. Heading swings 176°
        # from 5° to 181°, downwind both sides (TWA > 90).
        pre_start_ts = start - timedelta(seconds=30)
        for i in range(121):
            ts_dt = pre_start_ts + timedelta(seconds=i)
            hdg = 5.0 if i < 30 else 181.0  # 176° swing
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts_dt.isoformat(), 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts_dt.isoformat(), 0x05, 5.0),
            )
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts_dt.isoformat(), 0x05, 10.0, 130.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts_dt.isoformat(), 0x05, 37.0 + i * 1e-5, -122.0),
            )
        # Stored maneuver lands 15s BEFORE the race start → pre-start event.
        maneuver_ts = start - timedelta(seconds=15)
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (5, 'gybe', ?, ?, 10.0, 3.0, NULL, 10, 130, NULL)",
            (maneuver_ts.isoformat(), (maneuver_ts + timedelta(seconds=10)).isoformat()),
        )
        await db.commit()

        enriched, _ = await enrich_session_maneuvers(storage, 5)
        assert len(enriched) == 1
        # Still a gybe — the large-turn reclassification is gated on
        # ts >= race start so pre-start practice gybes don't become
        # false 'roundings'.
        assert enriched[0]["type"] == "gybe"

    @pytest.mark.asyncio
    async def test_enrich_does_not_upgrade_large_tack_to_rounding(self, storage: Storage) -> None:
        """A large-angle tack (~175°) stays a tack. Tacks legitimately swing
        through 80–100°, and on a start-line approach or sharp course change
        an isolated tack can get much larger without being a mark rounding.
        The user flagged a 175° tack that was previously mis-upgraded."""
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (4, 'big-tack-test', 'e', 1, ?, 'race', ?, ?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat()),
        )
        for i in range(121):
            ts = (start + timedelta(seconds=i)).isoformat()
            hdg = 5.0 if i < 60 else 180.0  # 175° swing
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, 5.5),
            )
            # Upwind both sides — this is what the user ran into on the
            # start-line tack flagged in race 22.
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 10.0, 12.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (4, 'tack', ?, ?, 10.0, 3.0, NULL, 10, 12, NULL)",
            (
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
            ),
        )
        await db.commit()

        enriched, _ = await enrich_session_maneuvers(storage, 4)
        assert len(enriched) == 1
        assert enriched[0]["type"] == "tack"

    @pytest.mark.asyncio
    async def test_enrich_keeps_normal_tack_classification(self, storage: Storage) -> None:
        """A clean ~90° tack must stay a tack — the rounding-reclassification
        threshold of 130° must not catch normal maneuvers."""
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (3, 'tack-test', 'e', 1, ?, 'race', ?, ?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat()),
        )
        # 90° heading swing: 45° → 315° (close-hauled to close-hauled).
        for i in range(121):
            ts = (start + timedelta(seconds=i)).isoformat()
            hdg = 45.0 if i < 60 else 315.0
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, 6.0),
            )
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 12.0, 45.0),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (3, 'tack', ?, ?, 10.0, 3.0, NULL, 12, 45, NULL)",
            (
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
            ),
        )
        await db.commit()

        enriched, _ = await enrich_session_maneuvers(storage, 3)
        assert len(enriched) == 1
        assert enriched[0]["type"] == "tack"


class TestMedianEntryBaseline:
    """#615: entry-window aggregates use median instead of mean.

    Median is robust to a low tail caused by a helm bleeding speed in
    the seconds before a ready-about. Mean would pull ``entry_bsp``
    down toward the bleed and under-report ``distance_loss_m``.
    """

    def test_entry_bsp_uses_median_under_pre_tack_speed_bleed(self) -> None:
        # Entry window is 15s ending 3s before maneuver_ts (line 177-178):
        # for maneuver_ts = _BASE_TS+30s, the window is [12s, 27s).
        # Seed the boat at 6.0 kt for 10s, then bleed to 4.0 kt for 5s
        # right before the helm calls "ready about".
        bsp_entry = [6.0] * 10 + [4.0] * 5
        # Pad with same baseline before/after the entry window.
        hdg = _const(70, 0.0)
        bsp = _const(12, 6.0) + _series(bsp_entry, 12) + _const(43, 5.0, 27)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 6.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        # Median of [6,6,6,6,6,6,6,6,6,6,4,4,4,4,4] = 6.0.
        # Old mean would have given (10*6 + 5*4) / 15 = 5.33.
        assert m.entry_bsp is not None
        assert abs(m.entry_bsp - 6.0) < 0.01

    def test_entry_twa_uses_median(self) -> None:
        # Entry TWA bleeds from 40° to 35° in the last 5s of the window.
        twa_entry = [40.0] * 10 + [35.0] * 5
        hdg = _const(70, 0.0)
        bsp = _const(70, 6.0)
        twa = _const(12, 40.0) + _series(twa_entry, 12) + _const(43, 40.0, 27)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 6.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.entry_twa is not None
        assert abs(m.entry_twa - 40.0) < 0.1

    def test_entry_tws_uses_median(self) -> None:
        # A wind sensor glitch in the last 3s of the entry window dumps
        # TWS samples to 6 kt. Mean would pull entry_tws down. Median
        # ignores the spike.
        tws_entry = [12.0] * 12 + [6.0] * 3
        hdg = _const(70, 0.0)
        bsp = _const(70, 6.0)
        twa = _const(70, 40.0)
        tws = _const(12, 12.0) + _series(tws_entry, 12) + _const(43, 12.0, 27)
        positions = _straight_positions(37.0, -122.0, 0.0, 6.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        assert m.entry_tws is not None
        assert abs(m.entry_tws - 12.0) < 0.01

    def test_exit_window_still_mean(self) -> None:
        # Exit BSP is the recovery dynamic — keep mean. Test that a
        # bleeding-then-recovering exit window still gets a mean-shaped
        # value (not the median that would smooth out the climb).
        # Exit window for exit_ts=40s is [43s, 58s) (43 = 40 + _SKIP_S).
        bsp_exit = [4.0] * 5 + [5.0] * 5 + [6.0] * 5  # mean=5.0, median=5.0
        # Use a lopsided distribution where mean ≠ median to make the
        # distinction visible: [4]*10 + [6]*5 → mean=4.67, median=4.0.
        bsp_exit = [4.0] * 10 + [6.0] * 5
        hdg = _const(70, 0.0)
        bsp = _const(43, 6.0) + _series(bsp_exit, 43) + _const(12, 6.0, 58)
        twa = _const(70, 40.0)
        tws = _const(70, 12.0)
        positions = _straight_positions(37.0, -122.0, 0.0, 6.0, 70)
        m = enrich_maneuver(
            maneuver_ts=_BASE_TS + timedelta(seconds=30),
            exit_ts=_BASE_TS + timedelta(seconds=40),
            hdg=hdg,
            bsp=bsp,
            twa=twa,
            tws=tws,
            positions=positions,
        )
        # Mean of [4]*10 + [6]*5 = 4.667, NOT the median 4.0.
        assert m.exit_bsp is not None
        assert abs(m.exit_bsp - 4.667) < 0.01


class TestPhaseSplitMetrics:
    """#614: duration splits into time_to_head_to_wind_s + time_to_recover_s."""

    def _inputs_with_htw_at_offset(self, htw_offset_s: float) -> dict:
        """Build enrich_maneuver inputs whose signed TWA crosses zero at a
        specified offset past maneuver_ts. The HTW sample lands at
        _BASE_TS + 30s + htw_offset_s."""
        maneuver_ts = _BASE_TS + timedelta(seconds=30)
        exit_ts = _BASE_TS + timedelta(seconds=60)
        # Signed TWA: +20° pre, linear to -20° over the maneuver window,
        # crossing zero at exactly the requested offset.
        vals: list[float] = []
        for i in range(90):
            rel = i - 30  # 0 at maneuver_ts
            if rel < 0:
                vals.append(20.0)
            elif rel <= 30:
                # Slope crafted so that value == 0 when rel == htw_offset_s.
                vals.append(20.0 - 20.0 * (rel / htw_offset_s))
            else:
                vals.append(-20.0)
        signed_twa = _series(vals)
        return {
            "maneuver_ts": maneuver_ts,
            "exit_ts": exit_ts,
            "hdg": _const(90, 0.0),
            "bsp": _const(90, 5.0),
            "twa": _const(90, 20.0),
            "tws": _const(90, 12.0),
            "positions": _straight_positions(37.0, -122.0, 0.0, 5.0, 90),
            "signed_twa": signed_twa,
            "maneuver_type": "tack",
        }

    def test_slow_turn_fast_recovery(self) -> None:
        # HTW at +25s of a 30s duration → turn=25s, recovery=5s.
        m = enrich_maneuver(**self._inputs_with_htw_at_offset(25.0))
        assert m.time_to_head_to_wind_s is not None
        assert m.time_to_recover_s is not None
        assert abs(m.time_to_head_to_wind_s - 25.0) <= 1.0
        assert abs(m.time_to_recover_s - 5.0) <= 1.0
        # Invariant: duration = turn + recovery within rounding.
        assert m.duration_sec is not None
        split_total = m.time_to_head_to_wind_s + m.time_to_recover_s
        assert abs(split_total - m.duration_sec) <= 0.5

    def test_fast_turn_slow_recovery(self) -> None:
        # HTW at +5s of a 30s duration → turn=5s, recovery=25s.
        m = enrich_maneuver(**self._inputs_with_htw_at_offset(5.0))
        assert m.time_to_head_to_wind_s is not None
        assert m.time_to_recover_s is not None
        assert abs(m.time_to_head_to_wind_s - 5.0) <= 1.0
        assert abs(m.time_to_recover_s - 25.0) <= 1.0

    def test_null_htw_leaves_both_null(self) -> None:
        # Rounding types get head_to_wind_ts = None; the phase-split fields
        # must follow suit — never raise, never synthesize a value.
        inputs = self._inputs_with_htw_at_offset(15.0)
        inputs["maneuver_type"] = "rounding"
        m = enrich_maneuver(**inputs)
        assert m.head_to_wind_ts is None
        assert m.time_to_head_to_wind_s is None
        assert m.time_to_recover_s is None

    def test_missing_exit_ts_leaves_recovery_null(self) -> None:
        # With no exit_ts the recovery phase is undefined, but we can still
        # compute the turn phase from maneuver_ts → HTW.
        inputs = self._inputs_with_htw_at_offset(10.0)
        inputs["exit_ts"] = None
        m = enrich_maneuver(**inputs)
        assert m.time_to_head_to_wind_s is not None
        assert abs(m.time_to_head_to_wind_s - 10.0) <= 1.0
        assert m.time_to_recover_s is None

    def test_fields_in_to_dict(self) -> None:
        m = enrich_maneuver(**self._inputs_with_htw_at_offset(15.0))
        d = m.to_dict()
        assert "time_to_head_to_wind_s" in d
        assert "time_to_recover_s" in d
        assert isinstance(d["time_to_head_to_wind_s"], float)
        assert isinstance(d["time_to_recover_s"], float)


class TestHeadToWindPersistence:
    """#613: head_to_wind_ts is persisted to the maneuvers table and cached payload."""

    @pytest.mark.asyncio
    async def test_enrich_writes_head_to_wind_to_table_and_payload(self, storage: Storage) -> None:
        # Pin smoothing τs near-zero so the zero-crossing detection is
        # tested in isolation from EMA lag (#749).
        for ch in ("twa_deg", "twd_deg", "heading_deg", "bsp_kts", "tws_kts"):
            await storage.set_setting(f"smoothing.{ch}.tau_s", "0.05")
        await storage.refresh_smoothing()

        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (10, 'htw-test', 'e', 1, ?, 'race', ?, ?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat()),
        )
        # 121 samples of signed TWA that cleanly crosses zero at offset 65s.
        # Pre-maneuver: +30°; tack spans 60→70s, with a continuous linear
        # crossing through zero at t=65s; post: -30°.
        for i in range(121):
            ts_dt = start + timedelta(seconds=i)
            ts = ts_dt.isoformat()
            hdg = 10.0 if i < 60 else 280.0
            await db.execute(
                "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                (ts, 0x05, hdg),
            )
            await db.execute(
                "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                (ts, 0x05, 6.0),
            )
            if i < 60:
                wind_angle = 30.0
            elif i <= 70:
                wind_angle = 30.0 - 6.0 * (i - 60)  # 30 → -30, zero at i=65
            else:
                wind_angle = -30.0
            await db.execute(
                "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                " VALUES (?, ?, ?, ?, 0)",
                (ts, 0x05, 12.0, wind_angle),
            )
            await db.execute(
                "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (ts, 0x05, 37.0 + i * 1e-5, -122.0),
            )
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (10, 'tack', ?, ?, 10.0, 3.0, NULL, 12, 30, NULL)",
            (
                (start + timedelta(seconds=60)).isoformat(),
                (start + timedelta(seconds=70)).isoformat(),
            ),
        )
        await db.commit()

        enriched, _ = await enrich_session_maneuvers(storage, 10)
        assert len(enriched) == 1
        m = enriched[0]
        assert m.get("head_to_wind_ts") is not None
        # Zero-crossing sample is at offset 65s.
        htw = datetime.fromisoformat(m["head_to_wind_ts"])
        assert abs((htw - (start + timedelta(seconds=65))).total_seconds()) <= 1.0

        # Column was persisted on the maneuvers table.
        cur = await db.execute("SELECT head_to_wind_ts FROM maneuvers WHERE session_id = 10")
        row = await cur.fetchone()
        assert row is not None and row["head_to_wind_ts"] is not None


class TestBackfillStaleManeuverCache:
    """#613: backfill worker re-enriches sessions whose cache code_version is stale."""

    @pytest.mark.asyncio
    async def test_backfill_rebuilds_stale_cache_entries(self, storage: Storage) -> None:
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)

        # Seed two sessions with minimal data.
        for rid in (20, 21):
            offset = 0 if rid == 20 else 3600
            s = start + timedelta(seconds=offset)
            e = s + timedelta(seconds=120)
            await db.execute(
                "INSERT INTO races"
                " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
                " VALUES (?, ?, 'e', ?, ?, 'race', ?, ?)",
                (rid, f"s-{rid}", rid, s.date().isoformat(), s.isoformat(), e.isoformat()),
            )
            for i in range(121):
                ts = (s + timedelta(seconds=i)).isoformat()
                hdg = 10.0 if i < 60 else 280.0
                await db.execute(
                    "INSERT INTO headings (ts, source_addr, heading_deg) VALUES (?, ?, ?)",
                    (ts, 0x05, hdg),
                )
                await db.execute(
                    "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
                    (ts, 0x05, 6.0),
                )
                await db.execute(
                    "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
                    " VALUES (?, ?, ?, ?, 0)",
                    (ts, 0x05, 12.0, 30.0 if i < 65 else -30.0),
                )
                await db.execute(
                    "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
                    " VALUES (?, ?, ?, ?)",
                    (ts, 0x05, 37.0 + i * 1e-5, -122.0),
                )
            await db.execute(
                "INSERT INTO maneuvers"
                " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
                "  vmg_loss_kts, tws_bin, twa_bin, details)"
                " VALUES (?, 'tack', ?, ?, 10.0, 3.0, NULL, 12, 30, NULL)",
                (
                    rid,
                    (s + timedelta(seconds=60)).isoformat(),
                    (s + timedelta(seconds=70)).isoformat(),
                ),
            )
            # Pre-populate the cache at an older code_version so the worker
            # treats it as stale and rebuilds.
            await db.execute(
                "INSERT INTO maneuver_cache (session_id, payload, code_version, computed_at)"
                " VALUES (?, '{}', 1, ?)",
                (rid, datetime.now(UTC).isoformat()),
            )
        await db.commit()

        processed = await backfill_stale_maneuver_cache(storage)
        assert processed == 2

        # Both cache entries are now at the current code_version with real payloads.
        for rid in (20, 21):
            cur = await db.execute(
                "SELECT code_version, payload FROM maneuver_cache WHERE session_id = ?",
                (rid,),
            )
            row = await cur.fetchone()
            assert row is not None
            assert int(row["code_version"]) == ENRICH_CACHE_VERSION
            assert len(row["payload"]) > 2  # real JSON, not "{}"

    @pytest.mark.asyncio
    async def test_backfill_is_idempotent_when_caught_up(self, storage: Storage) -> None:
        db = storage._conn()
        start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        await db.execute(
            "INSERT INTO races"
            " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
            " VALUES (30, 'caught-up', 'e', 1, ?, 'race', ?, ?)",
            (
                start.date().isoformat(),
                start.isoformat(),
                (start + timedelta(seconds=120)).isoformat(),
            ),
        )
        await db.execute(
            "INSERT INTO maneuver_cache (session_id, payload, code_version, computed_at)"
            ' VALUES (30, \'{"maneuvers": [], "video_sync": null}\', ?, ?)',
            (ENRICH_CACHE_VERSION, datetime.now(UTC).isoformat()),
        )
        await db.commit()

        processed = await backfill_stale_maneuver_cache(storage)
        assert processed == 0


class TestSpreadAwareRanking:
    """#616: rank_maneuvers labels everything 'consistent' when the spread
    on distance_loss_m is too small to mean anything, and exposes a
    numeric loss_percentile alongside the bucket label."""

    def _mk(self, loss: float | None) -> dict:
        return {"type": "tack", "ts": _BASE_TS.isoformat(), "distance_loss_m": loss}

    def test_tight_session_all_labeled_consistent(self) -> None:
        # 10 maneuvers losing 7.0–7.9 m. stdev≈0.3, median≈7.45 →
        # stdev / median ≈ 0.04 (well under 0.5). max-min = 0.9 m
        # (under 3.0). Both spread tests fail → all 'consistent'.
        items = [self._mk(7.0 + i / 10.0) for i in range(10)]
        ranked = rank_maneuvers(items)
        assert all(m["rank"] == "consistent" for m in ranked)
        # Percentiles still spread 0–90 (10 items, stride 10).
        percentiles = sorted(m["loss_percentile"] for m in ranked)
        assert percentiles == list(range(0, 100, 10))

    def test_wide_session_uses_quartile_split(self) -> None:
        # Losses 2..20 by 2s. median=11, sample stdev≈6.05, ratio≈0.55
        # (passes 0.5 floor), spread=18 (passes 3 m floor). Quartile
        # split active. NB: the issue example [3..12] doesn't satisfy
        # its own relative threshold (ratio ≈ 0.40); using [2..20] keeps
        # the test stable while leaving the constants tunable in code.
        losses = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]
        items = [self._mk(float(v)) for v in losses]
        ranked = rank_maneuvers(items)
        labels = [m["rank"] for m in ranked]
        assert "good" in labels and "bad" in labels and "consistent" not in labels
        lo = next(m for m in ranked if m["distance_loss_m"] == 2.0)
        hi = next(m for m in ranked if m["distance_loss_m"] == 20.0)
        assert lo["rank"] == "good" and lo["loss_percentile"] == 0
        assert hi["rank"] == "bad" and hi["loss_percentile"] == 90

    def test_absolute_floor_wins_when_relative_spread_high(self) -> None:
        # 10 maneuvers losing 0.05, 0.10, 0.15, ..., 0.50 m. Big relative
        # stdev (median ≈ 0.275, stdev ≈ 0.15 → ratio ≈ 0.55, *passes*
        # the relative check), but max-min = 0.45 m → far under 3.0 m.
        # Absolute floor must veto and return 'consistent'.
        items = [self._mk(0.05 * (i + 1)) for i in range(10)]
        ranked = rank_maneuvers(items)
        assert all(m["rank"] == "consistent" for m in ranked)

    def test_relative_threshold_wins_when_absolute_spread_passes(self) -> None:
        # 10 maneuvers around a high baseline of 100m, total spread 30m
        # (max=115, min=85). max-min = 30 ≥ 3 (passes absolute floor),
        # but stdev ≈ 9–10 vs median ≈ 100 → ratio < 0.5. Relative
        # threshold vetoes → 'consistent'. Boundary case in the AC.
        losses = [85, 90, 95, 98, 100, 100, 102, 105, 110, 115]
        items = [self._mk(float(v)) for v in losses]
        ranked = rank_maneuvers(items)
        assert all(m["rank"] == "consistent" for m in ranked)

    def test_loss_percentile_present_even_when_consistent(self) -> None:
        items = [self._mk(7.0 + i / 10.0) for i in range(5)]
        ranked = rank_maneuvers(items)
        for m in ranked:
            assert isinstance(m["loss_percentile"], int)
            assert 0 <= m["loss_percentile"] <= 100

    def test_unrankable_items_stay_none(self) -> None:
        items = [
            {"type": "tack", "distance_loss_m": None, "loss_kts": None},
            {"type": "tack", "distance_loss_m": None, "loss_kts": None},
        ]
        ranked = rank_maneuvers(items)
        for m in ranked:
            assert m["rank"] is None
            assert m.get("loss_percentile") is None


class TestRankManeuvers:
    def _mk(self, distance_loss: float | None, bsp_loss: float | None) -> dict:
        return {
            "type": "tack",
            "ts": _BASE_TS.isoformat(),
            "distance_loss_m": distance_loss,
            "loss_kts": bsp_loss,
        }

    def test_quartile_labels_assigned(self) -> None:
        items = [self._mk(float(i), float(i) / 10) for i in range(8)]
        ranked = rank_maneuvers(items)
        # Lowest-loss quartile → "good", highest → "bad", middle → "avg"
        labels = [m["rank"] for m in ranked]
        assert "good" in labels and "bad" in labels
        # The highest distance_loss entry should be 'bad'
        bad = next(m for m in ranked if m["distance_loss_m"] == 7.0)
        assert bad["rank"] == "bad"
        good = next(m for m in ranked if m["distance_loss_m"] == 0.0)
        assert good["rank"] == "good"

    def test_empty_input_returns_empty(self) -> None:
        assert rank_maneuvers([]) == []

    def test_all_none_loss_falls_back_to_bsp_loss(self) -> None:
        items = [
            {"type": "tack", "distance_loss_m": None, "loss_kts": 0.1},
            {"type": "tack", "distance_loss_m": None, "loss_kts": 0.5},
            {"type": "tack", "distance_loss_m": None, "loss_kts": 1.0},
            {"type": "tack", "distance_loss_m": None, "loss_kts": 2.0},
        ]
        ranked = rank_maneuvers(items)
        worst = max(ranked, key=lambda m: m["loss_kts"] or 0)
        assert worst["rank"] == "bad"

    def test_explicit_metric_recovery_time(self) -> None:
        """Passing metric='time_to_recover_s' should rank by that field
        instead of the distance_loss_m fallback."""
        items = [
            {"type": "tack", "distance_loss_m": 100.0, "time_to_recover_s": 1.0},
            {"type": "tack", "distance_loss_m": 90.0, "time_to_recover_s": 5.0},
            {"type": "tack", "distance_loss_m": 80.0, "time_to_recover_s": 10.0},
            {"type": "tack", "distance_loss_m": 70.0, "time_to_recover_s": 30.0},
            {"type": "tack", "distance_loss_m": 60.0, "time_to_recover_s": 50.0},
            {"type": "tack", "distance_loss_m": 50.0, "time_to_recover_s": 80.0},
            {"type": "tack", "distance_loss_m": 40.0, "time_to_recover_s": 120.0},
            {"type": "tack", "distance_loss_m": 30.0, "time_to_recover_s": 200.0},
        ]
        rank_maneuvers(items, metric="time_to_recover_s")
        # Worst distance_loss has the FASTEST recovery → should be 'good'
        # under the recovery metric, even though it has the most distance loss.
        fastest_recovery = next(m for m in items if m["time_to_recover_s"] == 1.0)
        slowest_recovery = next(m for m in items if m["time_to_recover_s"] == 200.0)
        assert fastest_recovery["rank"] == "good"
        assert slowest_recovery["rank"] == "bad"

    def test_unknown_metric_falls_back_to_default(self) -> None:
        """An unknown metric name should not crash; items get rank=None."""
        items = [
            {"type": "tack", "distance_loss_m": 1.0, "loss_kts": 0.1},
            {"type": "tack", "distance_loss_m": 2.0, "loss_kts": 0.5},
        ]
        rank_maneuvers(items, metric="not_a_real_field")
        # No items have a non-None value for 'not_a_real_field' → all unranked.
        assert all(m["rank"] is None for m in items)

    def test_metric_skips_absolute_floor_for_non_distance(self) -> None:
        """The 3m absolute spread floor only gates distance_loss_m ranking.
        For other metrics, only the relative-stdev test applies."""
        # Tight cluster of recovery times — 0.5s spread — would fail the
        # absolute floor (3m) if it were applied. But it isn't, so the
        # relative test gets to decide.
        items = [
            {"type": "tack", "distance_loss_m": 50.0, "time_to_recover_s": 5.0},
            {"type": "tack", "distance_loss_m": 50.0, "time_to_recover_s": 5.1},
            {"type": "tack", "distance_loss_m": 50.0, "time_to_recover_s": 5.2},
            {"type": "tack", "distance_loss_m": 50.0, "time_to_recover_s": 5.3},
            {"type": "tack", "distance_loss_m": 50.0, "time_to_recover_s": 5.4},
            {"type": "tack", "distance_loss_m": 50.0, "time_to_recover_s": 5.5},
        ]
        rank_maneuvers(items, metric="time_to_recover_s")
        # stdev/median ≈ 0.17/5.25 ≈ 0.03, below the 0.5 threshold,
        # so the spread-aware path should label everything 'consistent'.
        labels = {m["rank"] for m in items}
        assert labels == {"consistent"}
