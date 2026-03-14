"""Sail VMG comparison analysis plugin (#309).

Computes upwind and downwind VMG per sail per wind band, enabling
cross-sail performance comparison.
"""

from __future__ import annotations

import math
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
from helmlog.polar import _compute_twa

_WIND_REF_BOAT = 0
_WIND_REF_NORTH = 4

WIND_BANDS: list[tuple[float, float]] = [
    (0, 6),
    (6, 10),
    (10, 15),
    (15, 20),
    (20, float("inf")),
]


def wind_band_label(lo: float, hi: float) -> str:
    """Human-readable label for a wind band."""
    if hi == float("inf"):
        return f"{int(lo)}+"
    return f"{int(lo)}-{int(hi)}"


def wind_band_for(tws: float) -> tuple[float, float] | None:
    """Return the wind band tuple containing *tws*, or None."""
    for lo, hi in WIND_BANDS:
        if lo <= tws < hi:
            return (lo, hi)
    return None


# ---------------------------------------------------------------------------
# Pure VMG functions
# ---------------------------------------------------------------------------


def compute_upwind_vmg(bsp: float, twa: float) -> float:
    """Upwind VMG = BSP * cos(TWA). TWA < 90°."""
    return bsp * math.cos(math.radians(twa))


def compute_downwind_vmg(bsp: float, twa: float) -> float:
    """Downwind VMG = BSP * cos(180 - TWA). TWA > 90°."""
    return bsp * math.cos(math.radians(180.0 - twa))


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

# Key: (sail_id, wind_band_label, direction)
_CellKey = tuple[int, str, str]


class SailVMGPlugin(AnalysisPlugin):
    """Per-session sail VMG analysis."""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="sail_vmg",
            display_name="Sail VMG Comparison",
            description="Computes upwind/downwind VMG per sail per wind band.",
            version="1.0.0",
        )

    async def analyze(self, data: SessionData, ctx: AnalysisContext) -> AnalysisResult:
        # Determine active sail at each timestamp from sail_changes
        sail_intervals = _build_sail_intervals(data.sail_changes, data.end_utc)

        # Index speeds/headings/winds by truncated-second key
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

        # Collect VMG samples: (sail_id, wind_band, direction) → list[vmg]
        vmg_samples: dict[_CellKey, list[float]] = defaultdict(list)
        sail_names: dict[int, str] = {}

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
            if twa is None or bsp_kts <= 0:
                continue

            band = wind_band_for(tws_kts)
            if band is None:
                continue

            ts = str(spd_row["ts"])
            active = _find_active_sail(sail_intervals, ts)
            if not active:
                continue

            bl = wind_band_label(band[0], band[1])
            for sail_id, sail_name in active:
                sail_names[sail_id] = sail_name
                if twa < 90:
                    vmg = compute_upwind_vmg(bsp_kts, twa)
                    vmg_samples[(sail_id, bl, "upwind")].append(vmg)
                else:
                    vmg = compute_downwind_vmg(bsp_kts, twa)
                    vmg_samples[(sail_id, bl, "downwind")].append(vmg)

        # Build result
        cells: list[dict[str, Any]] = []
        for (sail_id, band_lbl, direction), samples in sorted(vmg_samples.items()):
            n = len(samples)
            mean_vmg = round(sum(samples) / n, 4) if n else 0
            sorted_s = sorted(samples)
            median_vmg = round(sorted_s[n // 2], 4) if n else 0
            cells.append(
                {
                    "sail_id": sail_id,
                    "sail_name": sail_names.get(sail_id, ""),
                    "wind_band": band_lbl,
                    "direction": direction,
                    "mean": mean_vmg,
                    "median": median_vmg,
                    "n": n,
                }
            )

        total_points = sum(len(s) for s in vmg_samples.values())
        metrics = [
            Metric(
                name="total_vmg_points", value=total_points, unit="count", label="VMG data points"
            ),
            Metric(
                name="sails_analyzed", value=len(sail_names), unit="count", label="Sails analyzed"
            ),
        ]

        insights: list[Insight] = []
        if total_points < 10:
            insights.append(
                Insight(
                    category="data_quality",
                    message="Too few data points for reliable VMG comparison.",
                    severity="warning",
                )
            )

        viz = [
            VizData(
                chart_type="table",
                title="Sail VMG by Wind Band",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sail_intervals(
    sail_changes: list[dict[str, Any]], end_utc: str
) -> list[tuple[str, str, list[tuple[int, str]]]]:
    """Build (start_ts, end_ts, [(sail_id, sail_name), ...]) intervals.

    Each interval spans from one sail_change to the next (or end_utc).
    """
    if not sail_changes:
        return []

    intervals: list[tuple[str, str, list[tuple[int, str]]]] = []
    for i, sc in enumerate(sail_changes):
        start_ts = str(sc["ts"])
        end_ts = str(sail_changes[i + 1]["ts"]) if i + 1 < len(sail_changes) else end_utc

        sails: list[tuple[int, str]] = []
        for key in ("main_id", "jib_id", "spinnaker_id"):
            sid = sc.get(key)
            if sid is not None:
                # Use sail_id; name will be resolved from sail_names dict
                sails.append((int(sid), key.replace("_id", "")))

        intervals.append((start_ts, end_ts, sails))

    return intervals


def _find_active_sail(
    intervals: list[tuple[str, str, list[tuple[int, str]]]],
    ts: str,
) -> list[tuple[int, str]]:
    """Return the active sails at timestamp *ts*."""
    for start, end, sails in intervals:
        if start <= ts < end:
            return sails
    # If ts is exactly the end of last interval
    if intervals and ts >= intervals[-1][0]:
        return intervals[-1][2]
    return []
