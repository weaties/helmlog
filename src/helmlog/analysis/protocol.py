"""Analysis framework protocol: ABC and dataclasses (#283).

Defines the contract that every analysis plugin must implement, plus the
structured input/output types used across the framework.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginMeta:
    """Immutable metadata describing an analysis plugin."""

    name: str
    display_name: str
    description: str
    version: str


# ---------------------------------------------------------------------------
# Structured input
# ---------------------------------------------------------------------------


@dataclass
class SessionData:
    """All instrument + context data for one session, pre-loaded for plugins."""

    session_id: int
    start_utc: str
    end_utc: str
    speeds: list[dict[str, Any]] = field(default_factory=list)
    winds: list[dict[str, Any]] = field(default_factory=list)
    headings: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    maneuvers: list[dict[str, Any]] = field(default_factory=list)
    weather: list[dict[str, Any]] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    boat_settings: list[dict[str, Any]] = field(default_factory=list)
    sail_changes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AnalysisContext:
    """Extra context passed to plugins alongside session data."""

    user_id: int
    co_op_id: str | None = None
    is_co_op_data: bool = False


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    """A single named measurement."""

    name: str
    value: float
    unit: str
    label: str = ""


@dataclass(frozen=True)
class Insight:
    """A textual observation from analysis."""

    category: str
    message: str
    severity: str = "info"  # info | warning | critical


@dataclass(frozen=True)
class VizData:
    """Data payload for a single chart/visualization."""

    chart_type: str
    title: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """Structured output from a plugin's analyze() call."""

    plugin_name: str
    plugin_version: str
    session_id: int
    metrics: list[Metric] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    viz: list[VizData] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        d: dict[str, Any] = {
            "plugin_name": self.plugin_name,
            "plugin_version": self.plugin_version,
            "session_id": self.session_id,
            "metrics": [
                {"name": m.name, "value": m.value, "unit": m.unit, "label": m.label}
                for m in self.metrics
            ],
            "insights": [
                {"category": i.category, "message": i.message, "severity": i.severity}
                for i in self.insights
            ],
            "viz": [
                {"chart_type": v.chart_type, "title": v.title, "data": v.data} for v in self.viz
            ],
        }
        if include_raw:
            d["raw"] = self.raw
        return d


# ---------------------------------------------------------------------------
# Plugin ABC
# ---------------------------------------------------------------------------


class AnalysisPlugin(ABC):
    """Abstract base class for analysis plugins."""

    @abstractmethod
    def meta(self) -> PluginMeta:
        """Return plugin metadata."""

    @abstractmethod
    async def analyze(self, data: SessionData, ctx: AnalysisContext) -> AnalysisResult:
        """Run analysis on a session and return structured results."""
