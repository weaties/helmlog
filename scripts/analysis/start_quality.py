"""Start quality matrix + trend chart.

For each race with Vakaros line endpoints + boat track + gun event, compute:

  - dist_at_gun_m   : signed perpendicular distance from line at the gun
                      (negative = behind the line / pre-start side; 0 = on the
                      line; positive = over the line at the gun)
  - speed_at_gun_kt : boat speed (SOG, GPS-based, calibration-immune) at gun
  - ttl_sec         : time from gun to first forward crossing of the line.
                      0 = crossed exactly at gun. Positive = late. For OCS
                      races where boat had to come back, this measures the
                      RE-cross.
  - score           : 0–100 composite (higher is better)

Generates start_quality_trend.png — a chart showing each component over the
season, plus a smoothed composite line.
"""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HELMLOG_DB", "data/logger.db")
OUT = os.environ.get("HELMLOG_OUT_DIR", ".") + "/start_quality_trend.png"

TARGET_SPEED_KT = 6.0  # J/105 upwind target start speed (rough)


def latlon_to_xy(lat, lon, ref_lat):
    R = 6371000.0
    x = math.radians(lon) * R * math.cos(math.radians(ref_lat))
    y = math.radians(lat) * R
    return x, y


def perp_distance_signed(boat_xy, line_a_xy, line_b_xy):
    """Signed perpendicular distance from boat to the line (in meters).

    Positive = on the side of the cross product +1.
    """
    abx = line_b_xy[0] - line_a_xy[0]
    aby = line_b_xy[1] - line_a_xy[1]
    apx = boat_xy[0] - line_a_xy[0]
    apy = boat_xy[1] - line_a_xy[1]
    cross = abx * apy - aby * apx
    line_len = math.hypot(abx, aby)
    if line_len < 1e-6:
        return 0.0
    return cross / line_len


def line_bias(boat_a_xy, pin_b_xy, twd_deg):
    """Compute line bias relative to the wind direction.

    Returns dict with:
      favored: 'boat', 'pin', or 'square' (within 2°)
      bias_deg: angle the line is tilted off square (positive = boat favored,
                negative = pin favored)
      bias_m:   linear advantage in meters (how much closer to upwind the
                favored end is)
      line_len_m: total length of the line
    """
    abx = pin_b_xy[0] - boat_a_xy[0]
    aby = pin_b_xy[1] - boat_a_xy[1]
    line_len = math.hypot(abx, aby)
    if line_len < 1e-6:
        return {"favored": "square", "bias_deg": 0.0, "bias_m": 0.0, "line_len_m": 0.0}

    # Line bearing from boat-end → pin-end (deg from N, increasing eastward)
    line_bearing = (math.degrees(math.atan2(abx, aby)) + 360) % 360

    # Wind FROM direction → upwind direction (where the boat wants to go)
    upwind_bearing = twd_deg % 360

    # Angle between line and the perpendicular to wind:
    #   if line is perpendicular to wind, bias = 0 (square)
    #   if line tilts so the boat-end is closer to upwind, bias > 0
    # Compute the signed difference between line_bearing and (upwind_bearing + 90).
    # The +90 makes the reference axis perpendicular to wind direction.
    perpendicular_to_wind = (upwind_bearing + 90) % 360
    diff = (line_bearing - perpendicular_to_wind + 540) % 360 - 180  # (-180, 180]

    # The bias angle measures how much the line tilts off square.
    # Convention: positive bias_deg = boat-end is more upwind = boat favored
    # Negative = pin favored
    # When line is perpendicular to wind (square), diff is ±0 or ±180.
    # When line is parallel to wind, diff is ±90.
    if abs(diff) > 90:
        # Line vector is mostly opposite our reference direction; flip sign
        bias_deg = (180 - abs(diff)) * (-1 if diff > 0 else 1)
    else:
        bias_deg = -diff  # positive = boat favored

    # Linear advantage at favored end (meters along the line that the
    # favored end is "ahead" in upwind terms)
    bias_m = line_len * math.sin(math.radians(abs(bias_deg)))
    if bias_deg < 0:
        bias_m = -bias_m

    if abs(bias_deg) < 2.0:
        favored = "square"
    elif bias_deg > 0:
        favored = "boat"
    else:
        favored = "pin"

    return {
        "favored": favored,
        "bias_deg": bias_deg,
        "bias_m": bias_m,
        "line_len_m": line_len,
    }


def boat_position_along_line(boat_xy, line_a_xy, line_b_xy):
    """Return position along line as 0..1 (0 = boat-end, 1 = pin-end).

    Negative = past boat-end; >1 = past pin-end.
    """
    abx = line_b_xy[0] - line_a_xy[0]
    aby = line_b_xy[1] - line_a_xy[1]
    apx = boat_xy[0] - line_a_xy[0]
    apy = boat_xy[1] - line_a_xy[1]
    line_len_sq = abx * abx + aby * aby
    if line_len_sq < 1e-6:
        return 0.0
    return (apx * abx + apy * aby) / line_len_sq


def get_line_endpoints(conn, vk_session_id, gun_iso):
    boat = conn.execute(
        "SELECT latitude_deg, longitude_deg FROM vakaros_line_positions"
        " WHERE session_id = ? AND line_type = 'boat' AND ts <= ?"
        " ORDER BY ts DESC LIMIT 1",
        (vk_session_id, gun_iso),
    ).fetchone()
    pin = conn.execute(
        "SELECT latitude_deg, longitude_deg FROM vakaros_line_positions"
        " WHERE session_id = ? AND line_type = 'pin' AND ts <= ?"
        " ORDER BY ts DESC LIMIT 1",
        (vk_session_id, gun_iso),
    ).fetchone()
    if boat is None or pin is None:
        return None
    return (boat["latitude_deg"], boat["longitude_deg"], pin["latitude_deg"], pin["longitude_deg"])


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

    # OCS-tagged races. We pull from the entity_tags table when the tag is
    # present in this DB, then union with a hard-coded fallback so the script
    # is correct even when running against an older backup that doesn't have
    # the tag yet (the tags were added on corvopi-live after the 5/12 backup).
    ocs_ids = {
        r["entity_id"]
        for r in conn.execute(
            "SELECT et.entity_id FROM entity_tags et JOIN tags t ON t.id=et.tag_id"
            " WHERE t.name='ocs' AND et.entity_type='session'"
        )
    } | {35, 55}  # 4/14 BC1, 4/19 PSSR-2

    # Load finishes (Corvo's place per logged-race-id, via local_session_id link)
    finishes = {}
    for r in conn.execute(
        "SELECT r.local_session_id AS log_id, rr.place"
        " FROM race_results rr"
        " JOIN races r ON r.id = rr.race_id"
        " WHERE rr.boat_id = 47 AND r.local_session_id IS NOT NULL"
    ):
        finishes[r["log_id"]] = r["place"]

    rows = []
    for r in races:
        vk = r["vakaros_session_id"]
        if vk is None:
            continue
        gun = conn.execute(
            "SELECT ts FROM vakaros_race_events WHERE session_id=?"
            " AND event_type='race_start' AND ts BETWEEN ? AND ?"
            " ORDER BY ts DESC LIMIT 1",
            (vk, r["start_utc"], r["end_utc"]),
        ).fetchone()
        if gun is None:
            continue
        gun_ts = gun["ts"]
        gun_dt = datetime.fromisoformat(gun_ts)
        line = get_line_endpoints(conn, vk, gun_ts)
        if line is None:
            continue
        b_lat, b_lon, p_lat, p_lon = line
        ref_lat = (b_lat + p_lat) / 2
        a_xy = latlon_to_xy(b_lat, b_lon, ref_lat)
        b_xy = latlon_to_xy(p_lat, p_lon, ref_lat)

        # Boat positions: gun-240 (4 min before) to gun+300 (5 min after)
        # — the wider window lets us track the pre-start setup
        win_start = (gun_dt - timedelta(seconds=240)).isoformat()
        win_end = (gun_dt + timedelta(seconds=300)).isoformat()
        cur = conn.execute(
            "SELECT substr(ts,1,19) AS k, AVG(latitude_deg) AS la,"
            " AVG(longitude_deg) AS lo FROM positions"
            " WHERE ts BETWEEN ? AND ? GROUP BY k ORDER BY k",
            (win_start, win_end),
        )
        positions = list(cur)
        if len(positions) < 30:
            continue

        # SOG at gun (avg ±5 sec)
        sog_cur = conn.execute(
            "SELECT AVG(sog_kts) FROM cogsog WHERE ts BETWEEN ? AND ?",
            (
                (gun_dt - timedelta(seconds=5)).isoformat(),
                (gun_dt + timedelta(seconds=5)).isoformat(),
            ),
        )
        sog_at_gun = sog_cur.fetchone()[0]

        # Tack at gun: derived from signed TWA (boat-relative wind angle).
        # Sign convention: positive = wind from starboard side = starboard tack.
        tack_at_gun = None
        twa_rows = list(
            conn.execute(
                "SELECT wind_angle_deg FROM winds WHERE reference=0 AND ts BETWEEN ? AND ?",
                (
                    (gun_dt - timedelta(seconds=10)).isoformat(),
                    (gun_dt + timedelta(seconds=10)).isoformat(),
                ),
            )
        )
        if twa_rows:
            # Each wind_angle_deg is the boat-referenced TWA (in [0, 360)
            # where 0 = head to wind, 90 = wind from starboard, 180 = dead
            # downwind, 270 = wind from port). Convert to signed (-180, 180]:
            #   positive (0–180) = starboard, negative (-180–0) = port.
            signed_twas = []
            for tw in twa_rows:
                signed = ((tw[0] + 180) % 360) - 180  # → (-180, 180]
                signed_twas.append(signed)
            avg_signed = statistics.mean(signed_twas)
            tack_at_gun = "stbd" if avg_signed >= 0 else "port"

        # TWD at gun (median over 60 sec before to 0 sec at gun). Try ref=4
        # (north-referenced TWD) first, fall back to ref=0 (boat-relative TWA)
        # + heading.
        twd_at_gun = None
        twd_window_start = (gun_dt - timedelta(seconds=60)).isoformat()
        twd_window_end = gun_dt.isoformat()
        twd_rows = list(
            conn.execute(
                "SELECT wind_angle_deg FROM winds WHERE reference=4 AND ts BETWEEN ? AND ?",
                (twd_window_start, twd_window_end),
            )
        )
        if twd_rows:
            twd_at_gun = statistics.median([r[0] % 360 for r in twd_rows])
        else:
            # Fall back: ref=0 + heading per second
            twds = []
            for w in conn.execute(
                "SELECT ts, wind_angle_deg FROM winds WHERE reference=0 AND ts BETWEEN ? AND ?",
                (twd_window_start, twd_window_end),
            ):
                hdg_row = conn.execute(
                    "SELECT heading_deg FROM headings WHERE ts >= ? AND ts < ? LIMIT 1",
                    (w[0][:19], w[0][:19] + ":99"),
                ).fetchone()
                if hdg_row is None:
                    continue
                signed = ((w[1] + 180) % 360) - 180
                twds.append((hdg_row[0] + signed) % 360)
            if twds:
                twd_at_gun = statistics.median(twds)

        # Favored-end analysis
        bias_info = None
        boat_pos_along = None
        if twd_at_gun is not None:
            bias_info = line_bias(a_xy, b_xy, twd_at_gun)
            # Where on the line did Corvo start? (use boat position at gun)
            for p in positions:
                t = datetime.fromisoformat(p["k"]).replace(tzinfo=gun_dt.tzinfo)
                if abs((t - gun_dt).total_seconds()) <= 1:
                    xy = latlon_to_xy(p["la"], p["lo"], ref_lat)
                    boat_pos_along = boat_position_along_line(xy, a_xy, b_xy)
                    break

        # Compute pre-start side reference (median 60-10 sec before gun)
        sides_pre = []
        for p in positions:
            t = datetime.fromisoformat(p["k"]).replace(tzinfo=gun_dt.tzinfo)
            if gun_dt - timedelta(seconds=60) <= t <= gun_dt - timedelta(seconds=10):
                xy = latlon_to_xy(p["la"], p["lo"], ref_lat)
                d = perp_distance_signed(xy, a_xy, b_xy)
                if abs(d) > 0.5:
                    sides_pre.append(1 if d > 0 else -1)
        if not sides_pre:
            continue
        pre_side = 1 if sum(sides_pre) > 0 else -1

        # Distance at gun (signed in pre-start frame: negative = behind line, positive = over)
        d_at_gun_signed = None
        for p in positions:
            t = datetime.fromisoformat(p["k"]).replace(tzinfo=gun_dt.tzinfo)
            if abs((t - gun_dt).total_seconds()) <= 1:
                xy = latlon_to_xy(p["la"], p["lo"], ref_lat)
                d = perp_distance_signed(xy, a_xy, b_xy)
                # Flip so that negative = pre-start side, positive = race side
                d_at_gun_signed = d * pre_side * -1  # now: positive = OVER, negative = behind
                break
        if d_at_gun_signed is None:
            continue

        # Time-to-cross: first time after gun when boat is on race side AND stays
        # there for ≥5 seconds (to ignore brief GPS noise). For OCS-returned
        # races, we measure the RE-cross (after the boat goes back to clear).
        is_ocs = r["id"] in ocs_ids
        ttl_sec = None
        race_side_streak = 0
        seen_prestart_after_gun = False
        for p in positions:
            t = datetime.fromisoformat(p["k"]).replace(tzinfo=gun_dt.tzinfo)
            if t < gun_dt:
                continue
            xy = latlon_to_xy(p["la"], p["lo"], ref_lat)
            d = perp_distance_signed(xy, a_xy, b_xy) * pre_side * -1
            if d > 0:
                race_side_streak += 1
                if race_side_streak >= 5 and ttl_sec is None:
                    # First sustained race-side streak — back-fill to first sec
                    candidate_ttl = (t - gun_dt).total_seconds() - 4
                    if candidate_ttl < 0:
                        candidate_ttl = 0
                    # For OCS races, only accept this as TTL once we've seen the
                    # boat on the pre-start side after the gun (the return).
                    if (not is_ocs) or seen_prestart_after_gun:
                        ttl_sec = candidate_ttl
            else:
                race_side_streak = 0
                seen_prestart_after_gun = True
        if ttl_sec is None:
            ttl_sec = 999  # never crossed in window

        # Composite score (0-100)
        # Distance: 0 = perfect (right at line). Penalty for behind, cap at -50m.
        # Over the line is bad too, cap at +5m for "barely on the line"
        if d_at_gun_signed > 0:
            d_score = max(0, 50 - 10 * d_at_gun_signed)  # 5m over → 0 pts
        else:
            d_score = max(0, 50 + 1.0 * d_at_gun_signed)  # 50m behind → 0 pts
        # Speed: target 6 kt, penalty for slow
        spd = sog_at_gun if sog_at_gun else 0
        s_score = max(0, 30 - 4 * abs(spd - TARGET_SPEED_KT))
        # Time to cross: 0 best, 30s ok, 60s+ bad
        if ttl_sec >= 999:
            t_score = 0
        else:
            t_score = max(0, 20 - 0.4 * ttl_sec)
        score = d_score + s_score + t_score
        # OCS penalty (already shows up in TTL but make it explicit)
        if r["id"] in ocs_ids:
            score *= 0.5

        # Sanity filter: if distance at gun is > 100 m the line endpoints are
        # almost certainly stale (line moved between races and we don't have
        # the new pin coords). Skip rather than report a bogus score.
        if abs(d_at_gun_signed) > 100:
            print(
                f"  -- skipping {r['name']}: |dist@gun|={abs(d_at_gun_signed):.0f}m → stale line data"
            )
            continue

        # Determine where Corvo started on the line (boat-end / mid / pin-end)
        if boat_pos_along is None:
            corvo_end = "?"
        elif boat_pos_along < 0.33:
            corvo_end = "boat"
        elif boat_pos_along > 0.67:
            corvo_end = "pin"
        else:
            corvo_end = "mid"

        # ---- Pre-start positioning trajectory ----
        # Sample boat position-along-line at gun-180s, gun-120s, gun-60s, gun
        # to see whether the team "drifted" to the boat-end (habit) or stayed
        # near where they were positioned during the start sequence.
        def pos_along_at(target_dt):
            """Return position-along-line (0..1) at target_dt, or None."""
            best = None
            best_dt = None
            for p in positions:
                t = datetime.fromisoformat(p["k"]).replace(tzinfo=gun_dt.tzinfo)
                dt_diff = abs((t - target_dt).total_seconds())
                if dt_diff <= 5 and (best_dt is None or dt_diff < best_dt):
                    xy = latlon_to_xy(p["la"], p["lo"], ref_lat)
                    best = boat_position_along_line(xy, a_xy, b_xy)
                    best_dt = dt_diff
            return best

        pos_at_3min = pos_along_at(gun_dt - timedelta(seconds=180))
        pos_at_2min = pos_along_at(gun_dt - timedelta(seconds=120))
        pos_at_1min = pos_along_at(gun_dt - timedelta(seconds=60))
        pos_at_gun = boat_pos_along

        # Classify the trajectory
        # We mostly care about: did the team move TOWARD the boat-end (0.0)
        # during the last 3 min? And was there a pre-start position?
        traj_drift = None  # how much position changed from gun-3min to gun
        if pos_at_3min is not None and pos_at_gun is not None:
            traj_drift = pos_at_gun - pos_at_3min
            # negative = drifted toward boat-end, positive = drifted toward pin

        # Did Corvo start at the favored end?
        if bias_info is None:
            got_favored = None
        elif bias_info["favored"] == "square":
            got_favored = "—"  # no end favored, doesn't matter
        elif bias_info["favored"] == corvo_end:
            got_favored = "✓"
        elif corvo_end == "mid":
            # Mid-line on a biased line — half a boat length lost
            got_favored = "~"
        else:
            got_favored = "✗"

        rows.append(
            {
                "id": r["id"],
                "date": r["date"],
                "name": r["name"],
                "gun_dt": gun_dt,
                "d_at_gun_signed": d_at_gun_signed,
                "sog_at_gun": spd,
                "ttl_sec": ttl_sec,
                "score": min(100, score),
                "ocs": r["id"] in ocs_ids,
                "place": finishes.get(r["id"]),
                "twd_at_gun": twd_at_gun,
                "favored_end": bias_info["favored"] if bias_info else "?",
                "bias_deg": bias_info["bias_deg"] if bias_info else 0.0,
                "bias_m": bias_info["bias_m"] if bias_info else 0.0,
                "line_len_m": bias_info["line_len_m"] if bias_info else 0.0,
                "corvo_end": corvo_end,
                "got_favored": got_favored,
                "pos_at_3min": pos_at_3min,
                "pos_at_2min": pos_at_2min,
                "pos_at_1min": pos_at_1min,
                "pos_at_gun": pos_at_gun,
                "traj_drift": traj_drift,
                "tack_at_gun": tack_at_gun,
            }
        )

    # ============================================================
    # Print matrix
    # ============================================================
    print("=" * 140)
    print("START QUALITY MATRIX — sorted chronologically")
    print("=" * 140)
    print(
        f"{'date':<11} {'race':<30} {'dist@gun':>10} {'SOG':>5} "
        f"{'TTL':>6} {'score':>6} {'fav':>5} {'bias°':>6} {'corvo':>6} "
        f"{'got?':>5} {'tack':>5} {'place':>5} {'ocs':>4}"
    )
    print(f"{'':11} {'':30} {'(m)':>10} {'(kt)':>5} {'(s)':>6}")
    print("-" * 140)
    for row in sorted(rows, key=lambda x: x["gun_dt"]):
        d_str = f"{row['d_at_gun_signed']:+.1f}"
        if row["ttl_sec"] >= 999:
            ttl_str = "—"
        else:
            ttl_str = f"{row['ttl_sec']:.0f}"
        ocs_str = "OCS" if row["ocs"] else ""
        place_str = str(row["place"]) if row["place"] else "—"
        bias_str = f"{row['bias_deg']:+.0f}" if row["bias_deg"] else "0"
        print(
            f"{row['date']:<11} {row['name'][:28]:<30} {d_str:>10} "
            f"{row['sog_at_gun']:>5.2f} {ttl_str:>6} "
            f"{row['score']:>6.0f} {row['favored_end']:>5} {bias_str:>6} "
            f"{row['corvo_end']:>6} {row['got_favored'] or '?':>5} "
            f"{(row.get('tack_at_gun') or '?'):>5} "
            f"{place_str:>5} {ocs_str:>4}"
        )

    # Tack vs corvo_end cross-tab
    print()
    print("--- Tack at gun vs end of line chosen ---")
    cross = defaultdict(int)
    for row in rows:
        t = row.get("tack_at_gun") or "?"
        c = row.get("corvo_end") or "?"
        cross[(t, c)] += 1
    print(f"{'tack':>8} {'boat':>6} {'mid':>6} {'pin':>6}")
    for tack in ("stbd", "port"):
        b = cross.get((tack, "boat"), 0)
        m = cross.get((tack, "mid"), 0)
        p = cross.get((tack, "pin"), 0)
        print(f"{tack:>8} {b:>6} {m:>6} {p:>6}")

    # ============================================================
    # Plot trend
    # ============================================================
    rows.sort(key=lambda x: x["gun_dt"])
    dates = [r["gun_dt"] for r in rows]
    scores = [r["score"] for r in rows]
    dists = [r["d_at_gun_signed"] for r in rows]
    sogs = [r["sog_at_gun"] for r in rows]
    ttls = [min(r["ttl_sec"], 60) for r in rows]
    ocs_mask = [r["ocs"] for r in rows]

    fig, axes = plt.subplots(4, 1, figsize=(11, 11), sharex=True)

    # Score
    ax = axes[0]
    colors = ["red" if o else "tab:blue" for o in ocs_mask]
    ax.scatter(dates, scores, c=colors, s=80, zorder=3)
    ax.plot(dates, scores, color="tab:blue", alpha=0.4, zorder=2)
    # Trend line (linear regression)
    if len(dates) >= 3:
        x_num = mdates.date2num(dates)
        m, b = (
            lambda x, y: (
                (
                    sum((xi - sum(x) / len(x)) * (yi - sum(y) / len(y)) for xi, yi in zip(x, y))
                    / max(1e-6, sum((xi - sum(x) / len(x)) ** 2 for xi in x))
                ),
                sum(y) / len(y)
                - (
                    sum((xi - sum(x) / len(x)) * (yi - sum(y) / len(y)) for xi, yi in zip(x, y))
                    / max(1e-6, sum((xi - sum(x) / len(x)) ** 2 for xi in x))
                )
                * (sum(x) / len(x)),
            )
        )(x_num, scores)
        trend = [m * xi + b for xi in x_num]
        ax.plot(
            dates, trend, "g--", alpha=0.6, label=f"trend ({'improving' if m > 0 else 'declining'})"
        )
        ax.legend(loc="lower right", fontsize=9)
    ax.set_ylabel("Composite\nstart score")
    ax.set_ylim(0, 100)
    ax.grid(True, alpha=0.3)
    ax.set_title(
        "Start quality over the season — Corvo 105\n"
        "Red dots = OCS races (composite score halved as penalty)",
        fontsize=11,
    )

    # Distance at gun
    ax = axes[1]
    ax.scatter(dates, dists, c=colors, s=80, zorder=3)
    ax.plot(dates, dists, color="tab:orange", alpha=0.4, zorder=2)
    ax.axhline(y=0, color="black", linestyle="--", alpha=0.4, label="on the line")
    ax.fill_between(
        dates,
        [-100] * len(dates),
        [0] * len(dates),
        color="tab:blue",
        alpha=0.05,
        label="behind line",
    )
    ax.fill_between(
        dates, [0] * len(dates), [100] * len(dates), color="tab:red", alpha=0.05, label="over line"
    )
    ax.set_ylabel("Distance from line\nat gun (m)")
    ax.set_ylim(-50, 30)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right", fontsize=8)

    # Speed at gun
    ax = axes[2]
    ax.scatter(dates, sogs, c=colors, s=80, zorder=3)
    ax.plot(dates, sogs, color="tab:green", alpha=0.4, zorder=2)
    ax.axhline(
        y=TARGET_SPEED_KT,
        color="black",
        linestyle="--",
        alpha=0.4,
        label=f"target {TARGET_SPEED_KT} kt",
    )
    ax.set_ylabel("SOG at gun (kt)")
    ax.set_ylim(0, 9)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right", fontsize=8)

    # TTL
    ax = axes[3]
    ax.scatter(dates, ttls, c=colors, s=80, zorder=3)
    ax.plot(dates, ttls, color="tab:purple", alpha=0.4, zorder=2)
    ax.set_ylabel("Time to clear line\nafter gun (s, capped at 60)")
    ax.set_ylim(0, 65)
    ax.grid(True, alpha=0.3)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(OUT, dpi=140, bbox_inches="tight")
    print(f"\nWrote {OUT}")

    # ============================================================
    # Pre-start positioning trajectory (habit vs opportunistic)
    # ============================================================
    print()
    print("=" * 100)
    print("PRE-START TRAJECTORY — boat position-along-line over time (0=boat-end, 1=pin-end)")
    print("=" * 100)
    print(
        f"{'date':<11} {'race':<32} {'gun-3m':>7} {'gun-2m':>7} {'gun-1m':>7} {'gun':>7}"
        f"  {'drift':>7} {'fav':>5} {'final':>6}"
    )
    print("-" * 95)
    drifts = []
    starts_at_3min = []
    finals_at_gun = []
    for row in sorted(rows, key=lambda x: x["gun_dt"]):

        def fmt(p):
            if p is None:
                return "  —  "
            return f"{p:+.2f}"

        drift_str = ""
        if row["traj_drift"] is not None:
            d = row["traj_drift"]
            if abs(d) < 0.10:
                drift_str = "stable"
            elif d < 0:
                drift_str = "→boat"
            else:
                drift_str = "→pin"
            drifts.append(d)
        if row["pos_at_3min"] is not None:
            starts_at_3min.append(row["pos_at_3min"])
        if row["pos_at_gun"] is not None:
            finals_at_gun.append(row["pos_at_gun"])
        print(
            f"{row['date']:<11} {row['name'][:30]:<32} "
            f"{fmt(row['pos_at_3min']):>7} {fmt(row['pos_at_2min']):>7} "
            f"{fmt(row['pos_at_1min']):>7} {fmt(row['pos_at_gun']):>7}  "
            f"{drift_str:>7} {row['favored_end']:>5} {row['corvo_end']:>6}"
        )

    print()
    print("-- Trajectory summary --")
    if drifts:
        print(
            f"  Mean drift gun-3m → gun: {statistics.mean(drifts):+.2f}  "
            f"(negative = drifted toward boat-end)"
        )
        toward_boat = sum(1 for d in drifts if d < -0.10)
        toward_pin = sum(1 for d in drifts if d > 0.10)
        stable = sum(1 for d in drifts if abs(d) <= 0.10)
        print(f"  Drifted toward boat-end: {toward_boat}")
        print(f"  Drifted toward pin-end:  {toward_pin}")
        print(f"  Stayed stable:           {stable}")
    if starts_at_3min:
        boat_at_3min = sum(1 for p in starts_at_3min if p < 0.33)
        pin_at_3min = sum(1 for p in starts_at_3min if p > 0.67)
        mid_at_3min = len(starts_at_3min) - boat_at_3min - pin_at_3min
        print(f"\n  At gun-3min: boat-end={boat_at_3min}, mid={mid_at_3min}, pin-end={pin_at_3min}")
    if finals_at_gun:
        boat_at_gun = sum(1 for p in finals_at_gun if p < 0.33)
        pin_at_gun = sum(1 for p in finals_at_gun if p > 0.67)
        mid_at_gun = len(finals_at_gun) - boat_at_gun - pin_at_gun
        print(f"  At gun:      boat-end={boat_at_gun}, mid={mid_at_gun}, pin-end={pin_at_gun}")

    print()
    print("Verdict logic:")
    print("  - If most races show 'stable' drift AND start_pos correlates with final_pos:")
    print("      → opportunistic (final position reflects where the boat happened to be)")
    print("  - If most races show 'drifted toward boat-end' AND finals cluster at boat-end")
    print("    regardless of starting position: → habit / boat-end preference")


if __name__ == "__main__":
    main()
