"""Consolidated downwind/upwind/shift/current analysis.

Improvements vs prior scripts:
  1. Uses Vakaros race_start as the effective gun when available (skips
     pre-start maneuvering that previously polluted the dataset). Falls back
     to start_utc + 5min for races without a Vakaros gun.
  2. VMG + shift analysis: races only, RTTS excluded, post-gun only.
  3. Current: includes practice sessions, but only counts "long sustained
     legs" (heading stable ±15° peak-to-peak for ≥60s, BSP > 2 kt) — this
     gives a much cleaner current vector by avoiding tacks/jibes/roundings.
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


def angle_diff(a, b):
    return (a - b + 540) % 360 - 180


def hdg_spread(values):
    """Peak-to-peak spread of headings (handling 359→0 wraparound)."""
    if len(values) < 2:
        return 0.0
    # Convert to unit vectors, find min/max angle relative to mean
    xs = [math.sin(math.radians(v)) for v in values]
    ys = [math.cos(math.radians(v)) for v in values]
    mean_dir = math.degrees(math.atan2(sum(xs) / len(xs), sum(ys) / len(ys)))
    deltas = [abs(angle_diff(v, mean_dir)) for v in values]
    return 2 * max(deltas)


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
        twd = wn[k][1] % 360 if k in wn else ((hdg + signed) % 360 if hdg is not None else None)
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
    out.sort(key=lambda r: r["k"])
    return out


def get_effective_start(conn, race):
    """Return the actual gun time for a race.

    Prefers Vakaros race_start event; falls back to start_utc + 5min when
    Vakaros gun is unavailable. The +5min skip approximates the typical race
    start sequence so we don't include pre-start maneuvering.
    """
    if race["vakaros_session_id"] is not None:
        gun = conn.execute(
            "SELECT ts FROM vakaros_race_events"
            " WHERE session_id=? AND event_type='race_start'"
            "   AND ts BETWEEN ? AND ?"
            " ORDER BY ts DESC LIMIT 1",
            (race["vakaros_session_id"], race["start_utc"], race["end_utc"]),
        ).fetchone()
        if gun is not None:
            return gun["ts"], "vakaros"
    # Fallback: start_utc + 5min
    db_start = datetime.fromisoformat(race["start_utc"])
    return (db_start + timedelta(minutes=5)).isoformat(), "fallback+5min"


def detect_steady_legs(rows, min_duration_s=60, max_hdg_spread=15.0, min_bsp=2.0):
    """Yield (start_idx, end_idx) of contiguous steady-heading segments.

    Steady = heading peak-to-peak within max_hdg_spread, BSP > min_bsp.
    Useful for clean current measurement (no maneuvers, no drifting).
    """
    n = len(rows)
    if n < min_duration_s:
        return
    # Sliding window: try to extend each starting point
    i = 0
    while i < n - min_duration_s:
        # Skip rows missing required fields
        if rows[i]["hdg"] is None or rows[i]["bsp"] < min_bsp or rows[i]["sog"] is None:
            i += 1
            continue
        j = i + 1
        # Extend while heading still steady and BSP still > min
        while j < n:
            if rows[j]["hdg"] is None or rows[j]["bsp"] < min_bsp or rows[j]["sog"] is None:
                break
            window = [rows[k]["hdg"] for k in range(i, j + 1) if rows[k]["hdg"] is not None]
            if hdg_spread(window) > max_hdg_spread:
                break
            j += 1
        # Did the segment last long enough?
        if j - i >= min_duration_s:
            yield (i, j)
            i = j  # skip past — non-overlapping segments
        else:
            i += 1


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Race sessions for VMG/shift analysis (RTTS excluded)
    races = list(
        conn.execute(
            "SELECT id, name, date, session_type, start_utc, end_utc, vakaros_session_id"
            " FROM races"
            " WHERE end_utc IS NOT NULL AND start_utc != end_utc"
            "   AND session_type='race' AND name NOT LIKE '%RTTS%'"
            " ORDER BY start_utc"
        )
    )

    # ALL sessions (race + practice, RTTS excluded) — used only for current analysis
    all_sessions = list(
        conn.execute(
            "SELECT id, name, date, session_type, start_utc, end_utc, vakaros_session_id"
            " FROM races"
            " WHERE end_utc IS NOT NULL AND start_utc != end_utc"
            "   AND name NOT LIKE '%RTTS%'"
            " ORDER BY start_utc"
        )
    )

    print(f"VMG/shift analysis: {len(races)} race sessions (practice + RTTS excluded)")
    print(f"Current analysis:   {len(all_sessions)} sessions (race + practice; RTTS excluded)")
    print()

    # Pre-compute effective starts for each race
    starts_info = {}
    for r in races:
        start, src = get_effective_start(conn, r)
        starts_info[r["id"]] = (start, src)
    n_vakaros = sum(1 for s, src in starts_info.values() if src == "vakaros")
    print(
        f"Effective start: {n_vakaros}/{len(races)} via Vakaros gun, "
        f"{len(races) - n_vakaros} via start_utc+5min fallback"
    )
    print()

    # ============================================================
    # Build VMG datasets (post-gun only)
    # ============================================================
    rows_dn, rows_up = [], []
    per_race_rows = {}
    for r in races:
        start, _ = starts_info[r["id"]]
        rs = load_aligned(conn, start, r["end_utc"])
        per_race_rows[r["id"]] = rs
        for x in rs:
            if x["bsp"] < 0.5 or not (1.5 < x["tws"] < 35):
                continue
            if 90 <= x["twa"] <= 180:
                rows_dn.append(x)
            elif 30 <= x["twa"] < 90:
                rows_up.append(x)
    print(f"Downwind seconds (post-gun): {len(rows_dn):,} ({len(rows_dn) / 3600:.1f} h)")
    print(f"Upwind seconds (post-gun):   {len(rows_up):,} ({len(rows_up) / 3600:.1f} h)")
    print()

    # ============================================================
    # 1. DOWNWIND POLAR (post-gun, races only, no RTTS)
    # ============================================================
    print("=" * 90)
    print("DOWNWIND POLAR — races only, post-gun, RTTS excluded")
    print("=" * 90)
    bin_dn = defaultdict(list)
    for r in rows_dn:
        bin_dn[(int(math.floor(r["tws"])), int(math.floor(r["twa"] / 5) * 5))].append(r["bsp"])

    print(
        f"{'TWS':>5} {'samples':>8} {'med TWA':>7}  "
        f"{'OPTIMAL TWA':>22} {'opt VMG':>8} {'we sail':>14} {'gap':>6}"
    )
    print("-" * 88)
    for tws in range(4, 22):
        relevant = [r for r in rows_dn if int(math.floor(r["tws"])) == tws]
        if len(relevant) < 60:
            continue
        twas = sorted([r["twa"] for r in relevant])
        med_twa = twas[len(twas) // 2]
        cands = []
        for ab in range(95, 180, 5):
            v = bin_dn.get((tws, ab), [])
            if len(v) >= 60:
                m = statistics.mean(v)
                cands.append((ab, m, m * abs(math.cos(math.radians(ab + 2.5))), len(v)))
        if not cands:
            continue
        opt = max(cands, key=lambda c: c[2])
        med_b = int(math.floor(med_twa / 5) * 5)
        med_v = bin_dn.get((tws, med_b), [])
        med_vmg = (
            statistics.mean(med_v) * abs(math.cos(math.radians(med_b + 2.5)))
            if len(med_v) >= 30
            else float("nan")
        )
        print(
            f"{tws:>3}-{tws + 1:<2} {len(relevant):>8} {med_twa:>7.1f}  "
            f"{opt[0]:>4}° (n={opt[3]:>3}) BSP {opt[1]:.2f}  {opt[2]:>8.2f}  "
            f"{med_b}° ({med_vmg:.2f})  {opt[2] - med_vmg:>+6.2f}"
        )

    # ============================================================
    # 2. UPWIND POLAR (post-gun)
    # ============================================================
    print()
    print("=" * 90)
    print("UPWIND POLAR — races only, post-gun, RTTS excluded")
    print("=" * 90)
    bin_up = defaultdict(list)
    for r in rows_up:
        bin_up[(int(math.floor(r["tws"])), int(math.floor(r["twa"] / 5) * 5))].append(r["bsp"])

    print(
        f"{'TWS':>5} {'samples':>8} {'med TWA':>7}  "
        f"{'OPTIMAL TWA':>22} {'opt VMG':>8} {'we sail':>14} {'gap':>6}"
    )
    print("-" * 88)
    for tws in range(4, 22):
        relevant = [r for r in rows_up if int(math.floor(r["tws"])) == tws]
        if len(relevant) < 60:
            continue
        twas = sorted([r["twa"] for r in relevant])
        med_twa = twas[len(twas) // 2]
        cands = []
        for ab in range(30, 90, 5):
            v = bin_up.get((tws, ab), [])
            if len(v) >= 60:
                m = statistics.mean(v)
                cands.append((ab, m, m * abs(math.cos(math.radians(ab + 2.5))), len(v)))
        if not cands:
            continue
        opt = max(cands, key=lambda c: c[2])
        med_b = int(math.floor(med_twa / 5) * 5)
        med_v = bin_up.get((tws, med_b), [])
        med_vmg = (
            statistics.mean(med_v) * abs(math.cos(math.radians(med_b + 2.5)))
            if len(med_v) >= 30
            else float("nan")
        )
        print(
            f"{tws:>3}-{tws + 1:<2} {len(relevant):>8} {med_twa:>7.1f}  "
            f"{opt[0]:>4}° (n={opt[3]:>3}) BSP {opt[1]:.2f}  {opt[2]:>8.2f}  "
            f"{med_b}° ({med_vmg:.2f})  {opt[2] - med_vmg:>+6.2f}"
        )

    # ============================================================
    # 3. SHIFT-CATCHING (post-gun, races only)
    # ============================================================
    print()
    print("=" * 90)
    print("SHIFT-CATCHING (post-gun, races only)")
    print("=" * 90)
    classified = []
    shift_mags = []
    for race in races:
        start, _ = starts_info[race["id"]]
        end = race["end_utc"]
        # Tacks within the post-gun window
        tacks = list(
            conn.execute(
                "SELECT id, ts, duration_sec FROM maneuvers"
                " WHERE type='tack' AND session_id=? AND ts BETWEEN ? AND ?"
                " ORDER BY ts",
                (race["id"], start, end),
            )
        )
        for t in tacks:
            ts = datetime.fromisoformat(t["ts"])
            b_start = (ts - timedelta(seconds=150)).isoformat()
            b_end = (ts - timedelta(seconds=90)).isoformat()
            a_start = (ts + timedelta(seconds=90)).isoformat()
            a_end = (ts + timedelta(seconds=150)).isoformat()

            def median_twd(s, e):
                # Try ref=4 first
                rows = conn.execute(
                    "SELECT wind_angle_deg FROM winds WHERE reference=4 AND ts BETWEEN ? AND ?",
                    (s, e),
                ).fetchall()
                if rows:
                    return statistics.median([r["wind_angle_deg"] % 360 for r in rows])
                # Fall back to ref=0 + heading
                wb_rows = conn.execute(
                    "SELECT ts, wind_angle_deg FROM winds WHERE reference=0 AND ts BETWEEN ? AND ?",
                    (s, e),
                ).fetchall()
                twds = []
                for w in wb_rows:
                    hdg_row = conn.execute(
                        "SELECT heading_deg FROM headings WHERE ts >= ? AND ts < ? LIMIT 1",
                        (w["ts"][:19], w["ts"][:19] + ":99"),
                    ).fetchone()
                    if hdg_row is None:
                        continue
                    signed = ((w["wind_angle_deg"] + 180) % 360) - 180
                    twds.append((hdg_row["heading_deg"] + signed) % 360)
                return statistics.median(twds) if twds else None

            def median_hdg(s, e):
                rows = conn.execute(
                    "SELECT heading_deg FROM headings WHERE ts BETWEEN ? AND ?", (s, e)
                ).fetchall()
                return statistics.median([r["heading_deg"] for r in rows]) if rows else None

            twd_b = median_twd(b_start, b_end)
            twd_a = median_twd(a_start, a_end)
            hdg_b = median_hdg(b_start, b_end)
            hdg_a = median_hdg(a_start, a_end)
            if None in (twd_b, twd_a, hdg_b, hdg_a):
                continue
            hc = angle_diff(hdg_a, hdg_b)
            tc = angle_diff(twd_a, twd_b)
            if abs(hc) < 60 or abs(hc) > 180:
                continue
            shift_mags.append(abs(tc))
            if abs(tc) < 10:
                v = "neutral"
            elif (hc > 0 and tc < 0) or (hc < 0 and tc > 0):
                v = "good"
            else:
                v = "bad"
            classified.append(
                {
                    "race_id": race["id"],
                    "race_name": race["name"],
                    "ts": t["ts"],
                    "twd_change": tc,
                    "verdict": v,
                }
            )

    if classified:
        print(
            f"{len(classified)} tacks classified across "
            f"{len(set(c['race_id'] for c in classified))} races."
        )
        print(f"Median wind shift around tacks: {statistics.median(shift_mags):.1f}°")
        cnt = defaultdict(int)
        for c in classified:
            cnt[c["verdict"]] += 1
        tot = sum(cnt.values())
        print(f"  Good (tacked on header ≥10°): {cnt['good']:>3} ({100 * cnt['good'] / tot:.0f}%)")
        print(f"  Bad  (tacked on lift ≥10°):   {cnt['bad']:>3} ({100 * cnt['bad'] / tot:.0f}%)")
        print(
            f"  Neutral (shift <10°):         {cnt['neutral']:>3} ({100 * cnt['neutral'] / tot:.0f}%)"
        )
        sg = cnt["good"] + cnt["bad"]
        if sg > 0:
            print(f"  Hit rate (when there WAS a shift): {100 * cnt['good'] / sg:.0f}%")

    # ============================================================
    # 4. CURRENT — long sustained legs across ALL sessions (race + practice)
    # ============================================================
    print()
    print("=" * 90)
    print("CURRENT — derived from sustained steady-heading legs ≥60s, ANY session")
    print("Includes practice sessions (longer beats give cleaner current vectors).")
    print("=" * 90)
    print(f"{'date':<11} {'session':<40} {'legs':>4} {'leg-sec':>7} {'drift kt':>8} {'set °':>6}")
    print("-" * 80)
    current_per_session = []
    for s in all_sessions:
        rs = load_aligned(conn, s["start_utc"], s["end_utc"])
        # Detect steady legs
        all_xs, all_ys, total_secs, n_legs = [], [], 0, 0
        for i0, i1 in detect_steady_legs(rs):
            xs, ys = [], []
            for k in range(i0, i1):
                x = rs[k]
                if x["sog"] is None or x["cog"] is None or x["hdg"] is None or x["bsp"] is None:
                    continue
                cx = x["sog"] * math.sin(math.radians(x["cog"])) - x["bsp"] * math.sin(
                    math.radians(x["hdg"])
                )
                cy = x["sog"] * math.cos(math.radians(x["cog"])) - x["bsp"] * math.cos(
                    math.radians(x["hdg"])
                )
                if math.hypot(cx, cy) < 4.0:
                    xs.append(cx)
                    ys.append(cy)
            if len(xs) >= 60:
                # Average current vector for this leg
                lcx = sum(xs) / len(xs)
                lcy = sum(ys) / len(ys)
                # Each leg contributes its leg-mean weighted by duration
                all_xs.extend([lcx] * len(xs))
                all_ys.extend([lcy] * len(xs))
                total_secs += len(xs)
                n_legs += 1
        if n_legs == 0:
            continue
        mcx = sum(all_xs) / len(all_xs)
        mcy = sum(all_ys) / len(all_ys)
        drift = math.hypot(mcx, mcy)
        set_d = (math.degrees(math.atan2(mcx, mcy)) + 360) % 360
        current_per_session.append(
            {
                "id": s["id"],
                "name": s["name"],
                "date": s["date"],
                "type": s["session_type"],
                "n_legs": n_legs,
                "secs": total_secs,
                "drift": drift,
                "set": set_d,
            }
        )
        marker = "(P)" if s["session_type"] == "practice" else "   "
        print(
            f"{s['date']:<11} {marker} {s['name']:<37} {n_legs:>4} "
            f"{total_secs:>7} {drift:>8.2f} {set_d:>6.0f}"
        )
    print()
    print("(P) = practice session")

    # ============================================================
    # 5. PER-RACE SCORECARD (post-gun, races only)
    # ============================================================
    print()
    print("=" * 90)
    print("PER-RACE SCORECARD — downwind/upwind ≥90% of best-VMG, shift hit rate")
    print("(All metrics are post-gun; pre-start excluded.)")
    print("=" * 90)

    # Build optimum VMG per TWS for each mode
    opt_dn = {}
    for tws in range(4, 22):
        cands = []
        for ab in range(95, 180, 5):
            v = bin_dn.get((tws, ab), [])
            if len(v) >= 60:
                m = statistics.mean(v)
                cands.append(m * abs(math.cos(math.radians(ab + 2.5))))
        if cands:
            opt_dn[tws] = max(cands)
    opt_up = {}
    for tws in range(4, 22):
        cands = []
        for ab in range(30, 90, 5):
            v = bin_up.get((tws, ab), [])
            if len(v) >= 60:
                m = statistics.mean(v)
                cands.append(m * abs(math.cos(math.radians(ab + 2.5))))
        if cands:
            opt_up[tws] = max(cands)

    # Aggregate shifts per race
    shifts_per_race = defaultdict(lambda: {"good": 0, "bad": 0, "neutral": 0})
    for c in classified:
        shifts_per_race[c["race_id"]][c["verdict"]] += 1

    summary = []
    for r in races:
        rs = per_race_rows[r["id"]]
        dn = [x for x in rs if 90 <= x["twa"] <= 180 and x["bsp"] > 0.5 and 1.5 < x["tws"] < 35]
        up = [x for x in rs if 30 <= x["twa"] < 90 and x["bsp"] > 1.0 and 1.5 < x["tws"] < 35]
        if len(dn) < 60 or len(up) < 60:
            continue

        def pct(rows, opts):
            good = total = 0
            for x in rows:
                tws_b = int(math.floor(x["tws"]))
                opt = opts.get(tws_b)
                if opt is None or opt <= 0:
                    continue
                if abs(x["vmg"]) / opt >= 0.90:
                    good += 1
                total += 1
            return (100.0 * good / total) if total else None

        dn_pct = pct(dn, opt_dn)
        up_pct = pct(up, opt_up)
        sh = shifts_per_race.get(r["id"], {"good": 0, "bad": 0, "neutral": 0})
        avg_tws_dn = statistics.mean([x["tws"] for x in dn])
        avg_tws_up = statistics.mean([x["tws"] for x in up])
        summary.append(
            {
                "date": r["date"],
                "name": r["name"],
                "id": r["id"],
                "src": starts_info[r["id"]][1],
                "tws_up": avg_tws_up,
                "tws_dn": avg_tws_dn,
                "dn_pct": dn_pct,
                "up_pct": up_pct,
                "good": sh["good"],
                "bad": sh["bad"],
                "neut": sh["neutral"],
            }
        )

    summary.sort(key=lambda s: -((s["dn_pct"] or 0) + (s["up_pct"] or 0)) / 2)
    print(
        f"{'date':<11} {'race':<32} {'TWSu':>4} {'TWSd':>4} "
        f"{'up%':>4} {'dn%':>4} {'shifts g/b/n':>13} {'start':>7}"
    )
    print("-" * 92)
    for s in summary:
        src_short = "vk" if s["src"] == "vakaros" else "+5m"
        print(
            f"{s['date']:<11} {s['name']:<32} "
            f"{s['tws_up']:>4.1f} {s['tws_dn']:>4.1f} "
            f"{s['up_pct']:>3.0f}% {s['dn_pct']:>3.0f}% "
            f"  {s['good']:>2}/{s['bad']:>2}/{s['neut']:>2}      {src_short:>4}"
        )


if __name__ == "__main__":
    main()
