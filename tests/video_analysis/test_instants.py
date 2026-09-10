"""Tests for the sampling plan (scripts/analysis/video/instants.py)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from scripts.analysis.video import instants
from scripts.analysis.video.common import load_race
from tests.video_analysis.conftest import GUN, SESSION_END

if TYPE_CHECKING:
    import sqlite3

W1 = GUN + timedelta(minutes=8, seconds=36)  # 01:33:37
L1 = GUN + timedelta(minutes=19, seconds=41)  # 01:44:42


def test_mark_roundings_drop_prestart_and_early_turns() -> None:
    raw = [
        GUN - timedelta(minutes=3),
        GUN - timedelta(seconds=24),
        GUN + timedelta(seconds=14),  # tack onto the course, not a mark
        W1,
        W1 + timedelta(seconds=30),  # double detection of the same rounding
        L1,
    ]
    marks = instants.mark_roundings(GUN, raw)
    assert marks == [W1, L1]


def test_infer_leeward_mark_from_gybe_then_tack() -> None:
    # Race 255 shape: W1 detected, the leeward turn came out as gybe + tack.
    w1 = GUN + timedelta(minutes=7)
    gybes = (
        w1 + timedelta(minutes=1),
        w1 + timedelta(minutes=11),
        w1 + timedelta(minutes=11, seconds=20),
    )
    tacks = (GUN + timedelta(minutes=2), w1 + timedelta(minutes=13))
    assert instants.infer_leeward_mark([w1], gybes, tacks) == gybes[-1]
    # Even counts, no later tack, no gybes on the run, or too soon → nothing inferred.
    assert instants.infer_leeward_mark([w1, w1 + timedelta(minutes=12)], gybes, tacks) is None
    assert instants.infer_leeward_mark([w1], gybes, (GUN + timedelta(minutes=2),)) is None
    assert instants.infer_leeward_mark([w1], (), tacks) is None
    assert instants.infer_leeward_mark([w1], (w1 + timedelta(minutes=1),), tacks) is None


def test_mark_names_alternate_from_windward() -> None:
    assert instants.mark_names(4) == ["W1", "L1", "W2", "L2"]
    assert instants.mark_names(3) == ["W1", "L1", "W2"]


def test_plan_tier1_names_and_offsets() -> None:
    plan = instants.plan(GUN, [W1, L1], SESSION_END, tier=1)
    names = [p.name for p in plan]
    assert names[:7] == ["gun-300", "gun-240", "gun-180", "gun-120", "gun-60", "gun-30", "gun-15"]
    assert "gun" in names and "gun+120" in names and "gun+240" in names
    assert "W1-90" in names and "W1" in names and "W1+90" in names
    assert "L1-20" in names and "L1+45" in names
    assert names[-3:] == ["fin-120", "fin-60", "fin"]
    by_name = {p.name: p for p in plan}
    assert by_name["gun"].utc == GUN and by_name["gun"].kind == "start"
    assert by_name["gun-300"].utc == GUN - timedelta(seconds=300)
    assert by_name["W1+45"].utc == W1 + timedelta(seconds=45)
    assert by_name["W1"].kind == "rounding" and by_name["fin"].kind == "finish"
    assert by_name["fin"].utc == SESSION_END
    # Tier 1 has no per-minute leg frames or set/douse crops.
    assert not any(p.kind in ("leg", "setdouse") for p in plan)
    assert len(plan) == 7 + 6 + 2 * 7 + 3


def test_plan_full_adds_leg_and_setdouse_frames() -> None:
    plan = instants.plan(GUN, [W1, L1], SESSION_END, tier=3)
    kinds = {p.kind for p in plan}
    assert {"leg", "setdouse"} <= kinds
    legs = [p for p in plan if p.kind == "leg"]
    # Leg frames every 60 s from gun+300 to the finish, skipping instants near roundings.
    assert all((p.utc - GUN).total_seconds() % 60 == 0 for p in legs)
    assert all(abs((p.utc - W1).total_seconds()) > 90 for p in legs)
    sd = [p for p in plan if p.kind == "setdouse"]
    assert sd[0].name == "W1set-15" and sd[0].utc == W1 - timedelta(seconds=15)
    assert any(p.name == "L1douse-90" for p in sd)
    assert len(plan) > 7 + 6 + 2 * 7 + 3


def test_plan_names_are_unique() -> None:
    plan = instants.plan(GUN, [W1, L1], SESSION_END, tier=3)
    names = [p.name for p in plan]
    assert len(names) == len(set(names))


def test_compute_for_race_stores_instants(
    meta_db: sqlite3.Connection, ledger: sqlite3.Connection
) -> None:
    race = load_race(meta_db, 254)
    report = instants.compute_for_race(ledger, race, tier=1)
    assert report.gun == GUN and report.marks == [W1, L1] and report.warnings == []
    rows = ledger.execute(
        "SELECT name, kind, utc, video_t FROM instants WHERE race_id = 254 ORDER BY video_t"
    ).fetchall()
    assert rows[0]["name"] == "gun-300"
    gun_row = next(r for r in rows if r["name"] == "gun")
    assert gun_row["video_t"] == pytest.approx(474.0)  # stored sync, no horn correction yet
    # Re-running replaces rather than duplicates.
    instants.compute_for_race(ledger, race, tier=1)
    n = ledger.execute("SELECT count(*) FROM instants WHERE race_id = 254").fetchone()[0]
    assert n == len(rows)


def test_compute_for_race_uses_ledger_sync_and_flags_odd_roundings(
    meta_db: sqlite3.Connection, ledger: sqlite3.Connection
) -> None:
    meta_db.execute("DELETE FROM maneuvers WHERE ts LIKE '%01:44:42%'")  # lose L1
    race = load_race(meta_db, 254)
    ledger.execute(
        "INSERT INTO sync VALUES (254,'jygj-NbqFJE','horn+vakaros',?,468.0,?,468.0,6.0,3,'now')",
        (GUN.isoformat(), GUN.isoformat()),
    )
    report = instants.compute_for_race(ledger, race, tier=1)
    assert report.marks == [W1]  # the lone gybe at 01:36 is too soon after W1 to be a mark
    assert any("odd" in w for w in report.warnings)
    gun_row = ledger.execute(
        "SELECT video_t FROM instants WHERE race_id = 254 AND name = 'gun'"
    ).fetchone()
    assert gun_row["video_t"] == pytest.approx(468.0)


def test_compute_for_race_without_gun_fails(
    meta_db: sqlite3.Connection, ledger: sqlite3.Connection
) -> None:
    meta_db.execute("DELETE FROM vakaros_race_events")
    race = load_race(meta_db, 254)
    with pytest.raises(instants.NoGunError):
        instants.compute_for_race(ledger, race, tier=1)
