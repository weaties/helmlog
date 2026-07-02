"""Tests for clock_sync.ClockDiscipliner — GPS clock discipline (#794).

Each test maps to a row of the spec decision table or a transition in the
clock-sync state diagram on issue #794.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from helmlog.clock_sync import (
    ClockDiscipliner,
    ClockFlag,
    estimate_track_offset_s,
)

BASE = datetime(2026, 6, 16, 1, 0, 0, tzinfo=UTC)


def _host(sec: float) -> datetime:
    return BASE + timedelta(seconds=sec)


# --- decision table -------------------------------------------------------


def test_no_gps_ever_is_unverified_and_passthrough() -> None:
    c = ClockDiscipliner()
    assert c.flag is ClockFlag.UNVERIFIED
    ts = _host(10)
    assert c.apply(ts) == ts  # host time used unchanged, never dropped


def test_small_skew_is_synced_and_passthrough() -> None:
    c = ClockDiscipliner()
    # GPS only 5s ahead of host — within SKEW_OK.
    for s in range(5):
        c.observe(_host(s), _host(s) + timedelta(seconds=5))
    assert c.flag is ClockFlag.SYNCED
    ts = _host(10)
    assert c.apply(ts) == ts


def test_large_skew_is_corrected_and_disciplined() -> None:
    c = ClockDiscipliner()
    # Host clock is 1h slow: GPS = host + 3600s (the race-201 incident).
    for s in range(5):
        c.observe(_host(s), _host(s) + timedelta(seconds=3600))
    assert c.flag is ClockFlag.CORRECTED
    # A record stamped at host time is bumped to true (GPS) time.
    assert c.apply(_host(10)) == _host(10) + timedelta(seconds=3600)


def test_ref_lost_after_timeout_keeps_last_offset() -> None:
    c = ClockDiscipliner()
    for s in range(5):
        c.observe(_host(s), _host(s) + timedelta(seconds=3600))
    assert c.flag is ClockFlag.CORRECTED
    # No GPS for > REF_TIMEOUT: still disciplined with the last-known offset.
    late = _host(5 + 200)
    assert c.apply(late) == late + timedelta(seconds=3600)
    assert c.flag is ClockFlag.REF_LOST


# --- state transitions ----------------------------------------------------


def test_single_outlier_does_not_move_synced_offset() -> None:
    c = ClockDiscipliner()
    for s in range(5):
        c.observe(_host(s), _host(s) + timedelta(seconds=2))
    assert c.flag is ClockFlag.SYNCED
    # One garbage GPS sample (parse glitch) 1h off — median ignores it.
    c.observe(_host(5), _host(5) + timedelta(seconds=3600))
    assert c.flag is ClockFlag.SYNCED
    assert abs(c.offset_s - 2.0) < 1.0


def test_sustained_step_is_adopted() -> None:
    c = ClockDiscipliner()
    for s in range(5):
        c.observe(_host(s), _host(s) + timedelta(seconds=2))
    assert c.flag is ClockFlag.SYNCED
    # A real, sustained clock step (e.g. NTP correction) of +3600s.
    for s in range(5, 12):
        c.observe(_host(s), _host(s) + timedelta(seconds=3600))
    assert c.flag is ClockFlag.CORRECTED


def test_ref_lost_recovers_on_resume() -> None:
    c = ClockDiscipliner()
    for s in range(5):
        c.observe(_host(s), _host(s) + timedelta(seconds=3600))
    # Force ref_lost via a stale apply.
    c.apply(_host(300))
    assert c.flag is ClockFlag.REF_LOST
    # GPS resumes, now in sync (host clock fixed): back to SYNCED.
    for s in range(300, 306):
        c.observe(_host(s), _host(s) + timedelta(seconds=1))
    assert c.flag is ClockFlag.SYNCED


# --- import-skew cross-check (track time-offset) --------------------------


def _line_track(start: datetime, n: int, step_s: float = 1.0) -> list:
    # Boat moving steadily NE so every second is a distinct position.
    return [
        (start + timedelta(seconds=i * step_s), 47.60 + i * 1e-4, -122.40 + i * 1e-4)
        for i in range(n)
    ]


def test_track_offset_recovers_known_one_hour_shift() -> None:
    live = _line_track(BASE, 200)
    # External (Vakaros) device clock is 1h ahead — the race-201 condition.
    external = [(ts + timedelta(seconds=3600), lat, lon) for (ts, lat, lon) in live[::5]]
    off = estimate_track_offset_s(live, external)
    assert off is not None
    assert abs(off - 3600) <= 10


def test_track_offset_zero_when_aligned() -> None:
    live = _line_track(BASE, 200)
    external = [(ts, lat, lon) for (ts, lat, lon) in live[::5]]
    off = estimate_track_offset_s(live, external)
    assert off is not None
    assert abs(off) <= 10


def test_track_offset_none_when_too_few_points() -> None:
    live = _line_track(BASE, 3)
    external = _line_track(BASE, 3)
    assert estimate_track_offset_s(live, external) is None


def test_track_offset_race_subset_of_full_day_reference() -> None:
    # GPS-correct Vakaros reference spanning the whole race day.
    full_day = _line_track(BASE, 3600)
    # One race is a 600s slice, but its recording clock is 1h SLOW: its
    # timestamps read 3600s earlier than the true (reference) time. Only the
    # race slice overlaps — the rest of the day must not poison the estimate.
    race = [(ts - timedelta(seconds=3600), lat, lon) for (ts, lat, lon) in full_day[1500:2100:5]]
    off = estimate_track_offset_s(race, full_day)
    assert off is not None
    # reference_clock - track_clock = +3600 (recording is 1h behind true time).
    assert abs(off - 3600) <= 10
