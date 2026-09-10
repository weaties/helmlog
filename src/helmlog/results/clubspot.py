"""Clubspot results provider (#459).

Parses the JSON API at results.theclubspot.com into normalized
``RegattaResults``.  The API requires one ``boatClassIDs`` param per
request, so each call returns results for a single class.  The provider
merges multiple class responses into one ``RegattaResults``.

The provider takes an ``httpx.AsyncClient`` for testability — tests
inject a mock client that returns saved fixture JSON.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any

from loguru import logger

from helmlog.results.base import (
    BoatFinish,
    RaceData,
    Regatta,
    RegattaResults,
    SeriesStanding,
    resolve_venue_tz,
)

if TYPE_CHECKING:
    import httpx

_API_BASE = "https://results.theclubspot.com/clubspot-results-v4"
_LANDING_BASE = "https://www.theclubspot.com/regatta"
_TIMEOUT = 30.0

# Clubspot regatta objectIds are 10-char alphanumeric Parse objectIds.
_OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9]{10}$")
_URL_REGATTA_RE = re.compile(r"/regatta/([A-Za-z0-9]{10})(?:[/?#]|$)")


@dataclass(frozen=True)
class ClubspotClassInfo:
    """One discovered class within a Clubspot regatta."""

    id: str
    name: str


@dataclass(frozen=True)
class ClubspotRegattaInfo:
    """Result of :meth:`ClubspotProvider.discover_regatta`."""

    source_id: str
    name: str
    url: str
    classes: tuple[ClubspotClassInfo, ...] = field(default_factory=tuple)


def parse_regatta_url(url: str) -> str:
    """Extract a Clubspot regatta objectId from a pasted URL or bare id.

    Accepts:
    - ``https://www.theclubspot.com/regatta/<id>/results``
    - club-branded subdomains like ``https://cycseattle.org/regatta/<id>/results``
    - the raw 10-char objectId on its own

    Raises ``ValueError`` if no objectId can be extracted.
    """
    if url is None:
        raise ValueError("Could not parse Clubspot regatta URL: empty")
    s = url.strip()
    if not s:
        raise ValueError("Could not parse Clubspot regatta URL: empty")
    if _OBJECT_ID_RE.match(s):
        return s
    m = _URL_REGATTA_RE.search(s)
    if not m:
        raise ValueError(f"Could not parse Clubspot regatta objectId from {url!r}")
    return m.group(1)


class ClubspotProvider:
    """Fetch race results from Clubspot's JSON API."""

    source_name: str = "clubspot"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ClubspotProvider requires an httpx.AsyncClient")
        return self._client

    async def fetch(self, regatta: Regatta) -> RegattaResults:
        """Fetch all classes for a Clubspot regatta.

        ``regatta.source_id`` is the Clubspot regatta objectId (e.g.
        ``wYFzQvmG4R``).  Class IDs to fetch are stored as a
        comma-separated string in ``regatta.default_class`` (Clubspot
        objectIds, not human names).  If ``default_class`` is empty the
        provider raises ``ValueError``.
        """
        if not regatta.default_class:
            raise ValueError(
                f"Clubspot regatta {regatta.source_id!r} has no class IDs configured "
                f"(set Regatta.default_class to comma-separated Clubspot class objectIds)"
            )

        class_ids = [c.strip() for c in regatta.default_class.split(",") if c.strip()]
        for cid in class_ids:
            if " " in cid or "/" in cid:
                raise ValueError(
                    f"Clubspot class ID {cid!r} looks like a class name, not an objectId. "
                    f"Use the Clubspot objectId (e.g. 7q1o9ikhPH), not 'J/105'."
                )

        venue_tz = resolve_venue_tz(regatta.venue_tz)
        all_races: dict[str, RaceData] = {}
        standings_by_key: dict[tuple[str, str], SeriesStanding] = {}

        for class_id in class_ids:
            payload = await self._fetch_class(regatta.source_id, class_id)
            races, standings = _parse_class_payload(payload, regatta.source_id, venue_tz=venue_tz)
            for r in races:
                key = f"{r.source_id}:{r.class_name}"
                all_races[key] = r
            for s in standings:
                standings_by_key[(s.sail_number, s.class_name)] = s

        return RegattaResults(
            regatta=regatta,
            races=tuple(all_races.values()),
            standings=tuple(standings_by_key.values()),
        )

    async def discover_regatta(self, url: str) -> ClubspotRegattaInfo:
        """Discover a Clubspot regatta's name and classes from a pasted URL.

        Mechanism (reverse-engineered from Clubspot's public SPA):

        1. Extract the regatta objectId from the URL (supports both
           ``theclubspot.com/regatta/<id>`` and club subdomains).
        2. GET the canonical ``www.theclubspot.com/regatta/<id>/results``
           page — Clubspot embeds the full regatta record as an
           HTML-escaped JSON blob in a ``data-regatta="..."`` attribute,
           including a ``boatClassesArray`` of Parse pointers to
           ``boatClasses`` objects.  This gives us the regatta name and
           the list of class objectIds with no auth required.
        3. The inline blob only stores class pointers (objectId without
           name).  To resolve human names we call the public results
           endpoint once per class — the first registration in the
           payload carries ``boatClassObject.name`` (e.g. ``"J/105"``).
           Classes with zero registrations fall back to their objectId
           as the display label.
        """
        source_id = parse_regatta_url(url)
        canonical_url = (
            url.strip()
            if _URL_REGATTA_RE.search(url or "")
            else (f"{_LANDING_BASE}/{source_id}/results")
        )
        landing_url = f"{_LANDING_BASE}/{source_id}/results"

        resp = await self._http().get(landing_url, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        name, class_ids = _extract_regatta_payload(resp.text)

        classes: list[ClubspotClassInfo] = []
        for cid in class_ids:
            try:
                payload = await self._fetch_class(source_id, cid)
            except Exception as exc:  # noqa: BLE001 - tolerant of per-class errors
                logger.warning("Clubspot discover class {} failed: {}", cid, exc)
                classes.append(ClubspotClassInfo(id=cid, name=cid))
                continue
            cname = _class_name_from_payload(payload) or cid
            classes.append(ClubspotClassInfo(id=cid, name=cname))

        return ClubspotRegattaInfo(
            source_id=source_id,
            name=name,
            url=canonical_url,
            classes=tuple(classes),
        )

    async def _fetch_class(self, regatta_id: str, class_id: str) -> dict[str, Any]:
        url = f"{_API_BASE}/{regatta_id}"
        resp = await self._http().get(url, params={"boatClassIDs": class_id}, timeout=_TIMEOUT)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        logger.debug(
            "Clubspot fetch {}/{}: {} scores",
            regatta_id,
            class_id,
            len(data.get("scoresByRegistration", [])),
        )
        return data


def _parse_class_payload(
    payload: dict[str, Any],
    regatta_id: str,
    *,
    venue_tz: tzinfo = UTC,
) -> tuple[list[RaceData], list[SeriesStanding]]:
    """Parse one Clubspot per-class JSON response into normalized types.

    ``venue_tz`` is the regatta's local timezone; race dates are emitted
    in it (#832).
    """
    registrations = payload.get("scoresByRegistration", [])
    if not registrations:
        return [], []

    class_name = (
        registrations[0]
        .get("registrationObject", {})
        .get("boatClassObject", {})
        .get("name", "Unknown")
    )

    race_finishes: dict[int, list[BoatFinish]] = {}
    race_meta: dict[int, dict[str, Any]] = {}
    standings: list[SeriesStanding] = []

    for entry in registrations:
        reg = entry.get("registrationObject", {})
        sail = reg.get("sailNumber", "")
        if not sail:
            continue

        boat_name = reg.get("boatName")
        skipper_parts = [reg.get("firstName", ""), reg.get("lastName", "")]
        skipper = " ".join(p for p in skipper_parts if p).strip() or None
        yacht_club = reg.get("clubName")

        standings.append(
            SeriesStanding(
                sail_number=str(sail),
                class_name=class_name,
                total_points=_to_float(entry.get("total")),
                net_points=_to_float(entry.get("net")),
            )
        )

        for score in entry.get("scoring_data", []):
            race_num = score.get("race_number")
            if race_num is None:
                continue

            start_data = score.get("start_data") or {}
            start_time_str = start_data.get("start_time")
            finish_time_str = score.get("finish_time")

            elapsed_ms = score.get("milliseconds_elapsed")
            elapsed_s = int(elapsed_ms // 1000) if elapsed_ms else None

            corrected_raw = score.get("corrected_time")
            corrected_s: int | None = None
            if isinstance(corrected_raw, (int, float)):
                corrected_s = int(corrected_raw)

            letter = score.get("letterScore")
            status = letter if letter else None

            finish = BoatFinish(
                sail_number=str(sail),
                boat_name=boat_name,
                skipper=skipper,
                yacht_club=yacht_club,
                boat_type=class_name,
                finish_time=finish_time_str,
                start_time=start_time_str,
                elapsed_seconds=elapsed_s,
                corrected_seconds=corrected_s,
                points=_to_float(score.get("points")),
                points_throwout=False,
                status_code=status,
                fleet=class_name,
            )

            race_finishes.setdefault(race_num, []).append(finish)

            if race_num not in race_meta:
                # Prefer finish_time for the race date: Clubspot series
                # reuse a stale start_data.start_time across all races in
                # the series, so start_time often carries the *first*
                # race's date.  finish_time is per-boat and reflects the
                # actual race day.
                finish_date = _extract_date(finish_time_str, venue_tz)
                race_date = finish_date or _extract_date(start_time_str, venue_tz)
                race_meta[race_num] = {
                    "start_time": start_time_str,
                    "date": race_date,
                    "scores_in_start": score.get("scores"),
                    "start_id": start_data.get("start_id", ""),
                    "_has_finish_date": bool(finish_date),
                }
            elif finish_time_str and not race_meta[race_num].get("_has_finish_date"):
                # First boat was DNC (no finish_time) — upgrade the date
                # now that we have a boat with a real finish.
                better = _extract_date(finish_time_str, venue_tz)
                if better:
                    race_meta[race_num]["date"] = better
                    race_meta[race_num]["_has_finish_date"] = True

    races: list[RaceData] = []
    for race_num in sorted(race_finishes):
        meta = race_meta.get(race_num, {})
        finishes = race_finishes[race_num]
        finishes.sort(key=lambda f: (f.points or 999, f.sail_number))

        source_id = f"{regatta_id}_R{race_num}_{class_name}"

        races.append(
            RaceData(
                source_id=source_id,
                race_number=race_num,
                name=f"Race {race_num}",
                date=meta.get("date", ""),
                class_name=class_name,
                finishes=tuple(finishes),
            )
        )

    return races, standings


_DATA_REGATTA_RE = re.compile(r'data-regatta="([^"]*)"')


def _extract_regatta_payload(doc: str) -> tuple[str, list[str]]:
    """Pull the embedded JSON blob out of a Clubspot results landing page.

    Returns ``(regatta_name, [class_object_ids])``.  Raises ``ValueError``
    if the blob is missing or malformed.
    """
    m = _DATA_REGATTA_RE.search(doc)
    if not m:
        raise ValueError("Clubspot landing page missing data-regatta attribute")
    raw = html.unescape(m.group(1))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not decode Clubspot data-regatta JSON: {exc}") from exc
    name = data.get("name") or ""
    classes_raw = data.get("boatClassesArray") or []
    class_ids: list[str] = []
    for entry in classes_raw:
        if isinstance(entry, dict):
            cid = entry.get("objectId")
            if isinstance(cid, str) and cid:
                class_ids.append(cid)
    return str(name), class_ids


def _class_name_from_payload(payload: dict[str, Any]) -> str | None:
    """Return the human class name from a clubspot-results-v4 payload.

    The first registration's ``boatClassObject.name`` is used.  Returns
    ``None`` when the class has no registrations yet.
    """
    regs = payload.get("scoresByRegistration") or []
    for reg in regs:
        bc = (reg or {}).get("registrationObject", {}).get("boatClassObject", {})
        name = bc.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _extract_date(iso_str: str | None, tz: tzinfo = UTC) -> str:
    """Extract the YYYY-MM-DD of an ISO timestamp in *tz*, or ``""``.

    Clubspot timestamps are UTC, so a Wednesday 18:15 PDT start is
    published as Thursday 01:15Z. The importer keys races on the
    venue-local date (that's what the auto-linker compares session start
    times against), so callers pass the regatta's venue tz (#832).
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _to_float(val: str | int | float | None) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
