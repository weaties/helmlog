"""Visualization plugin discovery (#286).

Scans the ``visualization/plugins/`` package for VisualizationPlugin
subclasses, mirroring the analysis discovery pattern.
"""

from __future__ import annotations

import importlib
import pkgutil

from loguru import logger

from helmlog.visualization.protocol import VisualizationPlugin

# ---------------------------------------------------------------------------
# Plugin registry (populated lazily on first call)
# ---------------------------------------------------------------------------

_registry: dict[str, VisualizationPlugin] | None = None


def _scan_plugins() -> dict[str, VisualizationPlugin]:
    """Import all modules under ``helmlog.visualization.plugins`` and collect plugins."""
    import helmlog.visualization.plugins as plugins_pkg

    found: dict[str, VisualizationPlugin] = {}
    for _importer, module_name, _is_pkg in pkgutil.iter_modules(
        plugins_pkg.__path__, plugins_pkg.__name__ + "."
    ):
        try:
            mod = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to import visualization plugin module {}", module_name)
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, VisualizationPlugin)
                and obj is not VisualizationPlugin
            ):
                try:
                    instance = obj()
                    meta = instance.meta()
                    found[meta.name] = instance
                    logger.debug("Registered visualization plugin: {}", meta.name)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to instantiate viz plugin {}", attr_name)

    return found


def discover_viz_plugins(*, force_rescan: bool = False) -> dict[str, VisualizationPlugin]:
    """Return all discovered visualization plugins, keyed by name.

    Results are cached after the first scan.  Pass *force_rescan=True* to
    re-import.
    """
    global _registry  # noqa: PLW0603
    if _registry is None or force_rescan:
        _registry = _scan_plugins()
    return dict(_registry)


def get_viz_plugin(name: str) -> VisualizationPlugin | None:
    """Return a single visualization plugin by name, or None if not found."""
    return discover_viz_plugins().get(name)
