"""Yacht Scoring results provider (#833).

yachtscoring.com is a JS single-page app; the HTML is an empty shell.  The
app talks to ``https://api.yachtscoring.com/v1`` and the public results
pages use an unauthenticated ``/v1/public/event/{id}/...`` prefix (the
non-``public`` routes return 401 without a login token).  Endpoints used:

- ``event/{id}``                    — name, dates, venue
- ``event/{id}/splits``             — classes ("splits"): id, class number, name, division
- ``event/{id}/races``              — per-class race schedule (paginated: ``size``/``page``)
- ``event/{id}/boats``              — entry list (yacht club lives here only)
- ``event/{id}/result-detail-report?raceNumber=N`` — one race, every class:
  place, status, finish/elapsed/corrected times
- ``event/{id}/cumulative-result``  — series standings + per-race points

Timestamps in the schedule and detail reports carry a ``Z`` suffix but are
venue wall-clock times (the SPA renders them with ``keepLocalTime``), so
the race date is simply the date part of the string.

Class IDs stored in ``Regatta.default_class`` are Yacht Scoring *split*
ids (e.g. ``2002693``), comma-separated, mirroring the Clubspot provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from helmlog.results.base import (
    BoatFinish,
    RaceData,
    Regatta,
    RegattaResults,
    SeriesStanding,
)

if TYPE_CHECKING:
    import httpx

_API_BASE = "https://api.yachtscoring.com/v1/public/event"
_LANDING_BASE = "https://www.yachtscoring.com/event_results_cumulative"
_TIMEOUT = 30.0
_PAGE_SIZE = 1000
# The API refuses requests without a browser-like User-Agent.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_EVENT_ID_RE = re.compile(r"^\d+$")
# New SPA routes: /emenu/50583, /event_results_cumulative/50583/...
_URL_PATH_RE = re.compile(r"yachtscoring\.com/[a-z_]+/(\d+)(?:[/?#]|$)", re.IGNORECASE)
# Legacy ColdFusion links: ?eID=50583
_URL_EID_RE = re.compile(r"[?&]eID=(\d+)", re.IGNORECASE)

# "AOK" means a normal finish; anything else is a scoring code.
_FINISH_OK = "AOK"

# Per-race points lookup: (class number, eventBoatId, raceNumber) -> points.
PointsIndex = dict[tuple[int, int, int], float]


@dataclass(frozen=True)
class YachtScoringClassInfo:
    """One discovered class (split) within a Yacht Scoring event."""

    id: str
    name: str


@dataclass(frozen=True)
class YachtScoringRegattaInfo:
    """Result of :meth:`YachtScoringProvider.discover_regatta`."""

    source_id: str
    name: str
    url: str
    classes: tuple[YachtScoringClassInfo, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _Split:
    id: int
    class_number: int
    class_name: str
    division: str


@dataclass(frozen=True)
class DetailClass:
    """One class's finishes in one ``result-detail-report``.

    ``boat_ids`` parallels ``finishes`` (Yacht Scoring ``eventBoatId``) so
    the provider can join per-race points and yacht clubs onto each row.
    """

    class_name: str
    finishes: list[BoatFinish]
    boat_ids: list[int | None]


def parse_event_url(url: str) -> str:
    """Extract a Yacht Scoring event id from a pasted URL or bare id.

    Accepts the SPA routes (``/event_results_cumulative/50583``,
    ``/emenu/50583``, ``/event_results_detail/50583/3``), the legacy
    ``?eID=50583`` query form, and a bare numeric id.

    Raises ``ValueError`` if no event id can be extracted.
    """
    s = (url or "").strip()
    if not s:
        raise ValueError("Could not parse Yacht Scoring event URL: empty")
    if _EVENT_ID_RE.match(s):
        return s
    m = _URL_EID_RE.search(s) or _URL_PATH_RE.search(s)
    if not m:
        raise ValueError(f"Could not parse Yacht Scoring event id from {url!r}")
    return m.group(1)


class YachtScoringProvider:
    """Fetch race results from Yacht Scoring's public JSON API."""

    source_name: str = "yachtscoring"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("YachtScoringProvider requires an httpx.AsyncClient")
        return self._client

    async def _get(self, event_id: str, path: str = "", **params: int | str) -> dict[str, Any]:
        url = f"{_API_BASE}/{event_id}{path}"
        resp = await self._http().get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def _get_paged(self, event_id: str, path: str) -> list[dict[str, Any]]:
        """Drain a ``{count, rows}`` paginated endpoint."""
        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            data = await self._get(event_id, path, size=_PAGE_SIZE, page=page)
            batch = data.get("rows") or []
            rows.extend(batch)
            if not batch or len(rows) >= int(data.get("count") or 0):
                return rows
            page += 1

    async def _get_splits(self, event_id: str) -> list[_Split]:
        data = await self._get(event_id, "/splits")
        splits: list[_Split] = []
        for row in data.get("rows") or []:
            try:
                splits.append(
                    _Split(
                        id=int(row["id"]),
                        class_number=int(row["splitClass"]),
                        class_name=str(row.get("splitClassName") or row["splitClass"]),
                        division=str(row.get("splitDivision") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Yacht Scoring split row malformed: {}", row)
        return splits

    async def discover_regatta(self, url: str) -> YachtScoringRegattaInfo:
        """Discover an event's name and classes from a pasted URL."""
        event_id = parse_event_url(url)
        event = await self._get(event_id)
        splits = await self._get_splits(event_id)
        return YachtScoringRegattaInfo(
            source_id=event_id,
            name=str(event.get("name") or f"Yacht Scoring event {event_id}"),
            url=f"{_LANDING_BASE}/{event_id}",
            classes=tuple(YachtScoringClassInfo(id=str(s.id), name=s.class_name) for s in splits),
        )

    async def fetch(self, regatta: Regatta) -> RegattaResults:
        """Fetch every selected class for a Yacht Scoring event.

        ``regatta.source_id`` is the numeric event id; ``default_class``
        holds comma-separated split ids.  Raises ``ValueError`` when no
        class ids are configured or none of them belong to the event.
        """
        if not regatta.default_class:
            raise ValueError(
                f"Yacht Scoring event {regatta.source_id!r} has no class IDs configured "
                f"(set Regatta.default_class to comma-separated Yacht Scoring split ids)"
            )
        wanted_ids = {c.strip() for c in regatta.default_class.split(",") if c.strip()}
        event_id = regatta.source_id

        splits = await self._get_splits(event_id)
        selected = [s for s in splits if str(s.id) in wanted_ids]
        if not selected:
            known = ", ".join(f"{s.id}={s.class_name}" for s in splits)
            raise ValueError(
                f"Class IDs {sorted(wanted_ids)} not a split of Yacht Scoring event "
                f"{event_id} (known: {known})"
            )
        class_numbers = {s.class_number for s in selected}

        event = await self._get(event_id)
        schedule = await self._get(event_id, "/races", size=_PAGE_SIZE, page=0)
        dates = _race_dates_from_schedule(schedule, class_numbers)
        clubs = await self._yacht_clubs(event_id)
        standings, points = _parse_cumulative(
            await self._get(event_id, "/cumulative-result"), class_numbers
        )

        race_numbers = sorted(set(dates) | {k[2] for k in points})
        races: list[RaceData] = []
        for race_number in race_numbers:
            detail = await self._get(event_id, "/result-detail-report", raceNumber=race_number)
            for class_number, cls in _parse_detail_report(detail, class_numbers).items():
                date = dates.get(race_number) or _first_finish_date(cls.finishes)
                finishes = tuple(
                    replace(
                        f,
                        points=points.get((class_number, bid, race_number))
                        if bid is not None
                        else None,
                        yacht_club=clubs.get(bid) if bid is not None else None,
                    )
                    for f, bid in zip(cls.finishes, cls.boat_ids, strict=True)
                )
                races.append(
                    RaceData(
                        source_id=f"{event_id}_R{race_number}_{cls.class_name}",
                        race_number=race_number,
                        name=f"Race {race_number}",
                        date=date,
                        class_name=cls.class_name,
                        finishes=finishes,
                    )
                )

        logger.debug(
            "Yacht Scoring fetch {}: {} races, {} standings", event_id, len(races), len(standings)
        )
        return RegattaResults(
            regatta=replace(
                regatta,
                start_date=regatta.start_date or _date_part(event.get("startDate")),
                end_date=regatta.end_date or _date_part(event.get("endDate")),
            ),
            races=tuple(races),
            standings=tuple(standings),
        )

    async def _yacht_clubs(self, event_id: str) -> dict[int, str]:
        """Map eventBoatId -> yacht club from the entry list (best effort)."""
        try:
            rows = await self._get_paged(event_id, "/boats")
        except Exception as exc:  # noqa: BLE001 - enrichment only
            logger.warning("Yacht Scoring boats list for {} failed: {}", event_id, exc)
            return {}
        clubs: dict[int, str] = {}
        for row in rows:
            club = (row.get("owner") or {}).get("club")
            if isinstance(row.get("id"), int) and isinstance(club, str) and club.strip():
                clubs[row["id"]] = club.strip()
        return clubs


# ---------------------------------------------------------------------------
# Parsers (pure functions over decoded JSON)
# ---------------------------------------------------------------------------


def _race_dates_from_schedule(schedule: dict[str, Any], class_numbers: set[int]) -> dict[int, str]:
    """Map race number -> venue-local YYYY-MM-DD for the selected classes.

    The schedule has one row per (class, race number); when the selected
    classes disagree on a date (rare), the first row wins.
    """
    dates: dict[int, str] = {}
    for row in schedule.get("rows") or []:
        split = row.get("split") or {}
        race_number = _to_int(row.get("raceNumber"))
        if _to_int(split.get("splitClass")) not in class_numbers or race_number is None:
            continue
        date = _date_part(row.get("startTime"))
        if date and race_number not in dates:
            dates[race_number] = date
    return dates


def _iter_classes(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Flatten circle -> division -> class into ``(division_name, class)``."""
    out: list[tuple[str, dict[str, Any]]] = []
    for circle in doc.get("data") or []:
        for division in circle.get("divisions") or []:
            division_name = str(division.get("divisionName") or "")
            for cls in division.get("classes") or []:
                out.append((division_name, cls))
    return out


def _parse_detail_report(doc: dict[str, Any], class_numbers: set[int]) -> dict[int, DetailClass]:
    """Parse one ``result-detail-report`` into finishes per selected class.

    Returns ``{class_number: DetailClass}``; classes absent from the race
    (e.g. a fleet that did not sail that day) are simply missing.  Points
    are not in this report — :func:`_parse_cumulative` supplies them and
    the provider joins the two on ``eventBoatId``.
    """
    out: dict[int, DetailClass] = {}
    for division_name, cls in _iter_classes(doc):
        class_number = _to_int(cls.get("class"))
        if class_number is None or class_number not in class_numbers:
            continue
        class_name = str(cls.get("className") or class_number)
        finishes: list[BoatFinish] = []
        boat_ids: list[int | None] = []
        for boat in cls.get("boats") or []:
            sail = str(boat.get("sailNumber") or "").strip()
            if not sail:
                continue
            boat_ids.append(_to_int(boat.get("eventBoatId")))
            status_raw = str(boat.get("finishStatus") or _FINISH_OK).strip().upper()
            status = None if status_raw == _FINISH_OK else status_raw
            finish_time = _wall_clock(boat.get("finishTime")) if status is None else None
            finishes.append(
                BoatFinish(
                    sail_number=sail,
                    place=_to_int(boat.get("placeClass")),
                    boat_name=_str_or_none(boat.get("name")),
                    skipper=_owner_name(boat.get("owner")),
                    boat_type=_str_or_none(boat.get("design")),
                    finish_time=finish_time,
                    elapsed_seconds=_to_int(boat.get("elapsedTime")),
                    corrected_seconds=_to_int(boat.get("correctedTime")),
                    status_code=status,
                    fleet=class_name,
                    division=division_name or None,
                )
            )
        out[class_number] = DetailClass(class_name, finishes, boat_ids)
    return out


def _parse_cumulative(
    doc: dict[str, Any], class_numbers: set[int]
) -> tuple[list[SeriesStanding], PointsIndex]:
    """Parse ``cumulative-result`` into standings plus a per-race points index.

    ``raceTotal`` is the *net* score (throw-outs already removed); the
    gross total is the sum of every race's ``raceValue``.  Boats are
    published in scored order, which is the place in class.
    """
    standings: list[SeriesStanding] = []
    points: PointsIndex = {}
    for _division, cls in _iter_classes(doc):
        class_number = _to_int(cls.get("class"))
        if class_number is None or class_number not in class_numbers:
            continue
        class_name = str(cls.get("className") or class_number)
        for place, boat in enumerate(cls.get("boats") or [], 1):
            sail = str(boat.get("sailNumber") or "").strip()
            boat_id = _to_int(boat.get("eventBoatId"))
            race_values: list[float] = []
            for rec in boat.get("records") or []:
                race_number = _to_int(rec.get("raceNumber"))
                value = _to_float(rec.get("raceValue"))
                if race_number is None or value is None:
                    continue
                race_values.append(value)
                if boat_id is not None:
                    points[(class_number, boat_id, race_number)] = value
            if not sail:
                continue
            standings.append(
                SeriesStanding(
                    sail_number=sail,
                    class_name=class_name,
                    total_points=sum(race_values) if race_values else None,
                    net_points=_to_float(boat.get("raceTotal")),
                    place_in_class=place,
                )
            )
    return standings, points


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _first_finish_date(finishes: list[BoatFinish]) -> str:
    for f in finishes:
        if f.finish_time:
            return f.finish_time[:10]
    return ""


def _date_part(value: object) -> str:
    """``2026-07-20T11:41:00.000Z`` -> ``2026-07-20`` (wall-clock, no tz math)."""
    if not isinstance(value, str) or len(value) < 10:
        return ""
    return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else ""


def _wall_clock(value: object) -> str | None:
    """Strip the misleading ``.000Z`` suffix: ``2026-07-20T12:58:36``."""
    if not isinstance(value, str) or not _date_part(value):
        return None
    return value[:19]


def _owner_name(owner: object) -> str | None:
    if not isinstance(owner, dict):
        return None
    parts = [str(owner.get("firstName") or "").strip(), str(owner.get("lastName") or "").strip()]
    return " ".join(p for p in parts if p) or None


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
