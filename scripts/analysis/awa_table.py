"""Compute the (TWA, TWS, BSP) -> AWA conversion table from race data.

For every (TWS bin, TWA bin) cell with enough samples, compute AWA using:
    AWA = atan2( TWS · sin(TWA), TWS · cos(TWA) + BSP )

Both TWA and AWA in degrees absolute (folded to [0, 180]).
"""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HELMLOG_DB", "data/logger.db")


def fold(a):
    a = abs(a) % 360
    return 360 - a if a > 180 else a


def twa_to_awa(twa_deg: float, tws_kt: float, bsp_kt: float) -> float:
    twa = math.radians(twa_deg)
    x = tws_kt * math.sin(twa)
    y = tws_kt * math.cos(twa) + bsp_kt
    awa = math.degrees(math.atan2(x, y))
    return fold(awa)


def get_effective_start(conn, race):
    if race["vakaros_session_id"] is not None:
        gun = conn.execute(
            "SELECT ts FROM vakaros_race_events WHERE session_id=?"
            " AND event_type='race_start' AND ts BETWEEN ? AND ?"
            " ORDER BY ts DESC LIMIT 1",
            (race["vakaros_session_id"], race["start_utc"], race["end_utc"]),
        ).fetchone()
        if gun:
            return gun["ts"]
    return (datetime.fromisoformat(race["start_utc"]) + timedelta(minutes=5)).isoformat()


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

    cells = defaultdict(list)  # (tws_b, twa_b) -> [bsp]
    for tws, twa, bsp in samples:
        cells[(int(math.floor(tws)), int(math.floor(twa / 5) * 5))].append(bsp)

    print("=" * 80)
    print("TWA → AWA conversion (using mean BSP at each cell, ≥30 samples)")
    print("=" * 80)
    print(f"{'TWS':>5} {'TWA':>5} {'mean BSP':>9} {'→ AWA':>8} {'n':>5}")
    print("-" * 50)
    # Print useful cells: TWS 5-18, TWA 30-175 step 5
    for tws_b in range(4, 21):
        for twa_b in range(30, 181, 5):
            v = cells.get((tws_b, twa_b), [])
            if len(v) >= 30:
                mean_bsp = statistics.mean(v)
                tws_mid = tws_b + 0.5
                twa_mid = twa_b + 2.5
                awa = twa_to_awa(twa_mid, tws_mid, mean_bsp)
                print(
                    f"{tws_b:>3}-{tws_b + 1:<2} {twa_b:>5} {mean_bsp:>9.2f} {awa:>8.0f} {len(v):>5}"
                )

    # ============================================================
    # Useful summary: for each (TWS, TWA point of interest), give AWA
    # ============================================================
    print()
    print("=" * 80)
    print("Quick lookup — common targets")
    print("=" * 80)
    targets = [
        # (label, tws_min, tws_max, twa)
        ("Light upwind sail", 4, 8, 60),  # what we sail
        ("Light upwind own-best", 4, 8, 45),  # own optimum
        ("Light downwind sail", 5, 10, 130),  # what we sail
        ("Light downwind own-best", 5, 10, 150),  # own optimum
        ("Mod upwind sail", 9, 13, 50),
        ("Mod upwind own-best", 9, 13, 40),
        ("Mod downwind sail", 11, 15, 150),
        ("Mod downwind own-best", 11, 15, 160),
        ("Heavy upwind", 14, 18, 40),
        ("Heavy downwind", 14, 18, 155),
    ]
    print(f"{'scenario':<26} {'TWS':>5} {'TWA':>5} {'BSP':>5} {'AWA':>5}")
    print("-" * 60)
    for label, tws_lo, tws_hi, twa in targets:
        bsps = []
        for tws_b in range(tws_lo, tws_hi + 1):
            twa_b = int(math.floor(twa / 5) * 5)
            v = cells.get((tws_b, twa_b), [])
            if v:
                bsps.extend(v)
        if not bsps:
            print(f"{label:<26} {tws_lo}-{tws_hi:<3} {twa:>5}  no data")
            continue
        mean_bsp = statistics.mean(bsps)
        tws_mid = (tws_lo + tws_hi) / 2
        awa = twa_to_awa(twa, tws_mid, mean_bsp)
        print(f"{label:<26} {tws_lo}-{tws_hi:<3} {twa:>5} {mean_bsp:>5.2f} {awa:>5.0f}")


if __name__ == "__main__":
    main()
