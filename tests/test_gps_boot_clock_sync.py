"""Best-effort GPS system-clock discipline at boot (#799).

The orchestrator is hardware-free — the SK-offset reader, the OS-clock setter,
and the sync probe are all injected — so these tests drive every branch of the
decision with fakes and never touch the real clock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from helmlog.clock_sync import (
    STEP_THRESHOLD_S,
    gps_boot_clock_sync,
    should_step_clock,
)

_Fakes = tuple[
    Callable[[], Awaitable[float | None]],
    Callable[[], bool],
    Callable[[datetime], None],
    list[datetime],
]

NOW = datetime(2026, 6, 18, 1, 0, 0, tzinfo=UTC)
BOOT_SKEW_S = 2630.0


class TestShouldStepClock:
    def test_never_steps_when_already_synchronized(self) -> None:
        # An authoritative NTP/timesyncd clock wins — don't fight it.
        assert should_step_clock(offset_s=BOOT_SKEW_S, clock_synchronized=True) is False

    def test_steps_when_unsynced_and_offset_large(self) -> None:
        assert should_step_clock(offset_s=BOOT_SKEW_S, clock_synchronized=False) is True
        assert should_step_clock(offset_s=-BOOT_SKEW_S, clock_synchronized=False) is True

    def test_skips_sub_threshold_jitter(self) -> None:
        assert should_step_clock(offset_s=STEP_THRESHOLD_S - 0.1, clock_synchronized=False) is False


def _fakes(*, offset: float | None, synced: bool) -> _Fakes:
    calls: list[datetime] = []

    async def measure_offset() -> float | None:
        return offset

    def is_synchronized() -> bool:
        return synced

    def set_clock(target: datetime) -> None:
        calls.append(target)

    return measure_offset, is_synchronized, set_clock, calls


class TestGpsBootClockSync:
    async def test_steps_clock_when_unsynced_and_adrift(self) -> None:
        measure, synced, setter, calls = _fakes(offset=BOOT_SKEW_S, synced=False)
        result = await gps_boot_clock_sync(
            measure_offset=measure,
            is_synchronized=synced,
            set_clock=setter,
            now=lambda: NOW,
        )
        assert result.action == "stepped"
        assert result.offset_s == BOOT_SKEW_S
        assert calls == [NOW + timedelta(seconds=BOOT_SKEW_S)]

    async def test_no_step_when_no_gps_time(self) -> None:
        measure, synced, setter, calls = _fakes(offset=None, synced=False)
        result = await gps_boot_clock_sync(
            measure_offset=measure,
            is_synchronized=synced,
            set_clock=setter,
            now=lambda: NOW,
        )
        assert result.action == "no-gps"
        assert calls == []

    async def test_no_step_when_already_synchronized(self) -> None:
        measure, synced, setter, calls = _fakes(offset=BOOT_SKEW_S, synced=True)
        result = await gps_boot_clock_sync(
            measure_offset=measure,
            is_synchronized=synced,
            set_clock=setter,
            now=lambda: NOW,
        )
        assert result.action == "skipped-synchronized"
        assert calls == []

    async def test_no_step_when_offset_small(self) -> None:
        measure, synced, setter, calls = _fakes(offset=2.0, synced=False)
        result = await gps_boot_clock_sync(
            measure_offset=measure,
            is_synchronized=synced,
            set_clock=setter,
            now=lambda: NOW,
        )
        assert result.action == "skipped-small"
        assert calls == []
