"""Polar scatter visualization plugin (#286).

Renders a Plotly scatterpolar chart from polar_baseline analysis results,
showing session boat speed vs true wind angle.
"""

from __future__ import annotations

from typing import Any

from helmlog.visualization.protocol import VisualizationPlugin, VizContext, VizPluginMeta


class PolarScatterPlugin(VisualizationPlugin):
    """Plotly scatter polar from polar_baseline analysis."""

    def meta(self) -> VizPluginMeta:
        return VizPluginMeta(
            name="polar_scatter",
            display_name="Polar Scatter",
            description="Scatter polar chart of boat speed vs true wind angle.",
            version="1.0.0",
            required_analysis=["polar_baseline"],
        )

    async def render(
        self,
        session_data: dict[str, Any],
        analysis_results: dict[str, Any],
        ctx: VizContext,
    ) -> dict[str, Any]:
        cells: list[dict[str, Any]] = []
        raw = analysis_results.get("raw", {})
        if isinstance(raw, dict):
            cells = raw.get("cells", [])

        # Also check viz data for cells
        if not cells:
            for v in analysis_results.get("viz", []):
                if v.get("chart_type") == "polar":
                    cells = v.get("data", {}).get("cells", [])
                    break

        theta: list[float] = []
        r: list[float] = []
        text: list[str] = []

        for cell in cells:
            twa_bin = cell.get("twa_bin", 0)
            bsp = cell.get("session_mean_bsp", 0)
            n = cell.get("sample_count", 0)
            theta.append(float(twa_bin))
            r.append(float(bsp))
            text.append(f"TWA: {twa_bin}, BSP: {bsp:.2f} kts (n={n})")

        return {
            "data": [
                {
                    "type": "scatterpolar",
                    "r": r,
                    "theta": theta,
                    "mode": "markers",
                    "marker": {"size": 8, "color": r, "colorscale": "Viridis"},
                    "text": text,
                    "hoverinfo": "text",
                }
            ],
            "layout": {
                "title": "Session Polar Scatter",
                "polar": {
                    "radialaxis": {"title": "BSP (kts)", "visible": True},
                    "angularaxis": {"direction": "clockwise"},
                },
                "showlegend": False,
            },
        }
