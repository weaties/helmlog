"""Pluggable visualization framework (#286).

Re-exports the public API for convenience.
"""

from helmlog.visualization.discovery import discover_viz_plugins, get_viz_plugin
from helmlog.visualization.preferences import resolve_viz_preference, set_viz_preference
from helmlog.visualization.protocol import VisualizationPlugin, VizContext, VizPluginMeta

__all__ = [
    "VisualizationPlugin",
    "VizContext",
    "VizPluginMeta",
    "discover_viz_plugins",
    "get_viz_plugin",
    "resolve_viz_preference",
    "set_viz_preference",
]
