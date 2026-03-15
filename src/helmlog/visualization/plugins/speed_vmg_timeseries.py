"""Speed/VMG time series visualization plugin (#286).

Renders a Plotly line chart of boat speed and VMG over time from raw
session instrument data.
"""

from __future__ import annotations

import math
from typing import Any

from helmlog.visualization.protocol import VisualizationPlugin, VizContext, VizPluginMeta


class SpeedVMGTimeseriesPlugin(VisualizationPlugin):
    """Plotly line chart of speed and VMG over time."""

    def meta(self) -> VizPluginMeta:
        return VizPluginMeta(
            name="speed_vmg_timeseries",
            display_name="Speed & VMG Time Series",
            description="Line chart of boat speed and VMG over the session.",
            version="1.0.0",
            required_analysis=[],
        )

    async def render(
        self,
        session_data: dict[str, Any],
        analysis_results: dict[str, Any],
        ctx: VizContext,
    ) -> dict[str, Any]:
        speeds = session_data.get("speeds", [])
        winds = session_data.get("winds", [])

        # Index wind data by truncated-second key
        wind_by_ts: dict[str, dict[str, Any]] = {}
        for w in winds:
            wind_by_ts.setdefault(str(w.get("ts", ""))[:19], w)

        time_vals: list[str] = []
        speed_vals: list[float] = []
        vmg_vals: list[float | None] = []

        for s in speeds:
            ts = str(s.get("ts", ""))
            bsp = float(s.get("speed_kts", 0))
            time_vals.append(ts)
            speed_vals.append(bsp)

            # Compute VMG if wind data available
            wind_row = wind_by_ts.get(ts[:19])
            if wind_row and bsp > 0:
                twa = float(wind_row.get("wind_angle_deg", 0))
                vmg = bsp * math.cos(math.radians(twa))
                vmg_vals.append(round(vmg, 3))
            else:
                vmg_vals.append(None)

        traces: list[dict[str, Any]] = [
            {
                "type": "scatter",
                "x": time_vals,
                "y": speed_vals,
                "mode": "lines",
                "name": "BSP (kts)",
                "line": {"color": "#1f77b4"},
            },
        ]

        if any(v is not None for v in vmg_vals):
            traces.append(
                {
                    "type": "scatter",
                    "x": time_vals,
                    "y": vmg_vals,
                    "mode": "lines",
                    "name": "VMG (kts)",
                    "line": {"color": "#ff7f0e", "dash": "dash"},
                }
            )

        return {
            "data": traces,
            "layout": {
                "title": "Speed & VMG Time Series",
                "xaxis": {"title": "Time"},
                "yaxis": {"title": "Speed (kts)"},
                "showlegend": True,
            },
        }
