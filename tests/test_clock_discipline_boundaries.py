"""Disciplined-clock stamping of race/audio boundaries + a boundary-vs-data
consistency guard (#794 follow-up).

PR #795 routed the *recording stream* through the GPS discipliner but left the
*web-route* timestamps (race start/end, audio sessions) on the raw host clock.
On an unsynced-boot Pi that splits a recording: the telemetry is GPS-correct
while the race window is host-skewed (the race-203 incident — boundaries 2630s
behind their own data). These tests pin:

1. ``Storage.disciplined_now`` / ``discipline_ts`` apply the live GPS offset.
2. ``_close_race`` flags a race whose boundaries disagree with its own
   (disciplined) telemetry — the failure the Vakaros track cross-check misses,
   because it validates the track, not the boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.clock_sync import ClockFlag

if TYPE_CHECKING:
    from helmlog.storage import Storage

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 6, 18, 1, 0, 0, tzinfo=UTC)
SKEW_S = 2630.0  # the race-203 offset: host clock 43m50s slow


async def _seed_positions(storage: Storage, race_id: int, start: datetime, span_s: int) -> None:
    db = storage._conn()  # noqa: SLF001
    rows = [
        ((start + timedelta(seconds=k)).isoformat(), 0, 47.60 + k * 1e-5, -122.40, race_id)
        for k in range(0, span_s, 10)
    ]
    await db.executemany(
        "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg, race_id)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


class TestDisciplinedNow:
    async def test_passthrough_before_any_gps_ref(self, storage: Storage) -> None:
        before = datetime.now(UTC)
        d = storage.disciplined_now()
        after = datetime.now(UTC)
        assert before <= d <= after  # default: no offset known yet -> host time

    async def test_applies_offset_when_corrected(self, storage: Storage) -> None:
        storage.update_clock(SKEW_S, ClockFlag.CORRECTED)
        gap = (storage.disciplined_now() - datetime.now(UTC)).total_seconds()
        assert abs(gap - SKEW_S) < 2.0

    async def test_applies_offset_when_ref_lost(self, storage: Storage) -> None:
        storage.update_clock(SKEW_S, ClockFlag.REF_LOST)
        gap = (storage.disciplined_now() - datetime.now(UTC)).total_seconds()
        assert abs(gap - SKEW_S) < 2.0

    async def test_no_offset_when_synced(self, storage: Storage) -> None:
        # SYNCED means host is within skew tolerance — trust it as-is.
        storage.update_clock(5.0, ClockFlag.SYNCED)
        gap = (storage.disciplined_now() - datetime.now(UTC)).total_seconds()
        assert abs(gap) < 2.0

    async def test_no_offset_when_unverified(self, storage: Storage) -> None:
        storage.update_clock(0.0, ClockFlag.UNVERIFIED)
        gap = (storage.disciplined_now() - datetime.now(UTC)).total_seconds()
        assert abs(gap) < 2.0

    async def test_discipline_ts_shifts_given_timestamp(self, storage: Storage) -> None:
        storage.update_clock(SKEW_S, ClockFlag.CORRECTED)
        assert storage.discipline_ts(BASE) == BASE + timedelta(seconds=SKEW_S)


class TestBoundaryGuard:
    async def test_flags_unverified_when_data_extends_past_end(self, storage: Storage) -> None:
        # Race marked + ended on a host clock that is SKEW_S slow, while the
        # telemetry is GPS-disciplined (correct). End_utc lands ~SKEW_S before
        # the last recorded fix — physically impossible with a sane clock.
        race = await storage.start_race("Test", BASE, "2026-06-18", 1, "20260618-skew-1")
        await storage.set_race_clock_flag(race.id, ClockFlag.CORRECTED.value)
        # Disciplined data runs from BASE for 90 min.
        await _seed_positions(storage, race.id, BASE, span_s=5400)
        # The host-skewed "end" button fires SKEW_S after start in host time.
        await storage.end_race(race.id, BASE + timedelta(seconds=100))
        race2 = await storage.get_race(race.id)
        assert race2 is not None
        assert race2.clock_flag == ClockFlag.UNVERIFIED.value

    async def test_keeps_flag_when_boundaries_consistent(self, storage: Storage) -> None:
        race = await storage.start_race("Test", BASE, "2026-06-18", 2, "20260618-ok-2")
        await storage.set_race_clock_flag(race.id, ClockFlag.CORRECTED.value)
        await _seed_positions(storage, race.id, BASE, span_s=900)
        # End after the last fix — the normal case.
        await storage.end_race(race.id, BASE + timedelta(seconds=950))
        race2 = await storage.get_race(race.id)
        assert race2 is not None
        assert race2.clock_flag == ClockFlag.CORRECTED.value

    async def test_no_flag_when_race_has_no_telemetry(self, storage: Storage) -> None:
        # Nothing to compare against — leave the flag untouched, never false-positive.
        race = await storage.start_race("Test", BASE, "2026-06-18", 3, "20260618-empty-3")
        await storage.set_race_clock_flag(race.id, ClockFlag.CORRECTED.value)
        await storage.end_race(race.id, BASE + timedelta(seconds=950))
        race2 = await storage.get_race(race.id)
        assert race2 is not None
        assert race2.clock_flag == ClockFlag.CORRECTED.value


class TestAudioBoundaryDiscipline:
    async def test_audio_start_end_disciplined_on_persist(self, storage: Storage) -> None:
        from helmlog.audio import AudioSession

        storage.update_clock(SKEW_S, ClockFlag.CORRECTED)
        host_start = BASE
        host_end = BASE + timedelta(seconds=600)
        session = AudioSession(
            file_path="/tmp/a.wav",
            device_name="dev",
            start_utc=host_start,
            end_utc=None,
            sample_rate=48000,
            channels=1,
        )
        sid = await storage.write_audio_session(session, race_id=None, session_type="race")
        await storage.update_audio_session_end(sid, host_end)
        row = await storage.get_audio_session_row(sid)
        assert row is not None
        assert datetime.fromisoformat(row["start_utc"]) == host_start + timedelta(seconds=SKEW_S)
        assert datetime.fromisoformat(row["end_utc"]) == host_end + timedelta(seconds=SKEW_S)

    async def test_audio_untouched_when_not_disciplining(self, storage: Storage) -> None:
        from helmlog.audio import AudioSession

        storage.update_clock(0.0, ClockFlag.UNVERIFIED)  # default: host time, no shift
        session = AudioSession(
            file_path="/tmp/b.wav",
            device_name="dev",
            start_utc=BASE,
            end_utc=None,
            sample_rate=48000,
            channels=1,
        )
        sid = await storage.write_audio_session(session, race_id=None, session_type="race")
        row = await storage.get_audio_session_row(sid)
        assert row is not None
        assert datetime.fromisoformat(row["start_utc"]) == BASE
