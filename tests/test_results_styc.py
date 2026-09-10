"""StycProvider tests against saved fixtures (#459, R36)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.results.base import Regatta, RegattaResults
from helmlog.results.styc import (
    StycProvider,
    _discover_race_numbers,
    _extract_race_date,
    _extract_status,
    _header_map,
    _parse_race_page,
    _parse_series,
    _time_to_seconds,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage

_FIXTURES = Path(__file__).parent / "fixtures" / "results" / "styc"
_SOURCE_ID = "Ballard_Cup_SeriesI_2025"


@pytest.fixture
def series_html() -> str:
    return (_FIXTURES / "series.htm").read_text()


@pytest.fixture
def race1_html() -> str:
    return (_FIXTURES / "race1.htm").read_text()


# ---------------------------------------------------------------------------
# Series parser
# ---------------------------------------------------------------------------


def test_parse_series_standings(series_html: str) -> None:
    standings = _parse_series(series_html, _SOURCE_ID)
    assert len(standings) > 0
    classes = {s.class_name for s in standings}
    assert any("Non-Flying" in c for c in classes)
    assert any("Multihull" in c for c in classes)


def test_parse_series_boat_fields(series_html: str) -> None:
    standings = _parse_series(series_html, _SOURCE_ID)
    golux = [s for s in standings if s.sail_number == "USA 5001"]
    assert len(golux) == 1
    assert golux[0].place_in_class == 2
    assert golux[0].total_points is not None
    assert golux[0].place_overall is not None


def test_discover_race_numbers(series_html: str) -> None:
    nums = _discover_race_numbers(series_html)
    assert nums == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# Race page parser
# ---------------------------------------------------------------------------


def test_parse_race_page_classes(race1_html: str) -> None:
    races = _parse_race_page(race1_html, _SOURCE_ID, 1)
    assert len(races) == 9
    class_names = {r.class_name for r in races}
    assert any("Non-Flying" in c for c in class_names)


def test_parse_race_date(race1_html: str) -> None:
    date = _extract_race_date(race1_html)
    assert date == "2025-04-14"


def test_parse_race_finishes(race1_html: str) -> None:
    races = _parse_race_page(race1_html, _SOURCE_ID, 1)
    class1 = [r for r in races if "Non-Flying" in r.class_name][0]
    assert len(class1.finishes) > 0
    golux = [f for f in class1.finishes if f.sail_number == "USA 5001"]
    assert len(golux) == 1
    assert golux[0].place == 1
    assert golux[0].elapsed_seconds is not None
    assert golux[0].start_time is not None


def test_parse_dnc_finish(race1_html: str) -> None:
    races = _parse_race_page(race1_html, _SOURCE_ID, 1)
    class1 = [r for r in races if "Non-Flying" in r.class_name][0]
    francy = [f for f in class1.finishes if f.sail_number == "42520"]
    assert len(francy) == 1
    assert francy[0].status_code == "DNC"
    assert francy[0].finish_time is None


def test_parse_race_source_ids_unique(race1_html: str) -> None:
    races = _parse_race_page(race1_html, _SOURCE_ID, 1)
    ids = [r.source_id for r in races]
    assert len(ids) == len(set(ids))


def test_race_date_has_correct_format() -> None:
    races = _parse_race_page((_FIXTURES / "race3.htm").read_text(), _SOURCE_ID, 3)
    for r in races:
        assert r.date, f"Missing date on {r.source_id}"
        assert len(r.date) == 10  # YYYY-MM-DD


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_extract_status_codes() -> None:
    assert _extract_status("DNC") == "DNC"
    assert _extract_status("5 DNF") == "DNF"
    assert _extract_status("RET") == "RET"
    assert _extract_status("19:05:39") is None
    assert _extract_status("") is None


def test_time_to_seconds() -> None:
    assert _time_to_seconds("0:50:39") == 3039
    assert _time_to_seconds("1:14:59") == 4499
    assert _time_to_seconds("") is None
    assert _time_to_seconds("DNC") is None


# ---------------------------------------------------------------------------
# Provider integration (mock HTTP)
# ---------------------------------------------------------------------------


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        if path.endswith("series.htm"):
            content = (_FIXTURES / "series.htm").read_bytes()
        else:
            match = __import__("re").search(r"race(\d+)\.htm", path)
            if match:
                fname = f"race{match.group(1)}.htm"
                fpath = _FIXTURES / fname
                if fpath.exists():
                    content = fpath.read_bytes()
                else:
                    return httpx.Response(404)
            else:
                return httpx.Response(404)
        return httpx.Response(200, content=content, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_provider_fetch_full() -> None:
    transport = _mock_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        provider = StycProvider(client=client)
        regatta = Regatta(
            source="styc",
            source_id=_SOURCE_ID,
            name="Ballard Cup Series I 2025",
            url="https://race.styc.org/race_info/Ballard_Cup/SeriesI/2025/",
        )
        result = await provider.fetch(regatta)

    assert isinstance(result, RegattaResults)
    assert len(result.races) > 0
    assert len(result.standings) > 0
    assert len(result.races) == 9 * 6  # 9 classes × 6 races


@pytest.mark.asyncio
async def test_provider_no_url_raises() -> None:
    async with httpx.AsyncClient() as client:
        provider = StycProvider(client=client)
        regatta = Regatta(source="styc", source_id="test", name="Test")
        with pytest.raises(ValueError, match="no URL configured"):
            await provider.fetch(regatta)


# ---------------------------------------------------------------------------
# Importer integration (R37, R38)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_styc_import_end_to_end(storage: Storage) -> None:
    """Full pipeline: StycProvider → importer → DB."""
    from helmlog.results.importer import import_results

    transport = _mock_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        provider = StycProvider(client=client)
        regatta = Regatta(
            source="styc",
            source_id=_SOURCE_ID,
            name="Ballard Cup Series I 2025",
            url="https://race.styc.org/race_info/Ballard_Cup/SeriesI/2025/",
        )
        result = await provider.fetch(regatta)

    counts = await import_results(storage, result)
    assert counts["races_upserted"] > 0
    assert counts["results_upserted"] > 0
    assert counts["standings_upserted"] > 0

    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM regattas WHERE source = 'styc'") as cur:
        (n,) = await cur.fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.asyncio
async def test_styc_reimport_idempotent(storage: Storage) -> None:
    """R38: second import produces zero net row count changes."""
    from helmlog.results.importer import import_results

    transport = _mock_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        provider = StycProvider(client=client)
        regatta = Regatta(
            source="styc",
            source_id=_SOURCE_ID,
            name="Ballard Cup Series I 2025",
            url="https://race.styc.org/race_info/Ballard_Cup/SeriesI/2025/",
        )
        result = await provider.fetch(regatta)

    await import_results(storage, result)

    db = storage._conn()

    async def _counts() -> dict[str, int]:
        totals = {}
        for table in ("regattas", "races", "boats", "race_results", "series_results"):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
                (n,) = await cur.fetchone()  # type: ignore[misc]
            totals[table] = n
        return totals

    before = await _counts()
    await import_results(storage, result)
    after = await _counts()
    assert before == after, f"Row counts changed: {before} vs {after}"


# ---------------------------------------------------------------------------
# Multi-day distance races (Race to the Straits) — #831
# ---------------------------------------------------------------------------


@pytest.fixture
def rtts_race1_html() -> str:
    return (_FIXTURES / "rtts_race1.htm").read_text()


def test_extract_race_date_multi_day_ampersand() -> None:
    html = "<p class=racedate>May 2 &amp; 3, 2026</p>"
    assert _extract_race_date(html) == "2026-05-02"


def test_extract_race_date_multi_day_dash() -> None:
    assert _extract_race_date("<p class=racedate>May 2-3, 2026</p>") == "2026-05-02"
    assert _extract_race_date("<p class=racedate>May 2 - 3, 2026</p>") == "2026-05-02"


def test_header_map_day_prefixed_columns() -> None:
    col = _header_map(
        [
            "boat name",
            "sailno",
            "saturday start",
            "saturday finish",
            "saturday corrected time",
            "place",
        ]
    )
    assert col["start"] == 2
    assert col["finish"] == 3
    assert col["corrected"] == 4
    assert col["place"] == 5


def test_header_map_bare_column_wins_over_day_prefixed() -> None:
    col = _header_map(["sailno", "saturday finish", "finish"])
    assert col["finish"] == 2


def test_parse_rtts_race_page(rtts_race1_html: str) -> None:
    races = _parse_race_page(rtts_race1_html, "RaceToTheStraits_2026", 1)
    assert len(races) == 13
    for r in races:
        assert r.date == "2026-05-02", f"Missing/incorrect date on {r.source_id}"
    corvo = [f for r in races for f in r.finishes if f.sail_number == "475"]
    assert len(corvo) == 1
    assert corvo[0].place == 5
    assert corvo[0].finish_time == "15:56:05"
    assert corvo[0].status_code is None
    assert corvo[0].corrected_seconds == 5 * 3600 + 50 * 60 + 54
