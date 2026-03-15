"""Track performance map visualization plugin (#286).

Renders a Plotly scattermapbox showing the GPS track coloured by boat
speed performance.
"""

from __future__ import annotations

from typing import Any

from helmlog.visualization.protocol import VisualizationPlugin, VizContext, VizPluginMeta


class TrackPerformanceMapPlugin(VisualizationPlugin):
    """Plotly scattermapbox with performance colouring."""

    def meta(self) -> VizPluginMeta:
        return VizPluginMeta(
            name="track_performance_map",
            display_name="Track Performance Map",
            description="GPS track coloured by boat speed performance.",
            version="1.0.0",
            required_analysis=[],
        )

    async def render(
        self,
        session_data: dict[str, Any],
        analysis_results: dict[str, Any],
        ctx: VizContext,
    ) -> dict[str, Any]:
        positions = session_data.get("positions", [])
        speeds = session_data.get("speeds", [])

        # Index speed by truncated-second key
        spd_by_ts: dict[str, float] = {}
        for s in speeds:
            spd_by_ts.setdefault(str(s.get("ts", ""))[:19], float(s.get("speed_kts", 0)))

        lats: list[float] = []
        lons: list[float] = []
        colors: list[float] = []
        text: list[str] = []

        for p in positions:
            lat = p.get("latitude_deg") or p.get("lat") or p.get("latitude")
            lon = p.get("longitude_deg") or p.get("lon") or p.get("longitude")
            if lat is None or lon is None:
                continue
            lat_f = float(lat)
            lon_f = float(lon)
            ts = str(p.get("ts", ""))
            bsp = spd_by_ts.get(ts[:19], 0.0)

            lats.append(lat_f)
            lons.append(lon_f)
            colors.append(bsp)
            text.append(f"{ts[:19]} — {bsp:.1f} kts")

        # Calculate center
        center_lat = sum(lats) / len(lats) if lats else 0.0
        center_lon = sum(lons) / len(lons) if lons else 0.0

        return {
            "data": [
                {
                    "type": "scattermapbox",
                    "lat": lats,
                    "lon": lons,
                    "mode": "markers+lines",
                    "marker": {
                        "size": 6,
                        "color": colors,
                        "colorscale": "Viridis",
                        "colorbar": {"title": "BSP (kts)"},
                    },
                    "text": text,
                    "hoverinfo": "text",
                    "line": {"width": 2},
                }
            ],
            "layout": {
                "title": "Track Performance Map",
                "mapbox": {
                    "style": "open-street-map",
                    "center": {"lat": center_lat, "lon": center_lon},
                    "zoom": 13,
                },
                "showlegend": False,
                "margin": {"r": 0, "t": 40, "l": 0, "b": 0},
            },
        }
