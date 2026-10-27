"""Simrad race timer business logic — pure functions, no I/O.

Consumes racing.startTimer.state / racing.startTimer.duration SK delta
events and computes the next SimradTimerState.  All calculations use the
NMEA UTC timestamp from the SK delta, not the Pi wall clock.

See docs/specs/simradtimerintegration.md.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta


@dataclass(frozen=True)
class SimradTimerState:
    """Singleton state for the Simrad-driven race timer."""

    duration_s: int | None = None
    t0_utc: datetime | None = None
    stopped_remaining_s: float | None = None
    is_running: bool = False
    instrument_timer_on: bool = False


def handle_duration(
    state: SimradTimerState,
    *,
    duration_s: int,
    nmea_ts: datetime,
) -> SimradTimerState:
    """Process racing.startTimer.duration — a SET command from the B&G.

    If the timer is currently running, resets t0 to nmea_ts + duration_s.
    Clears stopped_remaining_s in all cases (new duration supersedes any pause).
    """
    new_t0 = (nmea_ts + timedelta(seconds=duration_s)) if state.is_running else None
    return replace(
        state,
        duration_s=duration_s,
        t0_utc=new_t0,
        stopped_remaining_s=None,
        instrument_timer_on=True,
    )


def handle_running(
    state: SimradTimerState,
    *,
    nmea_ts: datetime,
) -> SimradTimerState:
    """Process racing.startTimer.state = "running".

    Fresh start: t0_utc = nmea_ts + duration_s.
    Resume after stop: t0_utc = nmea_ts + stopped_remaining_s.
    """
    if state.duration_s is None:
        raise ValueError(
            "cannot start timer: no duration set (racing.startTimer.duration not received)"
        )

    if state.stopped_remaining_s is not None:
        x = state.stopped_remaining_s
    else:
        x = float(state.duration_s)

    return replace(
        state,
        t0_utc=nmea_ts + timedelta(seconds=x),
        stopped_remaining_s=None,
        is_running=True,
        instrument_timer_on=True,
    )


def handle_stopped(
    state: SimradTimerState,
    *,
    nmea_ts: datetime,
) -> SimradTimerState:
    """Process racing.startTimer.state = "stopped".

    Computes stopped_remaining_s = t0_utc - nmea_ts, clamped to 0.
    """
    remaining = 0.0
    if state.t0_utc is not None:
        remaining = max(0.0, (state.t0_utc - nmea_ts).total_seconds())

    return replace(
        state,
        is_running=False,
        stopped_remaining_s=remaining,
        instrument_timer_on=True,
    )


def handle_reset(
    state: SimradTimerState,
    *,
    nmea_ts: datetime,
) -> SimradTimerState:
    """Process racing.startTimer.state = "reset".

    Resets timer back to full duration without changing running/stopped state.
    Running: new t0_utc = nmea_ts + duration_s.
    Stopped: stopped_remaining_s = duration_s.
    """
    duration = float(state.duration_s) if state.duration_s is not None else 0.0

    if state.is_running:
        return replace(
            state,
            t0_utc=nmea_ts + timedelta(seconds=duration),
            instrument_timer_on=True,
        )
    return replace(
        state,
        stopped_remaining_s=duration,
        instrument_timer_on=True,
    )


def handle_nearest_minute(
    state: SimradTimerState,
    *,
    nmea_ts: datetime,
) -> SimradTimerState:
    """Process racing.startTimer.state = "nearest-minute".

    Rounds timer to nearest whole minute without changing running/stopped state.
    Uses nmea_ts from the SK delta for the calculation.
    """
    if state.is_running and state.t0_utc is not None:
        remaining = (state.t0_utc - nmea_ts).total_seconds()
        rounded = round(remaining / 60.0) * 60.0
        return replace(
            state,
            t0_utc=nmea_ts + timedelta(seconds=rounded),
            instrument_timer_on=True,
        )

    if not state.is_running and state.stopped_remaining_s is not None:
        rounded = round(state.stopped_remaining_s / 60.0) * 60.0
        return replace(
            state,
            stopped_remaining_s=rounded,
            instrument_timer_on=True,
        )

    return replace(state, instrument_timer_on=True)
