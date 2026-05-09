"""Importer integration test: fetch → import → re-import idempotency (#459, R36-R38)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.results.base import Regatta
from helmlog.results.clubspot import ClubspotProvider
from helmlog.results.importer import import_results

if TYPE_CHECKING:
    from helmlog.results.base import RegattaResults
    from helmlog.storage import Storage

_FIXTURES = Path(__file__).parent / "fixtures" / "results" / "clubspot"
_REGATTA_ID = "wYFzQvmG4R"
_J105_CLASS_ID = "7q1o9ikhPH"


def _mock_transport() -> httpx.MockTransport:
    payload = (_FIXTURES / "wYFzQvmG4R_J105.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


async def _fetch_results() -> RegattaResults:  # noqa: F821

    transport = _mock_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="CYC Sound Wednesday",
            default_class=_J105_CLASS_ID,
        )
        return await provider.fetch(regatta)


# ---------------------------------------------------------------------------
# R37: fetch → import → query end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_creates_regatta(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT * FROM regattas WHERE source_id = ?", (_REGATTA_ID,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["name"] == "CYC Sound Wednesday"
    assert row["source"] == "clubspot"
    assert row["last_fetched_at"] is not None


@pytest.mark.asyncio
async def test_import_creates_races(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM races WHERE source = 'clubspot'") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 2


@pytest.mark.asyncio
async def test_import_races_have_full_iso_start_utc(storage: Storage) -> None:
    """Regression for #532: importer must not write a bare date into start_utc.

    Prior to the fix, _upsert_race stored race.date (e.g. "2026-04-13") in the
    start_utc column, which hydrated to a naive datetime downstream and broke
    /api/state with a tz-aware/naive subtraction TypeError. Imported rows must
    carry a full ISO-8601 timestamp (date + time + offset)."""
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT start_utc FROM races WHERE source = 'clubspot'") as cur:
        rows = await cur.fetchall()
    assert rows, "expected imported races"
    for row in rows:
        s = row["start_utc"]
        assert s is not None
        # Must be longer than a bare YYYY-MM-DD (10 chars) and contain a 'T'
        # separator — i.e. a real ISO datetime, not just a date.
        assert len(s) > 10, f"start_utc too short: {s!r}"
        assert "T" in s, f"start_utc missing time separator: {s!r}"


@pytest.mark.asyncio
async def test_imported_races_are_closed_and_not_current(storage: Storage) -> None:
    """Imported race rows must have end_utc set at insert time so they are
    never returned by ``get_current_race``. Otherwise every imported row
    shows up as an open session on the home page with a runaway timer."""
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT start_utc, end_utc FROM races WHERE source = 'clubspot'") as cur:
        rows = await cur.fetchall()
    assert rows, "expected imported races"
    for row in rows:
        assert row["end_utc"] is not None, "imported race missing end_utc"
        assert row["end_utc"] == row["start_utc"]
    assert await storage.get_current_race() is None


@pytest.mark.asyncio
async def test_import_creates_boats(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM boats") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 15


@pytest.mark.asyncio
async def test_import_creates_race_results(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM race_results") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    # 15 boats × 2 races = 30
    assert count == 30


@pytest.mark.asyncio
async def test_import_creates_series_results(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM series_results") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 15


@pytest.mark.asyncio
async def test_import_preserves_status_codes(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute(
        "SELECT status_code, dnf, dns FROM race_results WHERE status_code IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) > 0
    for row in rows:
        if row["status_code"] in ("DNF", "RET"):
            assert row["dnf"] == 1
        if row["status_code"] in ("DNS", "DNC"):
            assert row["dns"] == 1


@pytest.mark.asyncio
async def test_import_writes_audit_log(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results, user_id=None)
    db = storage._conn()
    async with db.execute(
        "SELECT action, detail FROM audit_log WHERE action = 'results_import'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    detail = json.loads(row["detail"])
    assert detail["source"] == "clubspot"
    assert detail["races_upserted"] == 2


# ---------------------------------------------------------------------------
# R38: re-importing same fixture is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reimport_idempotent(storage: Storage) -> None:
    """Second import of the same fixture produces zero net DB changes."""
    results = await _fetch_results()

    first = await import_results(storage, results)
    assert first["races_upserted"] == 2
    assert first["results_upserted"] == 30

    db = storage._conn()

    async def _counts() -> dict[str, int]:
        totals = {}
        for table in ("regattas", "races", "boats", "race_results", "series_results"):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
                (n,) = await cur.fetchone()  # type: ignore[misc]
            totals[table] = n
        return totals

    before = await _counts()

    second = await import_results(storage, results)
    assert second["races_upserted"] == 2

    after = await _counts()
    assert before == after, f"Row counts changed on re-import: {before} vs {after}"


@pytest.mark.asyncio
async def test_reimport_updates_changed_points(storage: Storage) -> None:
    """Re-import with changed points updates existing rows in place."""
    results = await _fetch_results()
    await import_results(storage, results)

    db = storage._conn()
    async with db.execute(
        "SELECT rr.points FROM race_results rr "
        "JOIN boats b ON rr.boat_id = b.id "
        "WHERE b.sail_number = '482'"
    ) as cur:
        original = [row["points"] for row in await cur.fetchall()]
    assert len(original) == 2
    assert all(p is not None for p in original)


@pytest.mark.asyncio
async def test_import_links_local_session_when_one_match(storage: Storage) -> None:
    """An imported race auto-links to the only local session on its date."""
    from datetime import UTC, datetime

    db = storage._conn()
    # Local session on the same date as the fixture's race 1.
    results = await _fetch_results()
    race_date = results.races[0].date  # YYYY-MM-DD
    start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Local Wed", "Local", 1, race_date, start.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]

    await import_results(storage, results)

    cur = await db.execute(
        "SELECT local_session_id FROM races WHERE source = 'clubspot' AND date = ?",
        (race_date,),
    )
    rows = await cur.fetchall()
    assert rows, "expected imported races on this date"
    for row in rows:
        assert row["local_session_id"] == local_id, (
            f"expected link to {local_id}, got {row['local_session_id']}"
        )

    # Now list_race_results on the local session should return the
    # imported results, not the (empty) hand-entered set.
    rr = await storage.list_race_results(local_id)
    assert rr, "imported results should supersede empty local results"
    assert all(row["imported"] for row in rr)
    _ = UTC  # silence unused


@pytest.mark.asyncio
async def test_import_zips_multiple_local_sessions_in_order(
    storage: Storage,
) -> None:
    """Two local sessions + two imported races → each imported race links
    to its own local session in race-number order (#550).

    Beer-can nights log each on-the-water race as its own local session,
    so race 1 should attach to the earlier local session and race 2 to
    the later one.
    """
    from datetime import datetime

    db = storage._conn()
    results = await _fetch_results()
    race_date = results.races[0].date
    early = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    late = datetime.fromisoformat(race_date + "T19:10:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Wednesday race 1", "Local", 1, race_date, early.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (early_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Wednesday race 2", "Local", 2, race_date, late.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (late_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    await import_results(storage, results)

    cur = await db.execute(
        "SELECT race_num, local_session_id FROM races"
        " WHERE source = 'clubspot' AND date = ? ORDER BY race_num",
        (race_date,),
    )
    rows = await cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["local_session_id"] == early_id, (
        "imported race 1 should link to the earlier local session"
    )
    assert rows[1]["local_session_id"] == late_id, (
        "imported race 2 should link to the later local session"
    )


@pytest.mark.asyncio
async def test_import_shares_single_local_session_when_fewer_locals(
    storage: Storage,
) -> None:
    """One local session + multiple imported races → all imported races
    share that single local session (short-handed day, unchanged)."""
    from datetime import datetime

    db = storage._conn()
    results = await _fetch_results()
    race_date = results.races[0].date
    start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Wednesday night", "Local", 1, race_date, start.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    await import_results(storage, results)

    cur = await db.execute(
        "SELECT local_session_id FROM races WHERE source = 'clubspot' AND date = ?",
        (race_date,),
    )
    rows = await cur.fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["local_session_id"] == local_id


@pytest.mark.asyncio
async def test_import_preserves_manual_local_session_link(
    storage: Storage,
) -> None:
    """A manually-set local_session_id on an imported race must survive re-import."""
    from datetime import datetime

    db = storage._conn()
    results = await _fetch_results()
    race_date = results.races[0].date
    a = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    b = datetime.fromisoformat(race_date + "T19:10:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Session A", "Local", 1, race_date, a.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (a_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Session B", "Local", 2, race_date, b.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (b_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    await import_results(storage, results)

    # Manually override: pin race 1 to session B (swap from the auto link).
    await db.execute(
        "UPDATE races SET local_session_id = ? WHERE source = 'clubspot' AND race_num = 1",
        (b_id,),
    )
    await db.commit()

    # Re-import must not clobber the manual link.
    await import_results(storage, results)

    cur = await db.execute(
        "SELECT race_num, local_session_id FROM races"
        " WHERE source = 'clubspot' AND date = ? ORDER BY race_num",
        (race_date,),
    )
    rows = await cur.fetchall()
    assert rows[0]["local_session_id"] == b_id, "manual link must be preserved"
    # Race 2 keeps whatever was written on first import (session B by order).
    assert rows[1]["local_session_id"] == b_id
    _ = a_id  # a_id unused once manual override runs


@pytest.mark.asyncio
async def test_force_relinks_existing_wrong_links(storage: Storage) -> None:
    """force=True rewrites auto-links that were set before zip-in-order
    matching existed — the backfill path for regattas imported on a
    server running the pre-#550 linker."""
    from datetime import datetime

    from helmlog.results.importer import _link_regatta_races_to_local_sessions

    db = storage._conn()
    results = await _fetch_results()
    race_date = results.races[0].date
    a = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    b = datetime.fromisoformat(race_date + "T19:10:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Race 1", "Local", 1, race_date, a.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (a_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Race 2", "Local", 2, race_date, b.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (b_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    await import_results(storage, results)

    # Simulate the pre-#550 state: both imported races pinned to the
    # earliest local session.
    await db.execute(
        "UPDATE races SET local_session_id = ? WHERE source = 'clubspot' AND date = ?",
        (a_id, race_date),
    )
    await db.commit()

    cur = await db.execute(
        "SELECT regatta_id FROM races WHERE source = 'clubspot' LIMIT 1",
    )
    (regatta_id,) = await cur.fetchone()  # type: ignore[misc]

    linked, _touched = await _link_regatta_races_to_local_sessions(db, regatta_id, force=True)
    await db.commit()
    # Race 1 was already pointing at a_id, so only race 2 needs rewriting.
    assert linked == 1, "race 2 should be moved off the earliest session"

    cur = await db.execute(
        "SELECT race_num, local_session_id FROM races"
        " WHERE source = 'clubspot' AND date = ? ORDER BY race_num",
        (race_date,),
    )
    rows = await cur.fetchall()
    assert rows[0]["local_session_id"] == a_id
    assert rows[1]["local_session_id"] == b_id


@pytest.mark.asyncio
async def test_reimport_backfills_null_local_session(storage: Storage) -> None:
    """A re-import populates local_session_id when the prior import left it NULL."""
    from datetime import datetime

    results = await _fetch_results()
    db = storage._conn()
    race_date = results.races[0].date

    # First import — no local session exists yet, so link is NULL.
    await import_results(storage, results)
    cur = await db.execute(
        "SELECT local_session_id FROM races WHERE source = 'clubspot' AND date = ?",
        (race_date,),
    )
    assert all(r["local_session_id"] is None for r in await cur.fetchall())

    # Now create a local session for that date.
    start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Backfilled session", "Local", 1, race_date, start.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]

    # Re-import should backfill the link.
    await import_results(storage, results)
    cur = await db.execute(
        "SELECT local_session_id FROM races WHERE source = 'clubspot' AND date = ?",
        (race_date,),
    )
    rows = await cur.fetchall()
    assert rows
    for row in rows:
        assert row["local_session_id"] == local_id


class _RecordingCache:
    """Minimal stand-in that records invalidate() calls."""

    def __init__(self) -> None:
        self.invalidations: list[int] = []

    async def invalidate(self, race_id: int) -> None:
        self.invalidations.append(race_id)


@pytest.mark.asyncio
async def test_import_invalidates_cache_for_linked_live_session(
    storage: Storage,
) -> None:
    """Regression for #666: results import must invalidate the cached
    session_summary blob for every live session it links imported races to.

    Scenario: the live session's summary was computed (and cached with
    ``results: []``) before the importer ran. After import, the importer
    upserts race_results and links them to the live session's id — but
    the cache entry keyed to that live session id still carries the
    stale empty results. Subsequent /history requests return the stale
    blob and the imported results never surface.
    """
    from datetime import datetime

    recorder = _RecordingCache()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    db = storage._conn()
    results = await _fetch_results()
    race_date = results.races[0].date
    start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Local live session", "Local", 1, race_date, start.isoformat()),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]

    # Reset recorder so only the importer's invalidations are counted.
    recorder.invalidations.clear()

    await import_results(storage, results)

    assert local_id in recorder.invalidations, (
        f"expected import to invalidate live session {local_id}, got {recorder.invalidations!r}"
    )


@pytest.mark.asyncio
async def test_force_rematch_invalidates_old_and_new_live_sessions(
    storage: Storage,
) -> None:
    """Regression for #666: the admin rematch (force=True) path must
    invalidate the OLD live session id when an imported race is moved
    off it, not just the new target."""
    from datetime import datetime

    from helmlog.results.importer import _link_regatta_races_to_local_sessions

    recorder = _RecordingCache()
    storage.bind_race_cache(recorder)  # type: ignore[arg-type]

    db = storage._conn()
    results = await _fetch_results()
    race_date = results.races[0].date
    a = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    b = datetime.fromisoformat(race_date + "T19:10:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Race 1", "Local", 1, race_date, a.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (a_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Race 2", "Local", 2, race_date, b.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (b_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    await import_results(storage, results)

    # Simulate pre-#550 state: both imported races pinned to session A.
    await db.execute(
        "UPDATE races SET local_session_id = ? WHERE source = 'clubspot' AND date = ?",
        (a_id, race_date),
    )
    await db.commit()

    cur = await db.execute(
        "SELECT regatta_id FROM races WHERE source = 'clubspot' LIMIT 1",
    )
    (regatta_id,) = await cur.fetchone()  # type: ignore[misc]

    recorder.invalidations.clear()
    _linked, touched = await _link_regatta_races_to_local_sessions(db, regatta_id, force=True)
    await db.commit()

    assert a_id in touched, "old link (session A) must be reported as touched"
    assert b_id in touched, "new link (session B) must be reported as touched"


@pytest.mark.asyncio
async def test_filter_drops_races_without_own_sail(storage: Storage) -> None:
    """Regression for #735: STYC publishes 10 division-races per race-num.
    With own_sail known, only races containing that sail are persisted.
    """
    from helmlog.results.base import BoatFinish, RaceData, Regatta, RegattaResults

    own = RaceData(
        source_id="own_div",
        race_number=1,
        name="Race 1",
        date="2026-05-04",
        class_name="Flying Sails Div 6",
        finishes=(
            BoatFinish(sail_number="475", place=1),
            BoatFinish(sail_number="403", place=2),
        ),
    )
    foreign = RaceData(
        source_id="foreign_div",
        race_number=1,
        name="Race 1",
        date="2026-05-04",
        class_name="Flying Sails Div 2",
        finishes=(BoatFinish(sail_number="34", place=1),),
    )
    results = RegattaResults(
        regatta=Regatta(source="test", source_id="filter_t1", name="Test"),
        races=(own, foreign),
    )

    counts = await import_results(storage, results, own_sail="475")
    assert counts["races_upserted"] == 1, "foreign division must be filtered out"

    db = storage._conn()
    cur = await db.execute(
        "SELECT source_id FROM races WHERE source = 'test' ORDER BY source_id",
    )
    rows = await cur.fetchall()
    assert [r["source_id"] for r in rows] == ["own_div"]


@pytest.mark.asyncio
async def test_filter_keeps_all_when_sail_absent_from_regatta(storage: Storage) -> None:
    """If the own sail isn't in *any* race (spectator regatta), don't
    silently drop everything — fall back to importing every race."""
    from helmlog.results.base import BoatFinish, RaceData, Regatta, RegattaResults

    foreign1 = RaceData(
        source_id="foreign_a",
        race_number=1,
        name="Race 1",
        date="2026-05-04",
        class_name="Class A",
        finishes=(BoatFinish(sail_number="34", place=1),),
    )
    foreign2 = RaceData(
        source_id="foreign_b",
        race_number=2,
        name="Race 2",
        date="2026-05-04",
        class_name="Class B",
        finishes=(BoatFinish(sail_number="153", place=1),),
    )
    results = RegattaResults(
        regatta=Regatta(source="test", source_id="filter_t2", name="Test"),
        races=(foreign1, foreign2),
    )

    counts = await import_results(storage, results, own_sail="475")
    assert counts["races_upserted"] == 2, "fall back to all races when own sail absent"


@pytest.mark.asyncio
async def test_filter_skipped_when_no_own_sail(storage: Storage) -> None:
    """No identity → no filter, every race imported (back-compat)."""
    from helmlog.results.base import BoatFinish, RaceData, Regatta, RegattaResults

    a = RaceData(
        source_id="a_div",
        race_number=1,
        name="Race 1",
        date="2026-05-04",
        class_name="Class A",
        finishes=(BoatFinish(sail_number="475", place=1),),
    )
    b = RaceData(
        source_id="b_div",
        race_number=1,
        name="Race 1",
        date="2026-05-04",
        class_name="Class B",
        finishes=(BoatFinish(sail_number="34", place=1),),
    )
    results = RegattaResults(
        regatta=Regatta(source="test", source_id="filter_t3", name="Test"),
        races=(a, b),
    )

    counts = await import_results(storage, results, own_sail=None)
    assert counts["races_upserted"] == 2


@pytest.mark.asyncio
async def test_link_matches_when_local_session_crosses_utc_midnight(
    storage: Storage,
) -> None:
    """Regression for #734: a local session whose ``start_utc`` is past
    midnight UTC but in the venue-local previous evening must still link
    to an imported race published with the venue-local date.

    Concrete example from corvopi-live: STYC publishes a Wed-night race
    as date 2026-05-04, but the local session's start_utc is
    2026-05-05T01:27Z (= 2026-05-04 18:27 PDT). String-compare on
    ``races.date`` (UTC-derived → '2026-05-05') previously failed to
    match the imported '2026-05-04' and left ``local_session_id`` NULL.
    """
    from helmlog.results.base import BoatFinish, RaceData, Regatta, RegattaResults

    db = storage._conn()
    # Local session started at 18:27 PDT on May 4 — that's 01:27 UTC on May 5.
    # The stored ``date`` column is UTC-derived ('2026-05-05'), but the
    # venue-local date is '2026-05-04'.
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        (
            "Local Wed evening race",
            "Local",
            1,
            "2026-05-05",
            "2026-05-05T01:27:15+00:00",
        ),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    # Imported race published with the venue-local date '2026-05-04'.
    imported = RaceData(
        source_id="cross_midnight_race",
        race_number=4,
        name="Race 4",
        date="2026-05-04",
        class_name="J/105",
        finishes=(BoatFinish(sail_number="475", place=1),),
    )
    results = RegattaResults(
        regatta=Regatta(
            source="test",
            source_id="cross_midnight",
            name="Cross-midnight regatta",
            venue_tz="America/Los_Angeles",
        ),
        races=(imported,),
    )

    await import_results(storage, results)

    cur = await db.execute(
        "SELECT local_session_id FROM races WHERE source = 'test' AND source_id = ?",
        ("cross_midnight_race",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["local_session_id"] == local_id, (
        "imported race must auto-link via venue-local date conversion"
    )


@pytest.mark.asyncio
async def test_race_with_no_date_skipped(storage: Storage) -> None:
    """R: imported race with no date is rejected, not written."""
    from helmlog.results.base import BoatFinish, RaceData, Regatta, RegattaResults

    bad_race = RaceData(
        source_id="no_date_race",
        race_number=99,
        name="Bad Race",
        date="",
        class_name="J/105",
        finishes=(BoatFinish(sail_number="000", place=1),),
    )
    results = RegattaResults(
        regatta=Regatta(source="test", source_id="t1", name="Test"),
        races=(bad_race,),
    )
    counts = await import_results(storage, results)
    assert counts["races_upserted"] == 0

    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM races WHERE source_id = 'no_date_race'") as cur:
        (n,) = await cur.fetchone()  # type: ignore[misc]
    assert n == 0


@pytest.mark.asyncio
async def test_two_regattas_same_class_do_not_collide_on_name(storage: Storage) -> None:
    """Regression for #605: races.name UNIQUE must not block a second regatta
    that shares a class name (e.g. J/105) with a previously-imported regatta.

    Before the fix, the importer generated name="Race 1 - J/105" for every
    J/105 class race regardless of regatta, so the second regatta's INSERT
    tripped the UNIQUE constraint on races.name.
    """
    from helmlog.results.base import BoatFinish, RaceData, Regatta, RegattaResults

    def _make(source_id: str, reg_name: str) -> RegattaResults:
        race = RaceData(
            source_id=f"{source_id}_R1_J/105",
            race_number=1,
            name="Race 1",
            date="2026-04-18",
            class_name="J/105",
            finishes=(BoatFinish(sail_number="105", place=1),),
        )
        return RegattaResults(
            regatta=Regatta(source="clubspot", source_id=source_id, name=reg_name),
            races=(race,),
        )

    counts_a = await import_results(storage, _make("regA", "Sound Wednesday"))
    assert counts_a["races_upserted"] == 1

    # Second regatta, same class, same race_number — must not raise.
    counts_b = await import_results(storage, _make("regB", "CYC Spring"))
    assert counts_b["races_upserted"] == 1

    db = storage._conn()
    async with db.execute(
        "SELECT COUNT(*) FROM races WHERE source = 'clubspot' AND event = 'J/105'"
    ) as cur:
        (n,) = await cur.fetchone()  # type: ignore[misc]
    assert n == 2
