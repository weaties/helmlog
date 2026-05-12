"""Current set/drift + wind-shift-catching analysis.

CURRENT (set/drift): Vector difference between motion-through-water (BSP @ HDG)
and motion-over-ground (SOG @ COG) is the current vector. Averaged over each
race when both signals are present.

SHIFT-CATCHING: For every tack/jibe maneuver in the maneuvers table, we look at
the TWD trend in the 90s before vs 90s after the maneuver. A tack into the new
breeze direction (i.e. tack on a header) is "good"; tack away from the shift
("tack on a lift") is "bad".
"""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HELMLOG_DB", "data/logger.db")


def fold(a):
    a = abs(a) % 360
    return 360 - a if a > 180 else a


def angle_diff(a, b):
    """Signed difference a - b normalized to (-180, 180]."""
    d = (a - b + 540) % 360 - 180
    return d


def vec_subtract(speed1, dir1, speed2, dir2):
    """Return (mag, dir) of vector1 - vector2 where dirs are deg from N."""
    x = speed1 * math.sin(math.radians(dir1)) - speed2 * math.sin(math.radians(dir2))
    y = speed1 * math.cos(math.radians(dir1)) - speed2 * math.cos(math.radians(dir2))
    mag = math.hypot(x, y)
    deg = (math.degrees(math.atan2(x, y)) + 360) % 360
    return mag, deg


def load_aligned(conn, start, end):
    sp = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT substr(ts,1,19), AVG(speed_kts) FROM speeds"
            " WHERE ts BETWEEN ? AND ? GROUP BY substr(ts,1,19)",
            (start, end),
        )
    }
    cg = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT substr(ts,1,19), AVG(cog_deg), AVG(sog_kts) FROM cogsog"
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
        cog, sog = cg.get(k, (None, None))
        hdg = hd.get(k)
        # TWD (north-referenced) = (heading + signed-twa) mod 360, or use ref=4
        if k in wn:
            twd = wn[k][1] % 360
        elif hdg is not None:
            twd = (hdg + signed) % 360
        else:
            twd = None
        out.append(
            {
                "k": k,
                "bsp": bsp,
                "sog": sog,
                "cog": cog,
                "hdg": hdg,
                "tws": tws,
                "twa": twa,
                "twd": twd,
                "tack": "starboard" if signed >= 0 else "port",
                "vmg": bsp * math.cos(math.radians(twa)),
            }
        )
    return out


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    races = list(
        conn.execute(
            "SELECT id, name, date, session_type, start_utc, end_utc FROM races"
            " WHERE end_utc IS NOT NULL AND start_utc != end_utc AND session_type='race'"
            "   AND name NOT LIKE '%RTTS%'"  # exclude double-handed long-distance
            " ORDER BY start_utc"
        )
    )
    print(f"{len(races)} races (RTTS excluded)\n")

    # ============================================================
    # CURRENT — set/drift per race
    # ============================================================
    print("=" * 90)
    print("CURRENT (set/drift) per race")
    print("Vector: (SOG @ COG) - (BSP @ HDG). Aggregated over the race.")
    print("Set = direction the current is FLOWING TOWARD (deg from N).")
    print("=" * 90)
    print(f"{'date':<11} {'race':<40} {'sec':>5} {'drift kt':>8} {'set °':>6} {'TWS':>5}")
    print("-" * 80)
    current_per_race = {}
    for r in races:
        rs = load_aligned(conn, r["start_utc"], r["end_utc"])
        # Need bsp + hdg + sog + cog all present, and BSP>1 (moving)
        valid = [
            x
            for x in rs
            if x["bsp"] > 1.0
            and x["sog"] is not None
            and x["cog"] is not None
            and x["hdg"] is not None
            and x["tws"] is not None
        ]
        if len(valid) < 60:
            continue
        # Per-second current vector: (SOG, COG) - (BSP, HDG)
        xs, ys = [], []
        for x in valid:
            cx = x["sog"] * math.sin(math.radians(x["cog"])) - x["bsp"] * math.sin(
                math.radians(x["hdg"])
            )
            cy = x["sog"] * math.cos(math.radians(x["cog"])) - x["bsp"] * math.cos(
                math.radians(x["hdg"])
            )
            # Filter out absurd values (instrument glitches)
            mag = math.hypot(cx, cy)
            if mag < 4.0:  # current rarely > 4 kt in race areas
                xs.append(cx)
                ys.append(cy)
        if len(xs) < 60:
            continue
        mean_cx = sum(xs) / len(xs)
        mean_cy = sum(ys) / len(ys)
        drift = math.hypot(mean_cx, mean_cy)
        set_deg = (math.degrees(math.atan2(mean_cx, mean_cy)) + 360) % 360
        avg_tws = statistics.mean([x["tws"] for x in valid])
        current_per_race[r["id"]] = {
            "drift": drift,
            "set": set_deg,
            "name": r["name"],
            "date": r["date"],
        }
        print(
            f"{r['date']:<11} {r['name']:<40} {len(xs):>5} "
            f"{drift:>8.2f} {set_deg:>6.0f} {avg_tws:>5.1f}"
        )

    # ============================================================
    # SHIFT-CATCHING — for every tack, was it on a header or a lift?
    # ============================================================
    print()
    print("=" * 90)
    print("SHIFT-CATCHING — for every tack, what was the wind doing?")
    print("=" * 90)
    print("Method: compare median TWD in 60-30s BEFORE the tack to TWD in 30-60s AFTER.")
    print("If the boat tacked INTO the new wind (turned toward where the wind shifted")
    print("TO), that's catching a header — good. If it tacked AWAY, that's tacking on")
    print("a lift — bad.")
    print()

    # All tacks with timestamps — race sessions only, exclude RTTS
    tacks = list(
        conn.execute(
            "SELECT m.id, m.session_id, m.ts, m.duration_sec, r.name AS race_name"
            " FROM maneuvers m"
            " JOIN races r ON r.id = m.session_id"
            " WHERE m.type = 'tack' AND r.session_type='race'"
            "   AND r.name NOT LIKE '%RTTS%' AND r.end_utc IS NOT NULL"
            " ORDER BY m.ts"
        )
    )

    # For each tack, look at TWD WAY before (90-150s) vs WAY after (90-150s) — gives
    # the sensor time to settle out of the tack disturbance, and reduces the chance
    # that a sensor-lag artifact looks like a real shift.
    classified = []
    shift_magnitudes = []
    for t in tacks:
        ts = datetime.fromisoformat(t["ts"])
        b_start = (ts - timedelta(seconds=150)).isoformat()
        b_end = (ts - timedelta(seconds=90)).isoformat()
        a_start = (ts + timedelta(seconds=90)).isoformat()
        a_end = (ts + timedelta(seconds=150)).isoformat()

        # Get TWD before (median over 30s window) — use ref=4 (TWD direct) if available,
        # else compute from heading + signed TWA at ref=0.
        def median_twd(start, end):
            twds = []
            for r in conn.execute(
                "SELECT wind_angle_deg FROM winds WHERE reference=4 AND ts BETWEEN ? AND ?",
                (start, end),
            ):
                twds.append(r["wind_angle_deg"] % 360)
            if twds:
                return statistics.median(twds)
            # Fall back to ref=0 (TWA boat-referenced) + heading
            wb_rows = conn.execute(
                "SELECT ts, wind_angle_deg FROM winds WHERE reference=0 AND ts BETWEEN ? AND ?",
                (start, end),
            ).fetchall()
            if not wb_rows:
                return None
            for w in wb_rows:
                ts_key = w["ts"][:19]
                hdg_row = conn.execute(
                    "SELECT heading_deg FROM headings WHERE ts >= ? AND ts < ? LIMIT 1",
                    (ts_key, ts_key + ":99"),
                ).fetchone()
                if hdg_row is None:
                    continue
                signed = ((w["wind_angle_deg"] + 180) % 360) - 180
                twds.append((hdg_row["heading_deg"] + signed) % 360)
            return statistics.median(twds) if twds else None

        # Heading before & after
        def median_hdg(start, end):
            rows = conn.execute(
                "SELECT heading_deg FROM headings WHERE ts BETWEEN ? AND ?", (start, end)
            ).fetchall()
            return statistics.median([r["heading_deg"] for r in rows]) if rows else None

        twd_before = median_twd(b_start, b_end)
        twd_after = median_twd(a_start, a_end)
        hdg_before = median_hdg(b_start, b_end)
        hdg_after = median_hdg(a_start, a_end)

        if None in (twd_before, twd_after, hdg_before, hdg_after):
            continue

        # Heading change — sign tells us direction of tack
        hdg_change = angle_diff(hdg_after, hdg_before)
        # TWD change — sign tells us direction of shift
        twd_change = angle_diff(twd_after, twd_before)

        # Only count if heading change is meaningful (a real tack: 60-180°)
        if abs(hdg_change) < 60 or abs(hdg_change) > 180:
            continue

        # Sign convention:
        # - If hdg_change > 0 (boat turned right/clockwise) and twd_change > 0
        #   (wind clocked right too), the tack was AWAY from the shift = bad
        # - If hdg_change > 0 and twd_change < 0 (wind backed left) — tacked
        #   INTO the new wind = good (caught a header)
        # Use a meaningful threshold (10°) — below that, it's noise
        if abs(twd_change) < 10:
            verdict = "neutral"
        elif (hdg_change > 0 and twd_change < 0) or (hdg_change < 0 and twd_change > 0):
            verdict = "good (tacked on header)"
        else:
            verdict = "bad (tacked on lift)"

        classified.append(
            {
                "race_id": t["session_id"],
                "race_name": t["race_name"],
                "ts": t["ts"],
                "twd_change": twd_change,
                "hdg_change": hdg_change,
                "verdict": verdict,
            }
        )
        shift_magnitudes.append(abs(twd_change))

    # Aggregate
    print(
        f"Classified {len(classified)} tacks across {len(set(c['race_id'] for c in classified))} races."
    )
    print(f"Median wind shift magnitude per tack: {statistics.median(shift_magnitudes):.1f}°")
    print(f"Mean shift magnitude per tack:        {statistics.mean(shift_magnitudes):.1f}°")
    counts = defaultdict(int)
    for c in classified:
        counts[c["verdict"]] += 1
    total = sum(counts.values())
    print()
    print(
        f"  Good (tacked on a header ≥10°):  {counts['good (tacked on header)']:>3} "
        f"({100 * counts['good (tacked on header)'] / total:.0f}%)"
    )
    print(
        f"  Bad  (tacked on a lift ≥10°):    {counts['bad (tacked on lift)']:>3} "
        f"({100 * counts['bad (tacked on lift)'] / total:.0f}%)"
    )
    print(
        f"  Neutral (shift <10°):            {counts['neutral']:>3} "
        f"({100 * counts['neutral'] / total:.0f}%)"
    )
    sg = counts["good (tacked on header)"] + counts["bad (tacked on lift)"]
    if sg > 0:
        hit_rate = counts["good (tacked on header)"] / sg
        print(f"\n  Shift-tack hit rate (when there WAS a shift): {hit_rate * 100:.0f}%")
        print(f"  (50% would be random chance; 60-70% is good club racing.)")

    # Per-race breakdown
    print()
    print("Per race:")
    print(f"{'date':<11} {'race':<40} {'tacks':>5} {'good':>4} {'bad':>4} {'neut':>4}")
    print("-" * 75)
    by_race = defaultdict(
        lambda: {
            "good (tacked on header)": 0,
            "bad (tacked on lift)": 0,
            "neutral": 0,
            "name": "",
            "date": "",
        }
    )
    for c in classified:
        b = by_race[c["race_id"]]
        b[c["verdict"]] += 1
        b["name"] = c["race_name"] or ""
    for rid, b in sorted(by_race.items()):
        race_row = next((r for r in races if r["id"] == rid), None)
        if race_row is None:
            continue
        total_r = b["good (tacked on header)"] + b["bad (tacked on lift)"] + b["neutral"]
        if total_r < 2:
            continue
        print(
            f"{race_row['date']:<11} {b['name']:<40} {total_r:>5} "
            f"{b['good (tacked on header)']:>4} "
            f"{b['bad (tacked on lift)']:>4} {b['neutral']:>4}"
        )


if __name__ == "__main__":
    main()
