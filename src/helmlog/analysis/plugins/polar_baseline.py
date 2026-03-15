"""Polar baseline analysis plugin (#283).

Wraps the existing session_polar_comparison logic from polar.py as the
first plugin on the analysis framework, proving the protocol works.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

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
from helmlog.polar import _compute_twa, _twa_bin, _tws_bin

_WIND_REF_BOAT = 0
_WIND_REF_NORTH = 4


class PolarBaselinePlugin(AnalysisPlugin):
    """Compare session BSP against the polar baseline."""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="polar_baseline",
            display_name="Polar Baseline Comparison",
            description="Compares session boat speed against the historical polar baseline.",
            version="1.0.0",
        )

    async def analyze(self, data: SessionData, ctx: AnalysisContext) -> AnalysisResult:
        # Index speeds/headings by truncated-second key
        spd_by_s: dict[str, dict[str, Any]] = {}
        for s in data.speeds:
            spd_by_s.setdefault(str(s["ts"])[:19], s)

        hdg_by_s: dict[str, dict[str, Any]] = {}
        for h in data.headings:
            hdg_by_s.setdefault(str(h["ts"])[:19], h)

        tw_by_s: dict[str, dict[str, Any]] = {}
        for w in data.winds:
            ref = int(w.get("reference", -1))
            if ref not in (_WIND_REF_BOAT, _WIND_REF_NORTH):
                continue
            tw_by_s.setdefault(str(w["ts"])[:19], w)

        # Bin session samples
        bin_samples: dict[tuple[int, int], list[float]] = defaultdict(list)
        for sk, spd_row in spd_by_s.items():
            wind_row = tw_by_s.get(sk)
            if wind_row is None:
                continue
            ref = int(wind_row.get("reference", -1))
            wind_angle = float(wind_row["wind_angle_deg"])
            tws_kts = float(wind_row["wind_speed_kts"])
            bsp_kts = float(spd_row["speed_kts"])

            hdg_row = hdg_by_s.get(sk)
            heading = float(hdg_row["heading_deg"]) if hdg_row else None

            twa = _compute_twa(wind_angle, ref, heading)
            if twa is None:
                continue

            tb = _tws_bin(tws_kts)
            ab = _twa_bin(twa)
            bin_samples[(tb, ab)].append(bsp_kts)

        # Build metrics
        all_bsp: list[float] = []
        cells: list[dict[str, Any]] = []

        for (tb, ab), samples in sorted(bin_samples.items()):
            session_mean = round(sum(samples) / len(samples), 4)
            all_bsp.extend(samples)
            cells.append(
                {
                    "tws_bin": tb,
                    "twa_bin": ab,
                    "session_mean_bsp": session_mean,
                    "sample_count": len(samples),
                }
            )

        total_samples = len(all_bsp)
        avg_bsp = round(sum(all_bsp) / total_samples, 4) if total_samples else 0.0

        metrics = [
            Metric(name="total_samples", value=total_samples, unit="count", label="Data points"),
            Metric(name="avg_bsp", value=avg_bsp, unit="kts", label="Average BSP"),
            Metric(name="bins_populated", value=len(cells), unit="count", label="Bins with data"),
        ]

        insights: list[Insight] = []
        if total_samples < 10:
            insights.append(
                Insight(
                    category="data_quality",
                    message="Insufficient data points for reliable polar comparison.",
                    severity="warning",
                )
            )

        # Build viz data for polar chart
        viz = [
            VizData(
                chart_type="polar",
                title="Session Polar",
                data={"cells": cells},
            )
        ]

        return AnalysisResult(
            plugin_name=self.meta().name,
            plugin_version=self.meta().version,
            session_id=data.session_id,
            metrics=metrics,
            insights=insights,
            viz=viz,
            raw={"cells": cells},
        )
