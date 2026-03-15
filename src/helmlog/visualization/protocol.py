"""Visualization framework protocol: ABC and dataclasses (#286).

Defines the contract that every visualization plugin must implement.
Visualization plugins render analysis results as Plotly JSON specs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VizPluginMeta:
    """Immutable metadata describing a visualization plugin."""

    name: str
    display_name: str
    description: str
    version: str
    required_analysis: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Render context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VizContext:
    """Context passed to visualization plugins alongside data."""

    user_id: int
    co_op_id: str | None = None
    is_co_op_data: bool = False


# ---------------------------------------------------------------------------
# Plugin ABC
# ---------------------------------------------------------------------------


class VisualizationPlugin(ABC):
    """Abstract base class for visualization plugins."""

    @abstractmethod
    def meta(self) -> VizPluginMeta:
        """Return plugin metadata."""

    @abstractmethod
    async def render(
        self,
        session_data: dict[str, Any],
        analysis_results: dict[str, Any],
        ctx: VizContext,
    ) -> dict[str, Any]:
        """Render a Plotly JSON spec from session data and analysis results.

        Returns a dict conforming to the Plotly JSON chart schema with
        at minimum ``data`` and ``layout`` keys.
        """
