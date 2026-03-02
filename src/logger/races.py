"""Race lifecycle management — naming logic and configuration.

Pure domain logic only. No database access here; storage methods live in
storage.py. This module is importable without hardware or a running server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass
class Race:
    """A single race or practice session window."""

    id: int
    name: str  # e.g. "20250810-BallardCup-2" or "20250810-BallardCup-P1"
    event: str  # e.g. "BallardCup"
    race_num: int
    date: str  # UTC date "YYYY-MM-DD"
    start_utc: datetime
    end_utc: datetime | None
    session_type: str = "race"  # "race" | "practice"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RaceConfig:
    """Web server bind configuration (from environment variables)."""

    web_host: str = field(default_factory=lambda: os.environ.get("WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: int(os.environ.get("WEB_PORT", "3002")))
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", "").rstrip("/"))
    grafana_port: str = field(
        default_factory=lambda: os.environ.get("GRAFANA_PORT", "3001")
    )
    grafana_uid: str = field(
        default_factory=lambda: os.environ.get("GRAFANA_DASHBOARD_UID", "j105-sailing")
    )
    sk_port: str = field(
        default_factory=lambda: os.environ.get("SK_PORT", "3000")
    )


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

_WEEKDAY_EVENTS: dict[int, str] = {
    0: "BallardCup",  # Monday
    2: "CYC",  # Wednesday
}


def default_event_for_date(d: date) -> str | None:
    """Return the default event name for a given UTC date, or None.

    Monday  → "BallardCup"
    Wednesday → "CYC"
    Any other day → None (user must supply the event name)
    """
    return _WEEKDAY_EVENTS.get(d.weekday())


def build_race_name(event: str, d: date, race_num: int, session_type: str = "race") -> str:
    """Build a race identifier string.

    Example: build_race_name("BallardCup", date(2025, 8, 10), 2)
             → "20250810-BallardCup-2"
             build_race_name("BallardCup", date(2025, 8, 10), 1, "practice")
             → "20250810-BallardCup-P1"
    """
    num_str = f"P{race_num}" if session_type == "practice" else str(race_num)
    return f"{d.strftime('%Y%m%d')}-{event}-{num_str}"


def build_grafana_url(
    base_url: str,
    uid: str,
    start_ms: int,
    end_ms: int | None,
    *,
    org_id: int = 1,
) -> str:
    """Build a Grafana deep-link URL for a session.

    For an active session (*end_ms* is ``None``) the URL includes
    ``refresh=10s`` so the dashboard auto-refreshes while the race is live.
    For a closed session (*end_ms* is set) the URL includes ``refresh=``
    (empty string) to disable auto-refresh.

    Example::

        # Closed session
        build_grafana_url("http://host:3001", "j105", 1700000000000, 1700003600000)
        # → "http://host:3001/d/j105/sailing-data?from=1700000000000&to=1700003600000&orgId=1&refresh="

        # Active session
        build_grafana_url("http://host:3001", "j105", 1700000000000, None)
        # → "http://host:3001/d/j105/sailing-data?from=1700000000000&to=now&orgId=1&refresh=10s"
    """
    to = str(end_ms) if end_ms is not None else "now"
    refresh = "" if end_ms is not None else "10s"
    path = f"/d/{uid}/sailing-data?from={start_ms}&to={to}&orgId={org_id}&refresh={refresh}"
    return f"{base_url}{path}"
