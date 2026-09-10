"""Results provider protocol and normalized data types (#459).

Each provider (Clubspot, STYC, ...) implements `ResultsProvider` and returns
a `RegattaResults` — a fully normalized snapshot of one regatta. The
importer in `importer.py` is source-agnostic and only consumes these
dataclasses; it never sees raw HTML or JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger


@dataclass(frozen=True)
class Regatta:
    """Admin-saved regatta-of-interest. One row in the `regattas` table."""

    source: str
    source_id: str
    name: str
    url: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    default_class: str | None = None
    venue_tz: str | None = None  # IANA tz name, e.g. "America/Los_Angeles"
    id: int | None = None


@dataclass(frozen=True)
class BoatFinish:
    """One boat's finish in one race.

    All fields except sail_number are optional — providers vary in what
    they publish, and the importer treats missing values as "no data".
    """

    sail_number: str
    place: int | None = None
    boat_name: str | None = None
    skipper: str | None = None
    boat_type: str | None = None
    yacht_club: str | None = None
    phrf_rating: int | None = None
    owner_email: str | None = None
    finish_time: str | None = None
    start_time: str | None = None
    elapsed_seconds: int | None = None
    corrected_seconds: int | None = None
    points: float | None = None
    points_throwout: bool = False
    status_code: str | None = None
    fleet: str | None = None
    division: str | None = None


@dataclass(frozen=True)
class RaceData:
    """One race within a regatta."""

    source_id: str
    race_number: int
    name: str
    date: str  # YYYY-MM-DD in the regatta's local timezone
    class_name: str
    finishes: tuple[BoatFinish, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SeriesStanding:
    """One boat's cumulative standing across a regatta series."""

    sail_number: str
    class_name: str
    total_points: float | None = None
    net_points: float | None = None
    place_in_class: int | None = None
    place_overall: int | None = None


@dataclass(frozen=True)
class RegattaResults:
    """Normalized snapshot of one regatta returned by a provider."""

    regatta: Regatta
    races: tuple[RaceData, ...] = field(default_factory=tuple)
    standings: tuple[SeriesStanding, ...] = field(default_factory=tuple)


@runtime_checkable
class ResultsProvider(Protocol):
    """A results source: Clubspot, STYC, or any future provider."""

    source_name: str

    async def fetch(self, regatta: Regatta) -> RegattaResults:
        """Fetch and normalize results for `regatta`.

        Raises on network errors, parse errors, or timeouts — the importer
        catches and surfaces these to the admin UI without touching the DB.
        """
        ...


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, ResultsProvider] = {}


def register_provider(provider: ResultsProvider) -> None:
    """Register a provider under its `source_name`.

    Overwrites any existing registration for that source — useful for tests
    that swap in a stub provider.
    """
    _PROVIDERS[provider.source_name] = provider


def get_provider(source: str) -> ResultsProvider | None:
    """Return the provider registered for `source`, or None if unknown."""
    return _PROVIDERS.get(source)


def resolve_venue_tz(venue_tz: str | None) -> tzinfo:
    """Resolve a regatta's venue timezone with sensible fallbacks.

    Order: explicit ``Regatta.venue_tz`` → system local tz → UTC. The
    system-local fallback is correct on a Pi located at the venue.
    """
    if venue_tz:
        try:
            return ZoneInfo(venue_tz)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown venue_tz {!r}, falling back", venue_tz)
    local = datetime.now().astimezone().tzinfo
    if local is not None:
        return local
    return ZoneInfo("UTC")
