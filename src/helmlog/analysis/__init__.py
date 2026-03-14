"""Pluggable analysis framework (#283).

Re-exports the public API for convenience.
"""

from helmlog.analysis.cache import AnalysisCache
from helmlog.analysis.discovery import discover_plugins, get_plugin, load_session_data
from helmlog.analysis.preferences import resolve_preference, set_preference
from helmlog.analysis.protocol import (
    AnalysisContext,
    AnalysisPlugin,
    AnalysisResult,
    Insight,
    Metric,
    PluginMeta,
    SessionData,
    VizData,
)

__all__ = [
    "AnalysisCache",
    "AnalysisContext",
    "AnalysisPlugin",
    "AnalysisResult",
    "Insight",
    "Metric",
    "PluginMeta",
    "SessionData",
    "VizData",
    "discover_plugins",
    "get_plugin",
    "load_session_data",
    "resolve_preference",
    "set_preference",
]
