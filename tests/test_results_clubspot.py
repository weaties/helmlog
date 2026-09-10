"""ClubspotProvider tests against saved fixture (#459, R36)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

from helmlog.results.base import Regatta, RegattaResults
from helmlog.results.clubspot import (
    ClubspotProvider,
    _parse_class_payload,
    parse_regatta_url,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "results" / "clubspot"
_REGATTA_ID = "wYFzQvmG4R"
_J105_CLASS_ID = "7q1o9ikhPH"


@pytest.fixture
def j105_payload() -> dict:
    return json.loads((_FIXTURES / "wYFzQvmG4R_J105.json").read_text())


# ---------------------------------------------------------------------------
# Low-level parser
# ---------------------------------------------------------------------------


def test_parse_j105_races(j105_payload: dict) -> None:
    races, standings = _parse_class_payload(j105_payload, _REGATTA_ID)
    assert len(races) == 2
    assert races[0].race_number == 1
    assert races[1].race_number == 2
    assert all(r.class_name == "J/105" for r in races)


def test_parse_j105_finishes_per_race(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    for r in races:
        assert len(r.finishes) == 15


def test_parse_j105_standings(j105_payload: dict) -> None:
    _, standings = _parse_class_payload(j105_payload, _REGATTA_ID)
    assert len(standings) == 15
    assert all(s.class_name == "J/105" for s in standings)
    totals = [s.total_points for s in standings if s.total_points is not None]
    assert len(totals) == 15


def test_parse_j105_sail_numbers(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    sails = {f.sail_number for r in races for f in r.finishes}
    assert "482" in sails  # Panic


def test_parse_status_codes(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    all_statuses = {f.status_code for r in races for f in r.finishes if f.status_code}
    assert "RET" in all_statuses


def test_parse_points_assigned(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    for r in races:
        for f in r.finishes:
            assert f.points is not None, f"Missing points for {f.sail_number} in {r.name}"


def test_parse_race_date_extracted(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    for r in races:
        assert r.date == "2026-04-09", f"Unexpected date {r.date} for {r.name}"


def test_parse_skipper_names(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    skippers = {f.skipper for r in races for f in r.finishes if f.skipper}
    assert len(skippers) > 0


def test_parse_race_source_ids_unique(j105_payload: dict) -> None:
    races, _ = _parse_class_payload(j105_payload, _REGATTA_ID)
    source_ids = [r.source_id for r in races]
    assert len(source_ids) == len(set(source_ids))


def test_parse_empty_payload() -> None:
    races, standings = _parse_class_payload({"scoresByRegistration": [], "races": []}, "dummy")
    assert races == []
    assert standings == []


# ---------------------------------------------------------------------------
# Provider integration (mock HTTP)
# ---------------------------------------------------------------------------


def _mock_transport(fixture_path: Path) -> httpx.MockTransport:
    payload = fixture_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_provider_fetch_j105() -> None:
    transport = _mock_transport(_FIXTURES / "wYFzQvmG4R_J105.json")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="CYC Sound Wednesday",
            default_class=_J105_CLASS_ID,
        )
        result = await provider.fetch(regatta)

    assert isinstance(result, RegattaResults)
    assert len(result.races) == 2
    assert len(result.standings) == 15
    assert result.regatta is regatta


@pytest.mark.asyncio
async def test_provider_multi_class() -> None:
    """Provider merges multiple class responses."""
    fixtures = {
        "7q1o9ikhPH": _FIXTURES / "wYFzQvmG4R_J105.json",
        "EvS9obW8uC": _FIXTURES / "wYFzQvmG4R_EvS9obW8uC.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        class_id = request.url.params.get("boatClassIDs", "")
        path = fixtures.get(class_id)
        if path:
            return httpx.Response(
                200,
                content=path.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="CYC Sound Wednesday",
            default_class="7q1o9ikhPH,EvS9obW8uC",
        )
        result = await provider.fetch(regatta)

    class_names = {r.class_name for r in result.races}
    assert "J/105" in class_names
    assert "J/80" in class_names
    assert len(result.standings) == 15 + 9


@pytest.mark.asyncio
async def test_provider_no_class_ids_raises() -> None:
    async with httpx.AsyncClient() as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="Test",
            default_class=None,
        )
        with pytest.raises(ValueError, match="no class IDs configured"):
            await provider.fetch(regatta)


# ---------------------------------------------------------------------------
# URL parsing + regatta discovery (#520)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.theclubspot.com/regatta/wYFzQvmG4R/results", "wYFzQvmG4R"),
        ("https://www.theclubspot.com/regatta/wYFzQvmG4R", "wYFzQvmG4R"),
        ("https://www.theclubspot.com/regatta/wYFzQvmG4R/", "wYFzQvmG4R"),
        ("https://cycseattle.org/regatta/wYFzQvmG4R/results", "wYFzQvmG4R"),
        ("http://cycseattle.org/regatta/wYFzQvmG4R/results?foo=bar", "wYFzQvmG4R"),
        ("  https://www.theclubspot.com/regatta/wYFzQvmG4R/results  ", "wYFzQvmG4R"),
        # Bare objectId — accept it too, since users may paste it.
        ("wYFzQvmG4R", "wYFzQvmG4R"),
    ],
)
def test_parse_regatta_url_happy(url: str, expected: str) -> None:
    assert parse_regatta_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/",
        "https://www.theclubspot.com/",
        "https://www.theclubspot.com/club/foo/results",
        "not a url at all",
    ],
)
def test_parse_regatta_url_invalid(url: str) -> None:
    with pytest.raises(ValueError, match="Could not parse"):
        parse_regatta_url(url)


@pytest.mark.asyncio
async def test_discover_regatta() -> None:
    """discover_regatta: parse landing HTML + resolve class names per-class."""
    landing_html = (_FIXTURES / "wYFzQvmG4R_landing.html").read_bytes()
    class_payloads = {
        "7q1o9ikhPH": _FIXTURES / "wYFzQvmG4R_J105.json",
        "EvS9obW8uC": _FIXTURES / "wYFzQvmG4R_EvS9obW8uC.json",
        "DAQPnlrgQX": _FIXTURES / "wYFzQvmG4R_DAQPnlrgQX.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "theclubspot.com" in host and "results" not in host:
            return httpx.Response(
                302,
                headers={"location": "https://cycseattle.org/regatta/wYFzQvmG4R/results"},
            )
        if "cycseattle.org" in host:
            return httpx.Response(
                200,
                content=landing_html,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if host == "results.theclubspot.com":
            class_id = request.url.params.get("boatClassIDs", "")
            path = class_payloads.get(class_id)
            if path:
                return httpx.Response(
                    200,
                    content=path.read_bytes(),
                    headers={"content-type": "application/json"},
                )
            # Unknown class with no registrations — return an empty payload
            return httpx.Response(
                200,
                json={"scoresByRegistration": [], "races": []},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        provider = ClubspotProvider(client=client)
        info = await provider.discover_regatta(
            "https://www.theclubspot.com/regatta/wYFzQvmG4R/results"
        )

    assert info.source_id == "wYFzQvmG4R"
    assert "Sound Wednesday" in info.name
    assert info.url == "https://www.theclubspot.com/regatta/wYFzQvmG4R/results"
    class_ids = [c.id for c in info.classes]
    # All five pointers from the landing data-regatta JSON.
    assert set(class_ids) >= {"7q1o9ikhPH", "EvS9obW8uC", "DAQPnlrgQX"}
    names_by_id = {c.id: c.name for c in info.classes}
    assert names_by_id["7q1o9ikhPH"] == "J/105"
    assert names_by_id["EvS9obW8uC"] == "J/80"
    # Classes with no registrations fall back to the objectId as the label.
    assert names_by_id["OXyZvAjvag"] == "OXyZvAjvag"


@pytest.mark.asyncio
async def test_discover_regatta_accepts_bare_id() -> None:
    landing_html = (_FIXTURES / "wYFzQvmG4R_landing.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "results.theclubspot.com":
            return httpx.Response(
                200,
                json={"scoresByRegistration": [], "races": []},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            content=landing_html,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        provider = ClubspotProvider(client=client)
        info = await provider.discover_regatta("wYFzQvmG4R")

    assert info.source_id == "wYFzQvmG4R"


# ---------------------------------------------------------------------------
# Route: POST /api/results/regattas/discover (#520)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_route(
    monkeypatch: pytest.MonkeyPatch,
    storage: Storage,
) -> None:
    from helmlog.results import clubspot as clubspot_mod
    from helmlog.results.clubspot import ClubspotClassInfo, ClubspotRegattaInfo
    from helmlog.web import create_app

    async def fake_discover(
        self: ClubspotProvider,
        url: str,
    ) -> ClubspotRegattaInfo:
        return ClubspotRegattaInfo(
            source_id="wYFzQvmG4R",
            name="Test Regatta",
            url=url,
            classes=(
                ClubspotClassInfo(id="7q1o9ikhPH", name="J/105"),
                ClubspotClassInfo(id="EvS9obW8uC", name="J/80"),
            ),
        )

    monkeypatch.setattr(clubspot_mod.ClubspotProvider, "discover_regatta", fake_discover)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/results/regattas/discover",
            data={
                "source": "clubspot",
                "url": "https://www.theclubspot.com/regatta/wYFzQvmG4R/results",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source_id"] == "wYFzQvmG4R"
    assert data["name"] == "Test Regatta"
    assert data["url"] == "https://www.theclubspot.com/regatta/wYFzQvmG4R/results"
    assert {c["id"] for c in data["classes"]} == {"7q1o9ikhPH", "EvS9obW8uC"}
    assert {c["name"] for c in data["classes"]} == {"J/105", "J/80"}


# ---------------------------------------------------------------------------
# Venue-local race dates — #832
# ---------------------------------------------------------------------------


def test_extract_date_converts_utc_to_venue_tz() -> None:
    from zoneinfo import ZoneInfo

    from helmlog.results.clubspot import _extract_date

    la = ZoneInfo("America/Los_Angeles")
    # Wed 8 Jul 2026 20:00 PDT finish is published as 9 Jul 03:00Z.
    assert _extract_date("2026-07-09T03:00:37.883Z", la) == "2026-07-08"
    assert _extract_date("2026-07-09T03:00:37.883Z") == "2026-07-09"  # default stays UTC
    assert _extract_date(None, la) == ""
    assert _extract_date("garbage", la) == ""


def test_parse_race_date_is_venue_local(j105_payload: dict) -> None:
    from zoneinfo import ZoneInfo

    races, _ = _parse_class_payload(
        j105_payload, _REGATTA_ID, venue_tz=ZoneInfo("America/Los_Angeles")
    )
    for r in races:
        assert r.date == "2026-04-08", f"Unexpected date {r.date} for {r.name}"


@pytest.mark.asyncio
async def test_provider_fetch_uses_regatta_venue_tz() -> None:
    transport = _mock_transport(_FIXTURES / "wYFzQvmG4R_J105.json")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="CYC Sound Wednesday",
            default_class=_J105_CLASS_ID,
            venue_tz="America/Los_Angeles",
        )
        result = await provider.fetch(regatta)
    assert {r.date for r in result.races} == {"2026-04-08"}
