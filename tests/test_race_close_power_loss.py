"""Race close anchors to last real data when power dies before the race is
ended (#785).

Spec: issue #785 — close-at-last-data (decision table A), boot reconciliation
(decision table B). Each test maps to a spec row, noted in the docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from helmlog.storage import Storage, StorageConfig, _reconcile_end_utc

if TYPE_CHECKING:
    from pathlib import Path

_GRACE = 120.0
_BOOT_WINDOW = 600.0


async def _insert_cogsog(storage: Storage, race_id: int, ts: datetime) -> None:
    """Insert one telemetry row so a race has a known last-data timestamp."""
    db = storage._conn()  # noqa: SLF001 — white-box telemetry seed
    await db.execute(
        "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts, race_id) VALUES (?, ?, ?, ?, ?)",
        (ts.isoformat(), 1, 0.0, 3.0, race_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Pure reconciliation helper
# ---------------------------------------------------------------------------


def test_reconcile_no_data_uses_proposed() -> None:
    """No telemetry → nothing to anchor to, keep the proposed end (rows A3/A6/A9)."""
    proposed = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)
    assert _reconcile_end_utc(proposed, None, _GRACE) == proposed


def test_reconcile_gap_beyond_grace_uses_last_data() -> None:
    """Proposed end far after last data → anchor to last data (rows A2/A5/A8)."""
    last = datetime(2026, 6, 9, 2, 49, 37, tzinfo=UTC)
    proposed = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)  # ~5h later
    assert _reconcile_end_utc(proposed, last, _GRACE) == last


def test_reconcile_within_grace_uses_proposed() -> None:
    """Data still flowing (gap <= grace) → keep the proposed end (rows A1/A4/A7)."""
    last = datetime(2026, 6, 9, 8, 0, 0, tzinfo=UTC)
    proposed = last + timedelta(seconds=60)  # within 120s grace
    assert _reconcile_end_utc(proposed, last, _GRACE) == proposed


def test_reconcile_boundary_equal_to_grace_uses_proposed() -> None:
    """Exactly grace seconds is not a gap; only strictly greater anchors back."""
    last = datetime(2026, 6, 9, 8, 0, 0, tzinfo=UTC)
    proposed = last + timedelta(seconds=_GRACE)
    assert _reconcile_end_utc(proposed, last, _GRACE) == proposed


# ---------------------------------------------------------------------------
# last_record_utc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_record_utc_none_when_empty(storage: Storage) -> None:
    race = await storage.start_race("CYC", datetime.now(UTC), "2026-06-09", 1, "R1")
    assert await storage.last_record_utc(race.id) is None


@pytest.mark.asyncio
async def test_last_record_utc_returns_max_ts(storage: Storage) -> None:
    race = await storage.start_race("CYC", datetime.now(UTC), "2026-06-09", 1, "R1")
    t0 = datetime(2026, 6, 9, 2, 0, 0, tzinfo=UTC)
    await _insert_cogsog(storage, race.id, t0)
    await _insert_cogsog(storage, race.id, t0 + timedelta(minutes=49))
    last = await storage.last_record_utc(race.id)
    assert last == t0 + timedelta(minutes=49)


# ---------------------------------------------------------------------------
# end_race close-time (decision table A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_race_live_data_uses_now(storage: Storage) -> None:
    """A1: ending on the water with data still arriving → end == now."""
    now = datetime.now(UTC)
    race = await storage.start_race("CYC", now - timedelta(hours=1), "2026-06-09", 1, "R1")
    await _insert_cogsog(storage, race.id, now - timedelta(seconds=5))
    await storage.end_race(race.id, now)
    r = await storage.get_race(race.id)
    assert r is not None and r.end_utc == now


@pytest.mark.asyncio
async def test_end_race_after_power_loss_uses_last_data(storage: Storage) -> None:
    """A2: data stopped hours ago, closed later → end == last data, audited."""
    now = datetime.now(UTC)
    last = now - timedelta(hours=5)
    race = await storage.start_race("CYC", now - timedelta(hours=8), "2026-06-09", 1, "R1")
    await _insert_cogsog(storage, race.id, last)
    await storage.end_race(race.id, now)
    r = await storage.get_race(race.id)
    assert r is not None and r.end_utc == last
    audit = await storage.list_audit_log(limit=10)
    assert any(e["action"] == "race_auto_close" for e in audit)


@pytest.mark.asyncio
async def test_end_race_empty_race_uses_now(storage: Storage) -> None:
    """A3: empty race manual close → falls back to now, no audit entry."""
    now = datetime.now(UTC)
    race = await storage.start_race("CYC", now - timedelta(minutes=5), "2026-06-09", 1, "R1")
    await storage.end_race(race.id, now)
    r = await storage.get_race(race.id)
    assert r is not None and r.end_utc == now
    audit = await storage.list_audit_log(limit=10)
    assert not any(e["action"] == "race_auto_close" for e in audit)


# ---------------------------------------------------------------------------
# start_race auto-close of a prior open race (decision table A, rows 7-9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_race_closes_prior_live_at_new_start(storage: Storage) -> None:
    """A7: back-to-back races, prior live up to new gun → prior end == new start."""
    base = datetime.now(UTC) - timedelta(hours=2)
    r1 = await storage.start_race("CYC", base, "2026-06-09", 1, "R1")
    await _insert_cogsog(storage, r1.id, base + timedelta(minutes=30))
    new_start = base + timedelta(minutes=30, seconds=30)  # within grace of last data
    await storage.start_race("CYC", new_start, "2026-06-09", 2, "R2")
    r1_after = await storage.get_race(r1.id)
    assert r1_after is not None and r1_after.end_utc == new_start


@pytest.mark.asyncio
async def test_start_race_closes_stale_prior_at_last_data(storage: Storage) -> None:
    """A8: prior race's data stopped long before the new race → end == last data."""
    base = datetime.now(UTC) - timedelta(hours=12)
    r1 = await storage.start_race("CYC", base, "2026-06-09", 1, "R1")
    last = base + timedelta(minutes=30)
    await _insert_cogsog(storage, r1.id, last)
    new_start = datetime.now(UTC)  # hours after last data
    await storage.start_race("CYC", new_start, "2026-06-09", 2, "R2")
    r1_after = await storage.get_race(r1.id)
    assert r1_after is not None and r1_after.end_utc == last


# ---------------------------------------------------------------------------
# Boot reconciliation (decision table B) — needs a file-backed DB across
# two Storage instances (a reboot).
# ---------------------------------------------------------------------------


async def _reopen(path: str) -> Storage:
    s = Storage(
        StorageConfig(db_path=path, close_grace_s=_GRACE, boot_resume_window_s=_BOOT_WINDOW)
    )
    await s.connect()
    return s


@pytest.mark.asyncio
async def test_boot_resumes_fresh_open_race(tmp_path: Path) -> None:
    """B1: quick reboot, recent data (gap < window) → race stays active, resumes."""
    db = str(tmp_path / "t.db")
    s = await _reopen(db)
    now = datetime.now(UTC)
    race = await s.start_race("CYC", now - timedelta(minutes=20), "2026-06-09", 1, "R1")
    await _insert_cogsog(s, race.id, now - timedelta(seconds=30))  # gap << 600s
    await s.close()

    s2 = await _reopen(db)
    assert s2.session_active is True
    assert s2._active_race_id == race.id  # noqa: SLF001
    cur = await s2._conn().execute("SELECT end_utc FROM races WHERE id = ?", (race.id,))  # noqa: SLF001
    assert (await cur.fetchone())["end_utc"] is None  # still open
    await s2.close()


@pytest.mark.asyncio
async def test_boot_closes_stale_open_race_at_last_data(tmp_path: Path) -> None:
    """B2: overnight power loss (gap >= window) → close at last data, not resumed."""
    db = str(tmp_path / "t.db")
    s = await _reopen(db)
    now = datetime.now(UTC)
    last = now - timedelta(hours=12)
    race = await s.start_race("CYC", now - timedelta(hours=14), "2026-06-08", 1, "R1")
    await _insert_cogsog(s, race.id, last)
    await s.close()

    s2 = await _reopen(db)
    assert s2.session_active is False
    assert s2._active_race_id is None  # noqa: SLF001
    r = await s2.get_race(race.id)
    assert r is not None and r.end_utc == last
    audit = await s2.list_audit_log(limit=10)
    assert any(e["action"] == "race_auto_close" for e in audit)
    await s2.close()


@pytest.mark.asyncio
async def test_boot_deletes_empty_open_race(tmp_path: Path) -> None:
    """B3: open race with no data at all on boot → deleted."""
    db = str(tmp_path / "t.db")
    s = await _reopen(db)
    race = await s.start_race("CYC", datetime.now(UTC) - timedelta(hours=2), "2026-06-09", 1, "R1")
    await s.close()

    s2 = await _reopen(db)
    assert s2.session_active is False
    assert s2._active_race_id is None  # noqa: SLF001
    assert await s2.get_race(race.id) is None  # row gone
    await s2.close()


@pytest.mark.asyncio
async def test_boot_no_open_race_is_idle(tmp_path: Path) -> None:
    """B4: a properly-closed race on boot → idle, untouched."""
    db = str(tmp_path / "t.db")
    s = await _reopen(db)
    now = datetime.now(UTC)
    race = await s.start_race("CYC", now - timedelta(hours=2), "2026-06-09", 1, "R1")
    await _insert_cogsog(s, race.id, now - timedelta(hours=1))
    await s.end_race(race.id, now - timedelta(minutes=58))
    await s.close()

    s2 = await _reopen(db)
    assert s2.session_active is False
    assert s2._active_race_id is None  # noqa: SLF001
    assert await s2.get_race(race.id) is not None  # untouched
    await s2.close()
