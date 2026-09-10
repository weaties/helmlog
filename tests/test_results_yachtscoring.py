"""YachtScoringProvider tests against saved public-API fixtures (#833).

Fixtures are real responses from ``api.yachtscoring.com/v1/public/event/50583``
(Race Week 2026), trimmed to the One Design division (J105 + J70) except
``detail_1`` which keeps every class so class filtering is exercised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

from helmlog.results.base import Regatta, RegattaResults
from helmlog.results.yachtscoring import (
    YachtScoringProvider,
    _parse_cumulative,
    _parse_detail_report,
    _race_dates_from_schedule,
    parse_event_url,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "results" / "yachtscoring"
_EVENT_ID = "50583"
_J105_SPLIT = "2002693"
_J70_SPLIT = "2002694"
_J105_CLASS = 3
_J70_CLASS = 4


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_FIXTURES / name).read_text())
    return data


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.yachtscoring.com/event_results_cumulative/50583", "50583"),
        ("https://www.yachtscoring.com/event_results_cumulative/50583?mode=overall", "50583"),
        ("https://www.yachtscoring.com/emenu/50583", "50583"),
        ("https://www.yachtscoring.com/event_results_detail/50583/3", "50583"),
        ("https://yachtscoring.com/emenu.cfm?eID=50583", "50583"),
        ("https://www.yachtscoring.com/event_results_cumulative.cfm?eID=50583", "50583"),
        ("  https://www.yachtscoring.com/emenu/50583/  ", "50583"),
        ("50583", "50583"),
    ],
)
def test_parse_event_url_happy(url: str, expected: str) -> None:
    assert parse_event_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/",
        "https://www.yachtscoring.com/",
        "https://www.yachtscoring.com/about",
        "not a url at all",
    ],
)
def test_parse_event_url_invalid(url: str) -> None:
    with pytest.raises(ValueError, match="Could not parse"):
        parse_event_url(url)


# ---------------------------------------------------------------------------
# Low-level parsers
# ---------------------------------------------------------------------------


def test_race_dates_from_schedule_are_venue_local() -> None:
    dates = _race_dates_from_schedule(_load("50583_races.json"), {_J105_CLASS})
    assert dates[1] == "2026-07-20"
    assert dates[2] == "2026-07-20"
    assert dates[3] == "2026-07-21"
    assert dates[6] == "2026-07-22"
    assert dates[9] == "2026-07-23"
    assert dates[12] == "2026-07-24"
    # The J/70 fleet sailed a 13th race; J/105 did not.
    assert 13 not in dates
    assert len(dates) == 12


def test_detail_report_filters_to_requested_class() -> None:
    doc = _load("50583_detail_1.json")
    by_class = _parse_detail_report(doc, {_J105_CLASS})
    assert set(by_class) == {_J105_CLASS}
    cls = by_class[_J105_CLASS]
    assert cls.class_name == "J105"
    assert len(cls.finishes) == 11
    corvo_idx = next(i for i, f in enumerate(cls.finishes) if f.sail_number == "475")
    assert cls.boat_ids[corvo_idx] == 521548


def test_detail_report_corvo_race1() -> None:
    doc = _load("50583_detail_1.json")
    finishes = _parse_detail_report(doc, {_J105_CLASS})[_J105_CLASS].finishes
    corvo = next(f for f in finishes if f.sail_number == "475")
    assert corvo.boat_name == "Corvo 105"
    assert corvo.skipper == "Dan Weatbrook"
    assert corvo.boat_type == "J/105"
    assert corvo.place == 5
    assert corvo.status_code is None
    assert corvo.elapsed_seconds == 4656
    assert corvo.corrected_seconds == 4656
    # Wall-clock venue time — the API's "Z" suffix is not really UTC.
    assert corvo.finish_time == "2026-07-20T12:58:36"
    assert corvo.fleet == "J105"
    assert corvo.division == "One Design"


def test_detail_report_status_codes() -> None:
    doc = _load("50583_detail_1.json")
    finishes = _parse_detail_report(doc, {_J105_CLASS})[_J105_CLASS].finishes
    insub = next(f for f in finishes if f.sail_number == "212")
    assert insub.status_code == "DNS"
    assert insub.finish_time is None
    assert insub.elapsed_seconds is None
    assert insub.place == 12


def test_detail_report_multi_class() -> None:
    doc = _load("50583_detail_1.json")
    by_class = _parse_detail_report(doc, {_J105_CLASS, _J70_CLASS})
    assert by_class[_J70_CLASS].class_name == "J70"
    assert len(by_class[_J70_CLASS].finishes) == 4


def test_detail_report_missing_class_is_absent() -> None:
    """Race 13 was J/70-only: J105 must not appear."""
    by_class = _parse_detail_report(_load("50583_detail_13.json"), {_J105_CLASS, _J70_CLASS})
    assert set(by_class) == {_J70_CLASS}


def test_cumulative_standings_and_points() -> None:
    standings, points = _parse_cumulative(_load("50583_cumulative.json"), {_J105_CLASS})
    assert len(standings) == 11
    assert all(s.class_name == "J105" for s in standings)
    corvo = next(s for s in standings if s.sail_number == "475")
    assert corvo.place_in_class == 6
    assert corvo.net_points == 67.0
    assert corvo.total_points == 79.0
    winner = next(s for s in standings if s.place_in_class == 1)
    assert winner.sail_number == "89"
    # Per-race points are keyed by (class, eventBoatId, raceNumber).
    assert points[(_J105_CLASS, 521548, 11)] == 1.0
    assert points[(_J105_CLASS, 521548, 9)] == 12.0


# ---------------------------------------------------------------------------
# Provider integration (mock HTTP)
# ---------------------------------------------------------------------------


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "api.yachtscoring.com"
    assert request.headers.get("user-agent", "").startswith("Mozilla/5.0")
    path = request.url.path
    prefix = f"/v1/public/event/{_EVENT_ID}"
    assert path.startswith(prefix), path
    tail = path[len(prefix) :]
    match tail:
        case "":
            name = "50583_event.json"
        case "/splits":
            name = "50583_splits.json"
        case "/races":
            assert request.url.params.get("size") == "1000"
            assert request.url.params.get("page") == "0"
            name = "50583_races.json"
        case "/boats":
            name = "50583_boats.json"
        case "/cumulative-result":
            name = "50583_cumulative.json"
        case "/result-detail-report":
            name = f"50583_detail_{request.url.params['raceNumber']}.json"
        case _:
            return httpx.Response(404)
    return httpx.Response(
        200,
        content=(_FIXTURES / name).read_bytes(),
        headers={"content-type": "application/json"},
    )


def _regatta(default_class: str | None = _J105_SPLIT) -> Regatta:
    return Regatta(
        source="yachtscoring",
        source_id=_EVENT_ID,
        name="2026 Race Week Bellingham",
        default_class=default_class,
    )


@pytest.mark.asyncio
async def test_provider_fetch_j105() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = YachtScoringProvider(client=client)
        result = await provider.fetch(_regatta())

    assert isinstance(result, RegattaResults)
    assert len(result.races) == 12
    assert [r.race_number for r in result.races] == list(range(1, 13))
    assert all(r.class_name == "J105" for r in result.races)
    assert all(len(r.finishes) == 11 for r in result.races)
    assert len(result.standings) == 11
    assert result.regatta.source_id == _EVENT_ID
    assert result.regatta.start_date == "2026-07-20"
    assert result.regatta.end_date == "2026-07-24"
    assert result.regatta.default_class == _J105_SPLIT
    source_ids = [r.source_id for r in result.races]
    assert len(set(source_ids)) == 12


@pytest.mark.asyncio
async def test_provider_corvo_finishes() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await YachtScoringProvider(client=client).fetch(_regatta())

    by_race = {r.race_number: r for r in result.races}
    corvo = {n: next(f for f in r.finishes if f.sail_number == "475") for n, r in by_race.items()}
    assert {n: f.place for n, f in corvo.items()} == {
        1: 5,
        2: 3,
        3: 4,
        4: 7,
        5: 7,
        6: 6,
        7: 12,
        8: 12,
        9: 12,
        10: 6,
        11: 1,
        12: 4,
    }
    assert {n: f.status_code for n, f in corvo.items() if f.status_code} == {
        7: "DNS",
        8: "RET",
        9: "RET",
    }
    assert {n: f.points for n, f in corvo.items()} == {
        1: 5.0,
        2: 3.0,
        3: 4.0,
        4: 7.0,
        5: 7.0,
        6: 6.0,
        7: 12.0,
        8: 12.0,
        9: 12.0,
        10: 6.0,
        11: 1.0,
        12: 4.0,
    }
    assert by_race[1].date == "2026-07-20"
    assert by_race[12].date == "2026-07-24"
    # Yacht club comes from the boats endpoint.
    assert corvo[1].yacht_club == "CYC Seattle"


@pytest.mark.asyncio
async def test_provider_multi_class() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await YachtScoringProvider(client=client).fetch(
            _regatta(f"{_J105_SPLIT},{_J70_SPLIT}")
        )

    class_names = {r.class_name for r in result.races}
    assert class_names == {"J105", "J70"}
    j70 = [r for r in result.races if r.class_name == "J70"]
    assert len(j70) == 13
    assert len([r for r in result.races if r.class_name == "J105"]) == 12
    assert len(result.standings) == 11 + 4


@pytest.mark.asyncio
async def test_provider_no_class_ids_raises() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(ValueError, match="no class IDs configured"):
            await YachtScoringProvider(client=client).fetch(_regatta(None))


@pytest.mark.asyncio
async def test_provider_unknown_class_id_raises() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(ValueError, match="not a split"):
            await YachtScoringProvider(client=client).fetch(_regatta("999"))


@pytest.mark.asyncio
async def test_discover_regatta() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        info = await YachtScoringProvider(client=client).discover_regatta(
            "https://www.yachtscoring.com/event_results_cumulative/50583"
        )

    assert info.source_id == _EVENT_ID
    assert info.name == "2026 Race Week Bellingham"
    assert info.url == "https://www.yachtscoring.com/event_results_cumulative/50583"
    names_by_id = {c.id: c.name for c in info.classes}
    assert names_by_id[_J105_SPLIT] == "J105"
    assert names_by_id[_J70_SPLIT] == "J70"
    assert len(info.classes) == 9


# ---------------------------------------------------------------------------
# Route wiring
# ---------------------------------------------------------------------------


def test_detect_source() -> None:
    from helmlog.routes.results import _detect_source

    assert _detect_source("https://www.yachtscoring.com/emenu/50583") == "yachtscoring"
    assert _detect_source("https://YachtScoring.com/event_results_cumulative/50583") == (
        "yachtscoring"
    )


@pytest.mark.asyncio
async def test_discover_route(monkeypatch: pytest.MonkeyPatch, storage: Storage) -> None:
    from helmlog.results import yachtscoring as ys_mod
    from helmlog.results.yachtscoring import YachtScoringClassInfo, YachtScoringRegattaInfo
    from helmlog.web import create_app

    async def fake_discover(self: YachtScoringProvider, url: str) -> YachtScoringRegattaInfo:
        return YachtScoringRegattaInfo(
            source_id=_EVENT_ID,
            name="2026 Race Week Bellingham",
            url=url,
            classes=(YachtScoringClassInfo(id=_J105_SPLIT, name="J105"),),
        )

    monkeypatch.setattr(ys_mod.YachtScoringProvider, "discover_regatta", fake_discover)
    monkeypatch.setenv("AUTH_DISABLED", "true")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Source auto-detected from the URL.
        resp = await client.post(
            "/api/results/regattas/discover",
            data={"url": "https://www.yachtscoring.com/event_results_cumulative/50583"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source"] == "yachtscoring"
    assert data["source_id"] == _EVENT_ID
    assert data["classes"] == [{"id": _J105_SPLIT, "name": "J105"}]


@pytest.mark.asyncio
async def test_add_and_fetch_route(monkeypatch: pytest.MonkeyPatch, storage: Storage) -> None:
    """End-to-end: add a yachtscoring regatta, fetch via the route, rows land in SQLite."""
    from helmlog.web import create_app

    monkeypatch.setenv("AUTH_DISABLED", "true")
    # The fetch route builds its own ``httpx.AsyncClient()``; point it at
    # the fixture transport while keeping the real class for the ASGI client.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(_handler)),
    )

    app = create_app(storage)
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/results/regattas",
            data={
                "source": "yachtscoring",
                "source_id": _EVENT_ID,
                "name": "2026 Race Week Bellingham",
                "url": "https://www.yachtscoring.com/event_results_cumulative/50583",
                "default_class": _J105_SPLIT,
            },
        )
        assert resp.status_code == 200, resp.text
        regatta_id = resp.json()["id"]

        resp = await client.post(f"/api/results/regattas/{regatta_id}/fetch")
        assert resp.status_code == 200, resp.text
        body = resp.json()

    assert body["races_upserted"] == 12
    assert body["results_upserted"] == 12 * 11
    assert body["standings_upserted"] == 11

    db = storage._conn()
    cur = await db.execute(
        "SELECT rr.place, rr.points, rr.status_code, r.date FROM race_results rr"
        " JOIN races r ON r.id = rr.race_id JOIN boats b ON b.id = rr.boat_id"
        " WHERE b.sail_number = '475' AND r.race_num = 11"
    )
    row = await cur.fetchone()
    assert row is not None
    assert tuple(row) == (1, 1.0, None, "2026-07-24")
