#!/usr/bin/env python3
"""Validation harness for the spatially varying wind field.

Generates visual output showing:
1. Wind field as a vector plot at a given timestep
2. Time-stepping animation showing shift propagation and puff movement
3. Boat tracks overlaid on the wind field
4. Side-by-side TWD/TWS time series for boats on opposite sides

Usage:
    uv run python scripts/wind_field_harness.py --seed 42
    uv run python scripts/wind_field_harness.py --seed 42 --output wind_field.html
    uv run python scripts/wind_field_harness.py --seed 42 --timestep 600
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

if TYPE_CHECKING:
    from matplotlib.figure import Figure

# Ensure project is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helmlog.courses import build_wl_course
from helmlog.synthesize import SynthConfig, simulate
from helmlog.wind_field import WindField

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_REF_LAT = 47.70
_REF_LON = -122.44
_BASE_TWD = 180.0
_GRID_POINTS = 20  # per axis
_COURSE_HALF_NM = 0.75  # half-width of visualized area


def _make_grid(
    ref_lat: float,
    ref_lon: float,
    half_nm: float,
    n: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Create a lat/lon grid centered on the reference point."""
    cos_ref = np.cos(np.radians(ref_lat))
    half_deg_lat = half_nm / 60.0
    half_deg_lon = half_nm / 60.0 / cos_ref
    lats = np.linspace(ref_lat - half_deg_lat, ref_lat + half_deg_lat, n)
    lons = np.linspace(ref_lon - half_deg_lon, ref_lon + half_deg_lon, n)
    return np.meshgrid(lons, lats)


def plot_wind_field(
    wf: WindField,
    elapsed_s: float,
    ref_lat: float,
    ref_lon: float,
    boat_rows: list[tuple[float, float]] | None = None,
) -> Figure:
    """Render wind direction and speed across the course as a vector plot."""
    lon_grid, lat_grid = _make_grid(ref_lat, ref_lon, _COURSE_HALF_NM, _GRID_POINTS)
    twd_grid = np.zeros_like(lon_grid)
    tws_grid = np.zeros_like(lon_grid)

    for i in range(lon_grid.shape[0]):
        for j in range(lon_grid.shape[1]):
            twd, tws = wf.at(elapsed_s, float(lat_grid[i, j]), float(lon_grid[i, j]))
            twd_grid[i, j] = twd
            tws_grid[i, j] = tws

    # Wind vectors (meteorological: direction wind comes FROM)
    u = -np.sin(np.radians(twd_grid)) * tws_grid
    v = -np.cos(np.radians(twd_grid)) * tws_grid

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    # Left: vector plot
    q = ax1.quiver(lon_grid, lat_grid, u, v, tws_grid, cmap="YlOrRd", scale=200)
    ax1.set_title(f"Wind Vectors — t={elapsed_s:.0f}s")
    ax1.set_xlabel("Longitude")
    ax1.set_ylabel("Latitude")
    fig.colorbar(q, ax=ax1, label="TWS (kts)")
    ax1.plot(ref_lon, ref_lat, "k+", markersize=12, markeredgewidth=2)

    if boat_rows:
        lats_b = [r[0] for r in boat_rows]
        lons_b = [r[1] for r in boat_rows]
        ax1.plot(lons_b, lats_b, "b-", linewidth=1.5, alpha=0.7, label="Boat track")
        ax1.plot(lons_b[-1], lats_b[-1], "bo", markersize=5)
        ax1.legend(fontsize=8)

    # Right: TWS heatmap
    c = ax2.pcolormesh(lon_grid, lat_grid, tws_grid, cmap="YlOrRd", shading="auto")
    ax2.set_title(f"TWS Heatmap — t={elapsed_s:.0f}s")
    ax2.set_xlabel("Longitude")
    ax2.set_ylabel("Latitude")
    fig.colorbar(c, ax=ax2, label="TWS (kts)")
    ax2.plot(ref_lon, ref_lat, "k+", markersize=12, markeredgewidth=2)

    # TWD contours
    ax2.contour(lon_grid, lat_grid, twd_grid, levels=10, colors="navy", linewidths=0.5, alpha=0.5)

    fig.tight_layout()
    return fig


def plot_comparative_series(
    wf: WindField,
    ref_lat: float,
    ref_lon: float,
    duration_s: int = 1800,
) -> Figure:
    """TWD and TWS time series for two boats on opposite sides of the course."""
    cos_ref = np.cos(np.radians(ref_lat))
    offset = 0.3 / 60.0 / cos_ref  # 0.3 nm cross-course

    times = list(range(0, duration_s, 5))
    left_twd, right_twd = [], []
    left_tws, right_tws = [], []

    for t in times:
        twd_l, tws_l = wf.at(t, ref_lat, ref_lon - offset)
        twd_r, tws_r = wf.at(t, ref_lat, ref_lon + offset)
        left_twd.append(twd_l)
        right_twd.append(twd_r)
        left_tws.append(tws_l)
        right_tws.append(tws_r)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    minutes = [t / 60.0 for t in times]

    ax1.plot(minutes, left_twd, "b-", label="Left side (−0.3 nm)", alpha=0.8)
    ax1.plot(minutes, right_twd, "r-", label="Right side (+0.3 nm)", alpha=0.8)
    ax1.set_ylabel("TWD (°)")
    ax1.set_title("Wind Direction — Left vs Right Side of Course")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    ax2.plot(minutes, left_tws, "b-", label="Left side (−0.3 nm)", alpha=0.8)
    ax2.plot(minutes, right_tws, "r-", label="Right side (+0.3 nm)", alpha=0.8)
    ax2.set_ylabel("TWS (kts)")
    ax2.set_xlabel("Time (min)")
    ax2.set_title("Wind Speed — Left vs Right Side of Course")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    return fig


def _fig_to_base64(fig: Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return base64.b64encode(buf.read()).decode()


def generate_html(seed: int, timestep: float) -> str:
    """Generate a self-contained HTML page with all validation plots."""
    wf = WindField(
        base_twd=_BASE_TWD,
        tws_low=9.0,
        tws_high=13.0,
        ref_lat=_REF_LAT,
        ref_lon=_REF_LON,
        seed=seed,
    )

    # Simulate a boat track
    legs = build_wl_course(_REF_LAT, _REF_LON, _BASE_TWD, 1.0, laps=1)
    config = SynthConfig(
        start_lat=_REF_LAT,
        start_lon=_REF_LON,
        base_twd=_BASE_TWD,
        tws_low=9.0,
        tws_high=13.0,
        shift_interval=(600.0, 1200.0),
        shift_magnitude=(5.0, 10.0),
        legs=legs,
        seed=seed,
        start_time=datetime(2025, 8, 10, 18, 0, 0, tzinfo=UTC),
    )
    rows = simulate(config)

    images: list[tuple[str, str]] = []

    # 1. Wind field at requested timestep with boat track
    elapsed_at = min(timestep, len(rows))
    track_up_to = [(r.lat, r.lon) for r in rows[: int(elapsed_at)]]
    fig = plot_wind_field(wf, timestep, _REF_LAT, _REF_LON, track_up_to)
    images.append((f"Wind Field at t={timestep:.0f}s", _fig_to_base64(fig)))

    # 2. Time-stepping: multiple snapshots showing evolution
    for t in [0, 300, 600, 900, 1200]:
        track_slice = [(r.lat, r.lon) for r in rows[: max(1, t)]]
        fig = plot_wind_field(wf, float(t), _REF_LAT, _REF_LON, track_slice)
        images.append((f"Wind Field at t={t}s ({t // 60} min)", _fig_to_base64(fig)))

    # 3. Comparative series
    fig = plot_comparative_series(wf, _REF_LAT, _REF_LON)
    images.append(("Left vs Right Side — TWD and TWS", _fig_to_base64(fig)))

    # Build HTML
    sections = []
    for title, b64 in images:
        sections.append(
            f'<h2>{title}</h2>\n<img src="data:image/png;base64,{b64}" style="max-width:100%">'
        )

    return f"""<!DOCTYPE html>
<html>
<head>
<title>Wind Field Validation — seed={seed}</title>
<style>
body {{ font-family: sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }}
h1 {{ color: #1a5276; }}
h2 {{ color: #2c3e50; border-bottom: 1px solid #bdc3c7; padding-bottom: 8px; }}
img {{ margin: 10px 0 30px; border: 1px solid #ddd; border-radius: 4px; }}
.meta {{ color: #7f8c8d; font-size: 14px; }}
</style>
</head>
<body>
<h1>Wind Field Validation Harness</h1>
<p class="meta">Seed: {seed} | Base TWD: {_BASE_TWD}&deg; |
   Ref: ({_REF_LAT}, {_REF_LON}) | Boat track: {len(rows)} points</p>
{"".join(sections)}
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Wind field validation harness")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--timestep", type=float, default=600.0, help="Timestep to visualize (s)")
    parser.add_argument("--output", type=str, default=None, help="Output HTML file path")
    args = parser.parse_args()

    html = generate_html(args.seed, args.timestep)

    out = Path(args.output) if args.output else Path(f"wind_field_seed{args.seed}.html")

    out.write_text(html)
    print(f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
