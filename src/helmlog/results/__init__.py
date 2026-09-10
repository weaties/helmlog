"""External race results import (#459).

Fetches published results from yacht club websites into helmlog so we can
analyze fleet performance across every series and regatta we sail.

This package is source-agnostic: `ClubspotProvider`, `StycProvider`, and
`YachtScoringProvider` each implement the `ResultsProvider` protocol in
`base.py`.  The importer in `importer.py` normalizes provider output into
the `regattas`, `races`, `race_results`, `series_results`, and `boats`
tables (schema v61).
"""

from helmlog.results.base import (
    BoatFinish,
    RaceData,
    Regatta,
    RegattaResults,
    ResultsProvider,
    SeriesStanding,
    get_provider,
    register_provider,
)
from helmlog.results.session_match import (
    SessionMatch,
    SessionMatchOutcome,
    match_race_to_sessions,
)

__all__ = [
    "BoatFinish",
    "RaceData",
    "Regatta",
    "RegattaResults",
    "ResultsProvider",
    "SessionMatch",
    "SessionMatchOutcome",
    "SeriesStanding",
    "get_provider",
    "match_race_to_sessions",
    "register_provider",
]
