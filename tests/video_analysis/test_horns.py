"""Tests for the horn detector and gun selection (scripts/analysis/video/horns.py)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np
import pytest
from scripts.analysis.video import horns
from scripts.analysis.video.common import load_race
from tests.video_analysis.conftest import GUN, SESSION_START

if TYPE_CHECKING:
    import sqlite3

SR = 8000


def synthetic_audio(horn_times: list[float], total_s: float, seed: int = 1) -> np.ndarray:
    """White-ish noise with 1 s 600 Hz horn blasts at the given times."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, 0.02, int(total_s * SR)).astype(np.float32)
    t = np.arange(SR) / SR
    blast = (0.5 * np.sin(2 * np.pi * 600 * t) * np.hanning(SR)).astype(np.float32)
    for h in horn_times:
        i = int(h * SR)
        x[i : i + SR] += blast
    return x


def ev(t: float, z: float = 50.0) -> horns.AudioEvent:
    return horns.AudioEvent(t, 1.0, z)


def test_detects_each_blast_once() -> None:
    x = synthetic_audio([10.0, 70.0, 250.0, 310.0], 330.0)
    events = horns.detect_events(x, SR)
    assert [round(e.t) for e in events] == [10, 70, 250, 310]
    assert all(e.dur_s >= 0.5 for e in events)
    assert all(e.z > horns.MIN_HORN_Z for e in events)


def test_gun_from_5_4_1_0_pattern() -> None:
    # 5-minute, 4-minute, 1-minute, gun: T-300, T-240, T-60, T, plus a stray blast.
    events = [ev(t) for t in (100.0, 168.0, 228.0, 408.0, 468.0)]
    cands = horns.gun_candidates(events)
    assert cands[0].t == 468.0 and cands[0].hits == 3
    best = horns.best_gun(cands)
    assert best is not None and best.t == 468.0


def test_quiet_events_are_not_horns() -> None:
    # Race 255 shape: a quiet click happens to sit 300/240 s after two other quiet clicks.
    quiet = [ev(300.4, 13.6), ev(358.6, 71.0), ev(599.9, 11.7)]
    loud = [ev(520.2, 93.0), ev(579.9, 168.0), ev(581.6, 134.0), ev(586.5, 124.0)]
    cands = horns.gun_candidates(quiet + loud)
    assert all(c.z >= horns.MIN_HORN_Z for c in cands)
    assert horns.best_gun(cands) is None  # no complete pattern → nothing without Vakaros
    best = horns.best_gun(cands, expected_t=585.1)
    assert best is not None and best.t == 579.9  # loudest supported blast near the gun


def test_expected_time_picks_the_right_sequence() -> None:
    # Two races back to back: two full sequences; pick the one nearest the expected t.
    seq1 = [1000.0, 1060.0, 1240.0, 1300.0]
    seq2 = [3000.0, 3060.0, 3240.0, 3300.0]
    cands = horns.gun_candidates([ev(t) for t in seq1 + seq2])
    assert {c.t for c in cands if c.hits >= 2} == {1300.0, 3300.0}
    assert horns.best_gun(cands, expected_t=3250.0).t == 3300.0  # type: ignore[union-attr]
    assert horns.best_gun(cands, expected_t=1200.0).t == 1300.0  # type: ignore[union-attr]
    # Nothing within ±MAX_SYNC_DELTA_S of the expected time → no gun.
    assert horns.best_gun(cands, expected_t=2000.0) is None
    assert horns.best_gun([]) is None


def test_unsupported_blast_near_expected_is_accepted() -> None:
    # Race 253 shape: no warning horns heard, one loud blast 5 s before the Vakaros gun.
    cands = horns.gun_candidates(
        [ev(606.5, 54.0), ev(607.4, 60.0), ev(623.8, 54.0), ev(683.9, 34.0)]
    )
    best = horns.best_gun(cands, expected_t=628.7)
    assert best is not None and best.t == 623.8 and best.hits == 0


def test_reconcile_sync_with_vakaros(
    meta_db: sqlite3.Connection, ledger: sqlite3.Connection
) -> None:
    race = load_race(meta_db, 254)
    # Stored sync says t=0 at 01:17:07, so the Vakaros gun (01:25:01) maps to t=474.
    # The horn was heard at t=468 → the stored sync is 6 s slow.
    result = horns.reconcile_sync(ledger, race, horns.GunCandidate(468.0, 3, 120.0))
    assert result.method == "horn+vakaros"
    assert result.gun_utc == GUN
    assert result.delta_s == pytest.approx(6.0)
    assert result.hits == 3
    row = ledger.execute("SELECT * FROM sync WHERE race_id = 254").fetchone()
    assert row["gun_video_t"] == 468.0 and row["horn_hits"] == 3
    assert row["sync_offset_s"] == 468.0 and row["sync_utc"] == GUN.isoformat()


def test_reconcile_sync_horn_only(meta_db: sqlite3.Connection, ledger: sqlite3.Connection) -> None:
    meta_db.execute("DELETE FROM vakaros_race_events")
    race = load_race(meta_db, 254)
    assert race.vakaros_gun is None
    result = horns.reconcile_sync(ledger, race, horns.GunCandidate(468.0, 3, 120.0))
    assert result.method == "horn"
    assert result.gun_utc == SESSION_START + timedelta(seconds=468)
    assert result.delta_s == 0.0


def test_reconcile_sync_without_horn_keeps_stored(
    meta_db: sqlite3.Connection, ledger: sqlite3.Connection
) -> None:
    race = load_race(meta_db, 254)
    result = horns.reconcile_sync(ledger, race, None)
    assert result.method == "stored"
    assert result.gun_utc == GUN and result.delta_s == 0.0


def test_reconcile_rejects_implausible_delta(
    meta_db: sqlite3.Connection, ledger: sqlite3.Connection
) -> None:
    race = load_race(meta_db, 254)
    with pytest.raises(horns.SyncError):
        horns.reconcile_sync(ledger, race, horns.GunCandidate(1200.0, 3, 120.0))
