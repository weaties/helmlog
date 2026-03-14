"""Plugin discovery and session data loading (#283).

Scans the ``analysis/plugins/`` package for AnalysisPlugin subclasses and
provides a helper to build SessionData from Storage queries.
"""

from __future__ import annotations

import importlib
import pkgutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from helmlog.analysis.protocol import AnalysisPlugin, SessionData

if TYPE_CHECKING:
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Plugin registry (populated lazily on first call)
# ---------------------------------------------------------------------------

_registry: dict[str, AnalysisPlugin] | None = None


def _scan_plugins() -> dict[str, AnalysisPlugin]:
    """Import all modules under ``helmlog.analysis.plugins`` and collect plugins."""
    import helmlog.analysis.plugins as plugins_pkg

    found: dict[str, AnalysisPlugin] = {}
    for _importer, module_name, _is_pkg in pkgutil.iter_modules(
        plugins_pkg.__path__, plugins_pkg.__name__ + "."
    ):
        try:
            mod = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to import analysis plugin module {}", module_name)
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, AnalysisPlugin)
                and obj is not AnalysisPlugin
            ):
                try:
                    instance = obj()
                    meta = instance.meta()
                    found[meta.name] = instance
                    logger.debug("Registered analysis plugin: {}", meta.name)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to instantiate plugin {}", attr_name)

    return found


def discover_plugins(*, force_rescan: bool = False) -> dict[str, AnalysisPlugin]:
    """Return all discovered plugins, keyed by name.

    Results are cached after the first scan.  Pass *force_rescan=True* to
    re-import.
    """
    global _registry  # noqa: PLW0603
    if _registry is None or force_rescan:
        _registry = _scan_plugins()
    return dict(_registry)


def get_plugin(name: str) -> AnalysisPlugin | None:
    """Return a single plugin by name, or None if not found."""
    return discover_plugins().get(name)


# ---------------------------------------------------------------------------
# Session data loader
# ---------------------------------------------------------------------------


async def load_session_data(storage: Storage, session_id: int) -> SessionData | None:
    """Build a SessionData from Storage queries.

    Returns None if the session doesn't exist or hasn't ended.
    """
    db = storage._conn()

    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None or row["end_utc"] is None:
        return None

    start_utc = str(row["start_utc"])
    end_utc = str(row["end_utc"])

    try:
        start = datetime.fromisoformat(start_utc).replace(tzinfo=UTC)
        end = datetime.fromisoformat(end_utc).replace(tzinfo=UTC)
    except ValueError:
        return None

    speeds = await storage.query_range("speeds", start, end, race_id=session_id)
    winds = await storage.query_range("winds", start, end, race_id=session_id)
    headings = await storage.query_range("headings", start, end, race_id=session_id)
    positions = await storage.query_range("positions", start, end, race_id=session_id)

    # Maneuvers
    maneuvers: list[dict[str, Any]] = []
    try:
        mcur = await db.execute(
            "SELECT * FROM maneuvers WHERE race_id = ? ORDER BY ts", (session_id,)
        )
        maneuvers = [dict(r) for r in await mcur.fetchall()]
    except Exception:  # noqa: BLE001
        pass

    # Weather
    weather: list[dict[str, Any]] = []
    try:
        wcur = await db.execute(
            "SELECT * FROM weather WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start_utc, end_utc),
        )
        weather = [dict(r) for r in await wcur.fetchall()]
    except Exception:  # noqa: BLE001
        pass

    # Sail changes
    sail_changes: list[dict[str, Any]] = []
    try:
        scur = await db.execute(
            "SELECT * FROM sail_changes WHERE race_id = ? ORDER BY ts", (session_id,)
        )
        sail_changes = [dict(r) for r in await scur.fetchall()]
    except Exception:  # noqa: BLE001
        pass

    # Boat settings
    boat_settings: list[dict[str, Any]] = []
    try:
        bscur = await db.execute(
            "SELECT * FROM boat_settings WHERE race_id = ? ORDER BY ts", (session_id,)
        )
        boat_settings = [dict(r) for r in await bscur.fetchall()]
    except Exception:  # noqa: BLE001
        pass

    return SessionData(
        session_id=session_id,
        start_utc=start_utc,
        end_utc=end_utc,
        speeds=speeds,
        winds=winds,
        headings=headings,
        positions=positions,
        maneuvers=maneuvers,
        weather=weather,
        sail_changes=sail_changes,
        boat_settings=boat_settings,
    )
