"""Generate a clean polar diagram of mean BSP vs (TWS, TWA) for the report."""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import numpy as np

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HELMLOG_DB", "data/logger.db")
OUT = sys.argv[2] if len(sys.argv) > 2 else (os.environ.get("HELMLOG_OUT_DIR", ".") + "/polar.png")


def fold(a):
    a = abs(a) % 360
    return 360 - a if a > 180 else a


def load_aligned(conn, start, end):
    sp = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT substr(ts,1,19), AVG(speed_kts) FROM speeds"
            " WHERE ts BETWEEN ? AND ? GROUP BY substr(ts,1,19)",
            (start, end),
        )
    }
    hd = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT substr(ts,1,19), AVG(heading_deg) FROM headings"
            " WHERE ts BETWEEN ? AND ? GROUP BY substr(ts,1,19)",
            (start, end),
        )
    }
    wb, wn = {}, {}
    for r in conn.execute(
        "SELECT substr(ts,1,19), reference, AVG(wind_speed_kts), AVG(wind_angle_deg)"
        " FROM winds WHERE ts BETWEEN ? AND ? AND reference IN (0,4)"
        " GROUP BY substr(ts,1,19), reference",
        (start, end),
    ):
        (wb if r[1] == 0 else wn)[r[0]] = (r[2], r[3])
    out = []
    for k, bsp in sp.items():
        if k in wb:
            tws, raw = wb[k]
            signed = ((raw + 180) % 360) - 180
        elif k in wn and k in hd:
            tws, twd_n = wn[k]
            signed = ((twd_n - hd[k] + 180) % 360) - 180
        else:
            continue
        twa = fold(signed)
        out.append((tws, twa, bsp))
    return out


def get_effective_start(conn, race):
    if race["vakaros_session_id"] is not None:
        gun = conn.execute(
            "SELECT ts FROM vakaros_race_events WHERE session_id=?"
            " AND event_type='race_start' AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
            (race["vakaros_session_id"], race["start_utc"], race["end_utc"]),
        ).fetchone()
        if gun is not None:
            return gun["ts"]
    return (datetime.fromisoformat(race["start_utc"]) + timedelta(minutes=5)).isoformat()


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    races = list(
        conn.execute(
            "SELECT id, name, date, start_utc, end_utc, vakaros_session_id FROM races"
            " WHERE end_utc IS NOT NULL AND start_utc != end_utc"
            "   AND session_type='race' AND name NOT LIKE '%RTTS%'"
            " ORDER BY start_utc"
        )
    )

    samples = []
    for r in races:
        start = get_effective_start(conn, r)
        for tws, twa, bsp in load_aligned(conn, start, r["end_utc"]):
            if 1.5 < tws < 35 and bsp > 0.2 and 30 <= twa <= 180:
                samples.append((tws, twa, bsp))

    # Bin: 5° TWA × 1kt TWS, mean BSP per bin
    cells = defaultdict(list)
    for tws, twa, bsp in samples:
        tb = int(math.floor(tws))
        ab = int(math.floor(twa / 5) * 5)
        cells[(tb, ab)].append(bsp)

    # Build curves: for each TWS bin, the (twa, mean_bsp) for that wind speed
    tws_bins_to_plot = [6, 8, 10, 12, 14, 16]
    fig, ax = plt.subplots(figsize=(8.5, 8.5), subplot_kw={"projection": "polar"})

    # Polar plot setup: 0° at top (head-to-wind), increasing clockwise
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    # Show 0-180 only (one half of polar)
    ax.set_thetamin(0)
    ax.set_thetamax(180)

    cmap = plt.colormaps["viridis"]
    colors = [cmap(i / max(1, len(tws_bins_to_plot) - 1)) for i in range(len(tws_bins_to_plot))]

    for tws_b, color in zip(tws_bins_to_plot, colors):
        # Build separate upwind (TWA<90) and downwind (TWA>90) curves —
        # we don't race in the 90° beam-reach zone, so a line crossing it
        # would imply a polar shape we don't actually sail.
        up_twas, up_bsps, dn_twas, dn_bsps = [], [], [], []
        for ab in range(30, 181, 5):
            v = cells.get((tws_b, ab), [])
            if len(v) >= 30:
                if ab < 90:
                    up_twas.append(ab)
                    up_bsps.append(statistics.mean(v))
                elif ab >= 95:  # leave 90 as a gap
                    dn_twas.append(ab)
                    dn_bsps.append(statistics.mean(v))
        label = f"{tws_b}–{tws_b + 1} kt TWS"
        if len(up_twas) >= 3:
            theta_up = [math.radians(t) for t in up_twas]
            ax.plot(
                theta_up, up_bsps, marker="o", markersize=4, linewidth=2, color=color, label=label
            )
            label = None  # avoid double legend entry
        if len(dn_twas) >= 3:
            theta_dn = [math.radians(t) for t in dn_twas]
            ax.plot(
                theta_dn, dn_bsps, marker="o", markersize=4, linewidth=2, color=color, label=label
            )

    # Style
    ax.set_rmax(9)
    ax.set_rticks([2, 4, 6, 8])
    ax.set_rlabel_position(135)
    ax.grid(True, alpha=0.4)
    ax.set_title(
        "Corvo 105 — Observed Polar (mean BSP per TWA bin)\n"
        "Race-only data, post-gun, RTTS excluded · April 9 – May 12, 2026",
        pad=20,
        fontsize=11,
    )
    # Annotate axis
    ax.text(math.radians(0), 9.3, "Head to wind", ha="center", fontsize=9, alpha=0.7)
    ax.text(math.radians(180), 9.3, "Dead\ndownwind", ha="center", fontsize=9, alpha=0.7)
    ax.text(math.radians(90), 9.3, "Beam reach", ha="center", fontsize=9, alpha=0.7)

    ax.legend(loc="upper right", bbox_to_anchor=(1.18, 1.05), fontsize=9, framealpha=0.95)

    fig.tight_layout()
    fig.savefig(OUT, dpi=140, bbox_inches="tight")
    print(f"Wrote {OUT}")

    # ============================================================
    # AWA polar — same data, but plotted vs apparent wind angle
    # ============================================================
    OUT_AWA = OUT.replace(".png", "_awa.png")
    fig3, ax3 = plt.subplots(figsize=(8.5, 8.5), subplot_kw={"projection": "polar"})
    ax3.set_theta_zero_location("N")
    ax3.set_theta_direction(-1)
    ax3.set_thetamin(0)
    ax3.set_thetamax(180)
    for tws_b, color in zip(tws_bins_to_plot, colors):
        up_awas, up_bsps, dn_awas, dn_bsps = [], [], [], []
        for ab in range(30, 181, 5):
            v = cells.get((tws_b, ab), [])
            if len(v) >= 30:
                bsp_mean = statistics.mean(v)
                # AWA from TWA + TWS + BSP
                tws_mid = tws_b + 0.5
                twa_rad = math.radians(ab + 2.5)
                awa = math.degrees(
                    math.atan2(
                        tws_mid * math.sin(twa_rad),
                        tws_mid * math.cos(twa_rad) + bsp_mean,
                    )
                )
                awa_abs = abs(awa) if abs(awa) <= 180 else 360 - abs(awa)
                if ab < 90:
                    up_awas.append(awa_abs)
                    up_bsps.append(bsp_mean)
                elif ab >= 95:
                    dn_awas.append(awa_abs)
                    dn_bsps.append(bsp_mean)
        label = f"{tws_b}–{tws_b + 1} kt TWS"
        if len(up_awas) >= 3:
            theta_up = [math.radians(a) for a in up_awas]
            ax3.plot(
                theta_up, up_bsps, marker="o", markersize=4, linewidth=2, color=color, label=label
            )
            label = None
        if len(dn_awas) >= 3:
            theta_dn = [math.radians(a) for a in dn_awas]
            ax3.plot(
                theta_dn, dn_bsps, marker="o", markersize=4, linewidth=2, color=color, label=label
            )
    ax3.set_rmax(9)
    ax3.set_rticks([2, 4, 6, 8])
    ax3.set_rlabel_position(135)
    ax3.grid(True, alpha=0.4)
    ax3.set_title(
        "Corvo 105 — Observed Polar by APPARENT WIND ANGLE\n"
        "(angular axis is what the AWA gauge reads, not TWA)\n"
        "Race-only · post-gun · April 9 – May 12, 2026",
        pad=20,
        fontsize=11,
    )
    ax3.text(math.radians(0), 9.3, "Head to wind", ha="center", fontsize=9, alpha=0.7)
    ax3.text(math.radians(180), 9.3, "Dead\ndownwind\n(AWA)", ha="center", fontsize=9, alpha=0.7)
    ax3.legend(loc="upper right", bbox_to_anchor=(1.18, 1.05), fontsize=9, framealpha=0.95)
    fig3.tight_layout()
    fig3.savefig(OUT_AWA, dpi=140, bbox_inches="tight")
    print(f"Wrote {OUT_AWA}")

    # Also save a heatmap visualization
    OUT2 = OUT.replace(".png", "_heatmap.png")
    fig2, ax2 = plt.subplots(figsize=(11, 6))
    twa_bins = list(range(35, 181, 5))
    tws_bins = list(range(4, 21))
    grid = np.full((len(tws_bins), len(twa_bins)), np.nan)
    for i, tb in enumerate(tws_bins):
        for j, ab in enumerate(twa_bins):
            v = cells.get((tb, ab), [])
            if len(v) >= 30:
                grid[i, j] = statistics.mean(v)
    im = ax2.imshow(
        grid,
        aspect="auto",
        origin="lower",
        cmap="viridis",
        extent=[twa_bins[0], twa_bins[-1] + 5, tws_bins[0], tws_bins[-1] + 1],
        vmin=2,
        vmax=9,
    )
    cbar = fig2.colorbar(im, ax=ax2)
    cbar.set_label("Mean BSP (kt)")
    ax2.set_xlabel("TWA (°)")
    ax2.set_ylabel("TWS (kt)")
    ax2.set_title(
        "Corvo 105 — Mean BSP heatmap by (TWS, TWA)\nRace-only · post-gun · cells with ≥30 sec only"
    )
    ax2.axvline(x=90, color="white", linestyle="--", alpha=0.5)
    ax2.text(60, 19.5, "← upwind", color="white", fontsize=9, alpha=0.85)
    ax2.text(120, 19.5, "downwind →", color="white", fontsize=9, alpha=0.85)
    fig2.tight_layout()
    fig2.savefig(OUT2, dpi=140, bbox_inches="tight")
    print(f"Wrote {OUT2}")


if __name__ == "__main__":
    main()
