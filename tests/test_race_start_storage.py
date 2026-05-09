"""Storage tests for #644 race-start tables (race_start_state + start_line_pings)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage


T0 = datetime(2026, 5, 1, 13, 45, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# race_start_state — singleton
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_empty(storage: Storage) -> None:
    assert await storage.get_race_start_state() is None


@pytest.mark.asyncio
async def test_upsert_then_read(storage: Storage) -> None:
    await storage.upsert_race_start_state(
        phase="armed",
        kind="5-4-1-0",
        t0_utc=T0,
        sync_offset_s=0.0,
        last_sync_at_utc=None,
        started_at_utc=None,
        classes_json="[]",
        now_utc=T0,
    )
    row = await storage.get_race_start_state()
    assert row is not None
    assert row["phase"] == "armed"
    assert row["kind"] == "5-4-1-0"
    assert row["t0_utc"] == T0.isoformat()
    assert row["classes_json"] == "[]"


@pytest.mark.asyncio
async def test_upsert_replaces_singleton(storage: Storage) -> None:
    await storage.upsert_race_start_state(
        phase="armed",
        kind="5-4-1-0",
        t0_utc=T0,
        sync_offset_s=0.0,
        last_sync_at_utc=None,
        started_at_utc=None,
        classes_json="[]",
        now_utc=T0,
    )
    await storage.upsert_race_start_state(
        phase="counting_down",
        kind="5-4-1-0",
        t0_utc=T0,
        sync_offset_s=2.5,
        last_sync_at_utc=T0,
        started_at_utc=None,
        classes_json="[]",
        now_utc=T0,
    )
    row = await storage.get_race_start_state()
    assert row is not None
    assert row["phase"] == "counting_down"
    assert row["sync_offset_s"] == 2.5

    # Only one row ever exists.
    cur = await storage._conn().execute(  # noqa: SLF001
        "SELECT COUNT(*) FROM race_start_state"
    )
    count_row = await cur.fetchone()
    assert count_row is not None
    assert count_row[0] == 1


@pytest.mark.asyncio
async def test_clear_state(storage: Storage) -> None:
    await storage.upsert_race_start_state(
        phase="armed",
        kind="5-4-1-0",
        t0_utc=T0,
        sync_offset_s=0.0,
        last_sync_at_utc=None,
        started_at_utc=None,
        classes_json="[]",
        now_utc=T0,
    )
    await storage.clear_race_start_state()
    assert await storage.get_race_start_state() is None


@pytest.mark.asyncio
async def test_classes_json_round_trip(storage: Storage) -> None:
    classes = [
        {"name": "PHRF-A", "order": 0, "is_ours": False, "prep_flag": "P"},
        {"name": "J/70", "order": 1, "is_ours": True, "prep_flag": "I"},
    ]
    await storage.upsert_race_start_state(
        phase="armed",
        kind="5-4-1-0",
        t0_utc=T0,
        sync_offset_s=0.0,
        last_sync_at_utc=None,
        started_at_utc=None,
        classes_json=json.dumps(classes),
        now_utc=T0,
    )
    row = await storage.get_race_start_state()
    assert row is not None
    assert json.loads(row["classes_json"]) == classes


# ---------------------------------------------------------------------------
# start_line_pings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_ping_returns_id(storage: Storage) -> None:
    pid = await storage.add_start_line_ping(
        race_id=None,
        end_kind="boat",
        latitude_deg=47.65,
        longitude_deg=-122.40,
        captured_at=T0,
        captured_by=None,
    )
    assert pid > 0


@pytest.mark.asyncio
async def test_add_ping_invalid_end_raises(storage: Storage) -> None:
    with pytest.raises(ValueError, match="end_kind"):
        await storage.add_start_line_ping(
            race_id=None,
            end_kind="committee",  # invalid
            latitude_deg=47.65,
            longitude_deg=-122.40,
            captured_at=T0,
            captured_by=None,
        )


@pytest.mark.asyncio
async def test_get_latest_unscoped_returns_none(storage: Storage) -> None:
    assert await storage.get_latest_start_line(race_id=None) is None


@pytest.mark.asyncio
async def test_get_latest_with_both_ends(storage: Storage) -> None:
    await storage.add_start_line_ping(
        race_id=None,
        end_kind="boat",
        latitude_deg=47.6500,
        longitude_deg=-122.4000,
        captured_at=T0,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=None,
        end_kind="pin",
        latitude_deg=47.6510,
        longitude_deg=-122.4010,
        captured_at=T0,
        captured_by=None,
    )
    line = await storage.get_latest_start_line(race_id=None)
    assert line is not None
    assert line["boat_end_lat"] == 47.6500
    assert line["pin_end_lat"] == 47.6510


@pytest.mark.asyncio
async def test_get_latest_uses_newest_per_end(storage: Storage) -> None:
    """Re-ping the boat end — newer ping wins, history preserved."""
    await storage.add_start_line_ping(
        race_id=None,
        end_kind="boat",
        latitude_deg=47.6500,
        longitude_deg=-122.4000,
        captured_at=T0,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=None,
        end_kind="boat",
        latitude_deg=47.6505,  # re-pinged, slightly different
        longitude_deg=-122.4005,
        captured_at=T0,
        captured_by=None,
    )
    line = await storage.get_latest_start_line(race_id=None)
    assert line is not None
    assert line["boat_end_lat"] == 47.6505

    history = await storage.list_start_line_pings(race_id=None)
    assert len(history) == 2  # both pings preserved


@pytest.mark.asyncio
async def test_pings_scoped_by_race(storage: Storage) -> None:
    # Insert a race row (start_race auto-closes prior open races).
    race = await storage.start_race("BallardCup", T0, "2026-05-01", 1, "20260501-BallardCup-1")
    await storage.add_start_line_ping(
        race_id=race.id,
        end_kind="boat",
        latitude_deg=47.65,
        longitude_deg=-122.40,
        captured_at=T0,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=None,
        end_kind="boat",
        latitude_deg=47.66,
        longitude_deg=-122.41,
        captured_at=T0,
        captured_by=None,
    )
    # Race-scoped query only sees its own ping.
    line = await storage.get_latest_start_line(race_id=race.id)
    assert line is not None
    assert line["boat_end_lat"] == 47.65

    # Unscoped only sees the unscoped ping.
    line2 = await storage.get_latest_start_line(race_id=None)
    assert line2 is not None
    assert line2["boat_end_lat"] == 47.66


@pytest.mark.asyncio
async def test_list_pings_ordered_oldest_first(storage: Storage) -> None:
    pid1 = await storage.add_start_line_ping(
        race_id=None,
        end_kind="boat",
        latitude_deg=47.65,
        longitude_deg=-122.40,
        captured_at=T0,
        captured_by=None,
    )
    pid2 = await storage.add_start_line_ping(
        race_id=None,
        end_kind="pin",
        latitude_deg=47.66,
        longitude_deg=-122.41,
        captured_at=T0,
        captured_by=None,
    )
    history = await storage.list_start_line_pings(race_id=None)
    assert [p["id"] for p in history] == [pid1, pid2]


# ---------------------------------------------------------------------------
# Carry-over from prior same-date race (#702)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_carry_over_from_prior_race_same_date(storage: Storage) -> None:
    """When race 2 has no pings, fall back to race 1's pings on the same
    date, tagged with the source race_id."""
    date = "2026-04-30"
    r1 = await storage.start_race("CYC", T0, date, 1, "20260430-CYC-1")
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="boat",
        latitude_deg=47.6895,
        longitude_deg=-122.4160,
        captured_at=T0,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="pin",
        latitude_deg=47.6901,
        longitude_deg=-122.4189,
        captured_at=T0,
        captured_by=None,
    )
    r2 = await storage.start_race("CYC", T0, date, 2, "20260430-CYC-2")

    line = await storage.get_latest_start_line(race_id=r2.id)
    assert line is not None
    assert line["boat_end_lat"] == 47.6895
    assert line["pin_end_lat"] == 47.6901
    # Both ends came from race 1 — annotated for the UI banner.
    assert line["boat_end_race_id"] == r1.id
    assert line["pin_end_race_id"] == r1.id


@pytest.mark.asyncio
async def test_carry_over_mixes_per_end(storage: Storage) -> None:
    """If race 2 re-pinged the boat end only, that end is fresh while the
    pin end carries over from race 1."""
    date = "2026-04-30"
    r1 = await storage.start_race("CYC", T0, date, 1, "20260430-CYC-1")
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="boat",
        latitude_deg=47.6895,
        longitude_deg=-122.4160,
        captured_at=T0,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="pin",
        latitude_deg=47.6901,
        longitude_deg=-122.4189,
        captured_at=T0,
        captured_by=None,
    )
    r2 = await storage.start_race("CYC", T0, date, 2, "20260430-CYC-2")
    await storage.add_start_line_ping(
        race_id=r2.id,
        end_kind="boat",
        latitude_deg=47.6905,
        longitude_deg=-122.4170,
        captured_at=T0,
        captured_by=None,
    )

    line = await storage.get_latest_start_line(race_id=r2.id)
    assert line is not None
    # Boat end is the new race-2 ping.
    assert line["boat_end_lat"] == 47.6905
    assert line["boat_end_race_id"] == r2.id
    # Pin end carried over from race 1.
    assert line["pin_end_lat"] == 47.6901
    assert line["pin_end_race_id"] == r1.id


@pytest.mark.asyncio
async def test_carry_over_does_not_cross_dates(storage: Storage) -> None:
    """Pings from a different UTC date must not bleed forward."""
    yesterday = "2026-04-29"
    today = "2026-04-30"
    r_yesterday = await storage.start_race("CYC", T0, yesterday, 1, "20260429-CYC-1")
    await storage.add_start_line_ping(
        race_id=r_yesterday.id,
        end_kind="boat",
        latitude_deg=47.0,
        longitude_deg=-122.0,
        captured_at=T0,
        captured_by=None,
    )
    r_today = await storage.start_race("CYC", T0, today, 1, "20260430-CYC-1")

    line = await storage.get_latest_start_line(race_id=r_today.id)
    assert line is None


@pytest.mark.asyncio
async def test_no_carry_over_when_race_has_own_pings(storage: Storage) -> None:
    """If the race has both ends pinged itself, it doesn't reach back."""
    date = "2026-04-30"
    r1 = await storage.start_race("CYC", T0, date, 1, "20260430-CYC-1")
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="boat",
        latitude_deg=1.0,
        longitude_deg=1.0,
        captured_at=T0,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=r1.id,
        end_kind="pin",
        latitude_deg=2.0,
        longitude_deg=2.0,
        captured_at=T0,
        captured_by=None,
    )
    r2 = await storage.start_race("CYC", T0, date, 2, "20260430-CYC-2")
    await storage.add_start_line_ping(
        race_id=r2.id,
        end_kind="boat",
        latitude_deg=10.0,
        longitude_deg=10.0,
        captured_at=T0,
        captured_by=None,
    )
    await storage.add_start_line_ping(
        race_id=r2.id,
        end_kind="pin",
        latitude_deg=20.0,
        longitude_deg=20.0,
        captured_at=T0,
        captured_by=None,
    )
    line = await storage.get_latest_start_line(race_id=r2.id)
    assert line is not None
    assert line["boat_end_lat"] == 10.0
    assert line["pin_end_lat"] == 20.0
    assert line["boat_end_race_id"] == r2.id
    assert line["pin_end_race_id"] == r2.id


# ---------------------------------------------------------------------------
# Schema migration sanity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_v82_creates_tables(storage: Storage) -> None:
    """v82 migration must create both tables with the documented columns."""
    db = storage._conn()  # noqa: SLF001
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('race_start_state', 'start_line_pings')"
    )
    rows = await cur.fetchall()
    assert {r[0] for r in rows} == {"race_start_state", "start_line_pings"}
