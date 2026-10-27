"""Tests for Simrad race timer business logic (simrad_timer.py).

TDD — these tests are written before the implementation.
Run with:  uv run pytest tests/test_simrad_timer_state.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from helmlog.simrad_timer import (
    SimradTimerState,
    handle_duration,
    handle_nearest_minute,
    handle_reset,
    handle_running,
    handle_stopped,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)


def _ts(offset_s: float = 0.0) -> datetime:
    return _BASE + timedelta(seconds=offset_s)


def _idle() -> SimradTimerState:
    return SimradTimerState()


# ---------------------------------------------------------------------------
# Duration (SET)
# ---------------------------------------------------------------------------


def test_handle_duration_stores_seconds() -> None:
    state = handle_duration(_idle(), duration_s=300, nmea_ts=_ts())
    assert state.duration_s == 300


def test_handle_duration_turns_instrument_timer_on() -> None:
    state = handle_duration(_idle(), duration_s=300, nmea_ts=_ts())
    assert state.instrument_timer_on is True


def test_handle_duration_while_running_resets_t0() -> None:
    running = SimradTimerState(
        duration_s=300,
        t0_utc=_ts(120),
        is_running=True,
        instrument_timer_on=True,
    )
    new_ts = _ts(200)
    state = handle_duration(running, duration_s=240, nmea_ts=new_ts)
    assert state.duration_s == 240
    assert state.t0_utc == new_ts + timedelta(seconds=240)
    assert state.is_running is True


def test_handle_duration_while_stopped_clears_stopped_remaining() -> None:
    stopped = SimradTimerState(
        duration_s=300,
        stopped_remaining_s=120.0,
        is_running=False,
        instrument_timer_on=True,
    )
    state = handle_duration(stopped, duration_s=300, nmea_ts=_ts())
    assert state.stopped_remaining_s is None


# ---------------------------------------------------------------------------
# Running (START)
# ---------------------------------------------------------------------------


def test_handle_running_fresh_start() -> None:
    state = SimradTimerState(duration_s=300, instrument_timer_on=True)
    result = handle_running(state, nmea_ts=_ts())
    assert result.t0_utc == _ts() + timedelta(seconds=300)
    assert result.is_running is True
    assert result.stopped_remaining_s is None


def test_handle_running_after_stop_uses_stopped_remaining() -> None:
    state = SimradTimerState(
        duration_s=300,
        stopped_remaining_s=120.0,
        is_running=False,
        instrument_timer_on=True,
    )
    result = handle_running(state, nmea_ts=_ts())
    assert result.t0_utc == _ts() + timedelta(seconds=120)
    assert result.is_running is True
    assert result.stopped_remaining_s is None


def test_handle_running_turns_instrument_timer_on() -> None:
    state = SimradTimerState(duration_s=300)
    result = handle_running(state, nmea_ts=_ts())
    assert result.instrument_timer_on is True


def test_handle_running_no_duration_raises() -> None:
    with pytest.raises(ValueError, match="duration"):
        handle_running(SimradTimerState(), nmea_ts=_ts())


# ---------------------------------------------------------------------------
# Stopped (STOP)
# ---------------------------------------------------------------------------


def test_handle_stopped_computes_remaining() -> None:
    t0 = _ts(300)  # gun is 5 min from base
    state = SimradTimerState(duration_s=300, t0_utc=t0, is_running=True, instrument_timer_on=True)
    stop_ts = _ts(120)  # stopped 2 min in, 3 min remaining
    result = handle_stopped(state, nmea_ts=stop_ts)
    assert result.is_running is False
    assert result.stopped_remaining_s == pytest.approx(180.0)


def test_handle_stopped_clamps_to_zero_if_past_gun() -> None:
    t0 = _ts(100)
    state = SimradTimerState(duration_s=300, t0_utc=t0, is_running=True, instrument_timer_on=True)
    result = handle_stopped(state, nmea_ts=_ts(150))  # 50s past gun
    assert result.stopped_remaining_s == 0.0


def test_handle_stopped_turns_instrument_timer_on() -> None:
    state = SimradTimerState(duration_s=300, t0_utc=_ts(300), is_running=True)
    result = handle_stopped(state, nmea_ts=_ts())
    assert result.instrument_timer_on is True


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_handle_reset_while_running_resets_t0_to_full_duration() -> None:
    t0 = _ts(150)
    state = SimradTimerState(duration_s=300, t0_utc=t0, is_running=True, instrument_timer_on=True)
    reset_ts = _ts(100)
    result = handle_reset(state, nmea_ts=reset_ts)
    assert result.t0_utc == reset_ts + timedelta(seconds=300)
    assert result.is_running is True


def test_handle_reset_while_stopped_resets_stopped_remaining_to_duration() -> None:
    state = SimradTimerState(
        duration_s=300,
        stopped_remaining_s=120.0,
        is_running=False,
        instrument_timer_on=True,
    )
    result = handle_reset(state, nmea_ts=_ts())
    assert result.stopped_remaining_s == 300.0
    assert result.is_running is False


def test_handle_reset_turns_instrument_timer_on() -> None:
    state = SimradTimerState(duration_s=300, stopped_remaining_s=120.0, is_running=False)
    result = handle_reset(state, nmea_ts=_ts())
    assert result.instrument_timer_on is True


# ---------------------------------------------------------------------------
# Nearest minute
# ---------------------------------------------------------------------------


def test_handle_nearest_minute_running_rounds_up() -> None:
    # remaining = 4:40 → rounds to 5:00
    state = SimradTimerState(
        duration_s=300, t0_utc=_ts(280), is_running=True, instrument_timer_on=True
    )
    result = handle_nearest_minute(state, nmea_ts=_ts())
    assert result.t0_utc == _ts() + timedelta(seconds=300)
    assert result.is_running is True


def test_handle_nearest_minute_running_rounds_down() -> None:
    # remaining = 4:20 → rounds to 4:00
    state = SimradTimerState(
        duration_s=300, t0_utc=_ts(260), is_running=True, instrument_timer_on=True
    )
    result = handle_nearest_minute(state, nmea_ts=_ts())
    assert result.t0_utc == _ts() + timedelta(seconds=240)
    assert result.is_running is True


def test_handle_nearest_minute_stopped_rounds() -> None:
    state = SimradTimerState(
        duration_s=300,
        stopped_remaining_s=280.0,  # 4:40 → rounds to 5:00
        is_running=False,
        instrument_timer_on=True,
    )
    result = handle_nearest_minute(state, nmea_ts=_ts())
    assert result.stopped_remaining_s == pytest.approx(300.0)
    assert result.is_running is False


def test_handle_nearest_minute_turns_instrument_timer_on() -> None:
    state = SimradTimerState(duration_s=300, stopped_remaining_s=280.0, is_running=False)
    result = handle_nearest_minute(state, nmea_ts=_ts())
    assert result.instrument_timer_on is True
