"""GPS clock discipline for the recording path (#794).

The Signal K server runs on the Pi (``SK_HOST=localhost``), so delta
timestamps carry the Pi's *system* clock. When the Pi boots before NTP/GPS
sync, that clock can be wrong and stamp an entire recording at the wrong
absolute time — the race-201 incident, where the whole session was 1h slow.

The GPS broadcasts authoritative UTC on the NMEA 2000 bus (PGN 126992 System
Time / 129033 Time & Date), surfaced by Signal K as ``navigation.datetime``.
This module measures the offset between the host clock and that GPS time and
disciplines record timestamps to it.

Pure and hardware-free: feed it ``(host_ts, gps_ts)`` samples plus the host
timestamp of each record, and it returns disciplined timestamps and a
per-session clock flag. The ``SKReader`` edge wires it to the live stream.

Design (per the approved spec):
- The offset is the **median** of the last ``window`` ``gps - host`` samples.
  A rolling median is the outlier guard the spec calls for: a single bad GPS
  sample cannot move the output, while a *sustained* step (a real NTP
  correction mid-session) is adopted after a few samples.
- Recording is **never blocked** on clock state — losing the race is worse
  than logging it with a flag. When GPS time is unavailable we fall back to
  host time and flag the session ``unverified``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Defaults (spec Q2). Max tolerated host<->GPS divergence, and how long
# without a GPS sample before the reference is considered lost.
SKEW_OK_S: float = 30.0
REF_TIMEOUT_S: float = 120.0
_WINDOW: int = 5


class ClockFlag(StrEnum):
    """Per-session time-base provenance, persisted on the race row."""

    SYNCED = "synced"  # host within SKEW_OK of GPS; timestamps trusted as-is
    CORRECTED = "corrected"  # host diverged; timestamps GPS-disciplined
    UNVERIFIED = "unverified"  # no GPS time ever seen; host time, best effort
    REF_LOST = "ref_lost"  # had GPS, now stale; coasting on last offset


@dataclass
class ClockDiscipliner:
    """Tracks the host<->GPS offset and disciplines record timestamps."""

    skew_ok_s: float = SKEW_OK_S
    ref_timeout_s: float = REF_TIMEOUT_S
    window: int = _WINDOW

    _samples: list[float] = field(default_factory=list)  # recent (gps - host) seconds
    _offset_s: float = 0.0  # smoothed offset, gps - host
    _have_ref: bool = False
    _last_gps_host: datetime | None = None  # host ts of the last GPS sample
    _flag: ClockFlag = ClockFlag.UNVERIFIED

    def observe(self, host_ts: datetime, gps_ts: datetime) -> None:
        """Record a GPS-time observation paired with the host time of the same delta."""
        self._samples.append((gps_ts - host_ts).total_seconds())
        if len(self._samples) > self.window:
            self._samples.pop(0)
        self._offset_s = statistics.median(self._samples)
        self._have_ref = True
        self._last_gps_host = host_ts
        self._recompute_flag(host_ts)

    def apply(self, host_ts: datetime) -> datetime:
        """Return the timestamp a record stamped at ``host_ts`` should carry."""
        self._recompute_flag(host_ts)  # record arrival may reveal a stale ref
        if self._flag in (ClockFlag.CORRECTED, ClockFlag.REF_LOST):
            return host_ts + timedelta(seconds=self._offset_s)
        return host_ts

    def _recompute_flag(self, now_host: datetime) -> None:
        if not self._have_ref:
            self._flag = ClockFlag.UNVERIFIED
            return
        if (
            self._last_gps_host is not None
            and (now_host - self._last_gps_host).total_seconds() > self.ref_timeout_s
        ):
            self._flag = ClockFlag.REF_LOST
            return
        self._flag = (
            ClockFlag.SYNCED if abs(self._offset_s) <= self.skew_ok_s else ClockFlag.CORRECTED
        )

    @property
    def flag(self) -> ClockFlag:
        return self._flag

    @property
    def offset_s(self) -> float:
        """Smoothed ``gps - host`` offset in seconds (0.0 until a GPS ref exists)."""
        return self._offset_s


# Threshold above which a recording's clock is considered skewed relative to a
# GPS-sourced external track (Vakaros). 60s comfortably clears GPS/host jitter
# while catching the whole-recording offsets this feature exists to find (#794).
IMPORT_SKEW_OK_S: float = 60.0

# A track point: (timestamp, latitude_deg, longitude_deg).
TrackPoint = tuple[datetime, float, float]


def _equirect_m2(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Squared planar distance (m^2) between two nearby lat/lon points.

    Equirectangular approximation — exact enough for the few-km scales and
    sub-degree separations of a single race course, and cheap (no trig per
    pair beyond one cosine)."""
    import math

    mean_lat = math.radians((lat1 + lat2) * 0.5)
    dx = math.radians(lon2 - lon1) * math.cos(mean_lat) * 6_371_000.0
    dy = math.radians(lat2 - lat1) * 6_371_000.0
    return dx * dx + dy * dy


def estimate_track_offset_s(
    track: list[TrackPoint],
    reference: list[TrackPoint],
    *,
    max_offset_s: float = 7200.0,
    step_s: float = 10.0,
) -> float | None:
    """Best-fit time offset aligning ``track`` onto ``reference`` GPS tracks.

    Both are the *same boat* logged by two devices; if their clocks agree, the
    same wall-clock instant is the same position. We scan candidate offsets and
    return the ``offset`` (seconds, ``reference_clock - track_clock``) that
    minimises the median squared distance between each ``track`` point at time
    ``t`` and the ``reference`` point nearest in time to ``t + offset``.

    Iterate the *contiguous* track and look up the *covering* one: at the
    Vakaros→live cross-check pass ``track=race`` (one race) and
    ``reference=vakaros`` (the whole race day), so non-overlapping reference
    points never poison the median. Returns ``None`` when either side is too
    sparse to decide. Used to catch a recording whose clock was wrong (#794).
    """
    if len(track) < 5 or len(reference) < 5:
        return None

    import bisect
    import statistics

    ref_sorted = sorted(reference, key=lambda p: p[0])
    ref_epoch = [p[0].timestamp() for p in ref_sorted]

    def nearest_ref(t: float) -> TrackPoint:
        i = bisect.bisect_left(ref_epoch, t)
        if i == 0:
            return ref_sorted[0]
        if i >= len(ref_sorted):
            return ref_sorted[-1]
        before, after = ref_sorted[i - 1], ref_sorted[i]
        return before if (t - ref_epoch[i - 1]) <= (ref_epoch[i] - t) else after

    best_offset: float | None = None
    best_cost = float("inf")
    n_steps = int(max_offset_s / step_s)
    for k in range(-n_steps, n_steps + 1):
        offset = k * step_s
        dists = [
            _equirect_m2(lat, lon, *nearest_ref(ts.timestamp() + offset)[1:])
            for ts, lat, lon in track
        ]
        cost = statistics.median(dists)
        if cost < best_cost:
            best_cost = cost
            best_offset = offset
    return best_offset


# ---------------------------------------------------------------------------
# OS-level GPS clock discipline at boot (#799)
# ---------------------------------------------------------------------------
#
# The in-recording-path discipline above corrects *record* timestamps, but a
# race marked before GPS sync still gets a host-time boundary (the pre-sync
# residual #797 leaves). The complete fix is to step the *system* clock to GPS
# time once, at boot, before the recording service starts — so even raw host
# stamps are right. This orchestration is kept hardware-free: ``main.py`` injects
# the SK-offset reader, the system-clock setter, and the sync check.

# Don't step the system clock for sub-threshold jitter — only a real boot skew.
STEP_THRESHOLD_S: float = 10.0


def should_step_clock(
    *, offset_s: float, clock_synchronized: bool, threshold_s: float = STEP_THRESHOLD_S
) -> bool:
    """Decide whether to step the system clock to GPS time at boot.

    Step only when the system clock is **not** already synchronized — never
    fight an authoritative NTP/timesyncd source — and the host↔GPS offset
    clears ``threshold_s`` so jitter can't trigger a needless step.
    """
    if clock_synchronized:
        return False
    return abs(offset_s) >= threshold_s


@dataclass(frozen=True)
class BootClockSyncResult:
    """Outcome of a boot-time GPS clock-sync attempt (for logging/tests)."""

    action: str  # "stepped" | "skipped-synchronized" | "skipped-small" | "no-gps"
    offset_s: float | None = None
    target_utc: datetime | None = None


async def gps_boot_clock_sync(
    *,
    measure_offset: Callable[[], Awaitable[float | None]],
    is_synchronized: Callable[[], bool],
    set_clock: Callable[[datetime], None],
    now: Callable[[], datetime],
    threshold_s: float = STEP_THRESHOLD_S,
) -> BootClockSyncResult:
    """Best-effort: step the system clock to GPS time at boot if it's adrift.

    All side-effecting parts are injected so this stays hardware-free and fully
    testable: ``measure_offset`` reads the host↔GPS offset from Signal K
    (``None`` if no GPS reference appears in time), ``is_synchronized`` reports
    whether the OS clock is already disciplined, ``set_clock`` applies the step,
    and ``now`` supplies the host time being corrected.

    Never raises and never blocks indefinitely (the injected ``measure_offset``
    owns the timeout): the in-app discipliner + boundary guard remain the safety
    net, so a no-op here only forfeits the *system*-clock correction.
    """
    offset = await measure_offset()
    if offset is None:
        return BootClockSyncResult(action="no-gps")
    synchronized = is_synchronized()
    if not should_step_clock(
        offset_s=offset, clock_synchronized=synchronized, threshold_s=threshold_s
    ):
        action = "skipped-synchronized" if synchronized else "skipped-small"
        return BootClockSyncResult(action=action, offset_s=offset)
    target = now() + timedelta(seconds=offset)
    set_clock(target)
    return BootClockSyncResult(action="stepped", offset_s=offset, target_utc=target)
