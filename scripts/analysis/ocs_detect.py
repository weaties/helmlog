"""Detect OCS (over-the-line at the gun, returned to clear) races.

Method per race:
  1. Find the gun (vakaros race_start event) and the most recent start-line
     endpoints (boat + pin) at or before the gun.
  2. Establish the "pre-start side" of the line by averaging the boat's
     side-of-line during the 60–10 sec window BEFORE the gun.
  3. Determine the boat's side AT the gun (avg over ±2 sec).
  4. If at-gun side != pre-start side → boat was over early. Then check the
     post-gun window (gun → gun+300s) for a return to the pre-start side.
  5. Classify:
       - clean        → at-gun on pre-start side
       - over_clear   → at-gun on race side AND returned to pre-start side
       - over_no_clear→ at-gun on race side BUT no documented return (could
                        be wrong, could be DSQ, could be race officials called
                        them back later — flag for review)
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


# ---------------------------------------------------------------------------
# Geometry helpers — treat lat/lon as planar over short distances
# ---------------------------------------------------------------------------


def latlon_to_xy(lat, lon, ref_lat):
    """Approx local meters from reference latitude (Mercator-ish)."""
    R = 6371000.0
    x = math.radians(lon) * R * math.cos(math.radians(ref_lat))
    y = math.radians(lat) * R
    return x, y


def side_of_line(boat_xy, line_a_xy, line_b_xy):
    """Cross product sign — +1, -1, or 0 (on the line)."""
    abx = line_b_xy[0] - line_a_xy[0]
    aby = line_b_xy[1] - line_a_xy[1]
    apx = boat_xy[0] - line_a_xy[0]
    apy = boat_xy[1] - line_a_xy[1]
    cross = abx * apy - aby * apx
    if cross > 0:
        return 1
    if cross < 0:
        return -1
    return 0


def get_line_endpoints(conn, vk_session_id, gun_iso):
    """Return (boat_lat, boat_lon, pin_lat, pin_lon) at or before gun_iso.

    Picks the most recent ping for each end_kind. None if missing.
    """
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


def positions_in_range(conn, start_iso, end_iso):
    """Yield (ts, lat, lon) per second within range."""
    cur = conn.execute(
        "SELECT substr(ts,1,19) AS k, AVG(latitude_deg) AS la,"
        " AVG(longitude_deg) AS lo FROM positions"
        " WHERE ts BETWEEN ? AND ? GROUP BY k ORDER BY k",
        (start_iso, end_iso),
    )
    return [(r["k"], r["la"], r["lo"]) for r in cur]


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # All races (race + practice for context, but only RACE matters for OCS)
    races = list(
        conn.execute(
            "SELECT id, name, date, session_type, start_utc, end_utc, vakaros_session_id"
            " FROM races"
            " WHERE end_utc IS NOT NULL AND start_utc != end_utc"
            "   AND session_type='race' AND name NOT LIKE '%RTTS%'"
            " ORDER BY start_utc"
        )
    )

    print("=" * 90)
    print("OCS DETECTION — pre-start side determined from boat track 60–10 sec before gun")
    print("=" * 90)
    print(
        f"{'date':<11} {'race':<35} {'gun':<10} {'pre':>4} {'gun':>4} "
        f"{'returned?':>10} {'verdict':<20}"
    )
    print("-" * 100)

    classifications = {}

    for r in races:
        vk = r["vakaros_session_id"]
        if vk is None:
            continue

        # Get gun
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

        # Line endpoints
        line = get_line_endpoints(conn, vk, gun_ts)
        if line is None:
            continue
        boat_lat, boat_lon, pin_lat, pin_lon = line
        ref_lat = (boat_lat + pin_lat) / 2

        # Convert line endpoints to xy
        a_xy = latlon_to_xy(boat_lat, boat_lon, ref_lat)
        b_xy = latlon_to_xy(pin_lat, pin_lon, ref_lat)

        # Boat positions: 90 sec before gun → 300 sec after gun
        win_start = (gun_dt - timedelta(seconds=90)).isoformat()
        win_end = (gun_dt + timedelta(seconds=300)).isoformat()
        positions = positions_in_range(conn, win_start, win_end)
        if len(positions) < 30:
            continue

        # Side per second
        sides = []
        for ts, lat, lon in positions:
            xy = latlon_to_xy(lat, lon, ref_lat)
            sides.append((ts, side_of_line(xy, a_xy, b_xy)))

        # Pre-start side: median side over t = gun-60 to gun-10
        pre_window_start = gun_dt - timedelta(seconds=60)
        pre_window_end = gun_dt - timedelta(seconds=10)
        pre_sides = [
            s
            for ts, s in sides
            if pre_window_start
            <= datetime.fromisoformat(ts).replace(tzinfo=gun_dt.tzinfo)
            <= pre_window_end
            and s != 0
        ]
        if len(pre_sides) < 5:
            continue
        pre_side = 1 if sum(pre_sides) > 0 else -1

        # At-gun side: median over gun ± 3 sec
        atgun_window_start = gun_dt - timedelta(seconds=3)
        atgun_window_end = gun_dt + timedelta(seconds=3)
        atgun_sides = [
            s
            for ts, s in sides
            if atgun_window_start
            <= datetime.fromisoformat(ts).replace(tzinfo=gun_dt.tzinfo)
            <= atgun_window_end
            and s != 0
        ]
        if len(atgun_sides) < 2:
            continue
        atgun_side = 1 if sum(atgun_sides) > 0 else -1

        # Post-gun: did the boat return to pre-start side any time in [gun+5, gun+300]?
        returned = False
        for ts, s in sides:
            t = datetime.fromisoformat(ts).replace(tzinfo=gun_dt.tzinfo)
            if t > gun_dt + timedelta(seconds=5) and t < gun_dt + timedelta(seconds=300):
                if s == pre_side:
                    returned = True
                    break

        # Classification
        if pre_side == atgun_side:
            verdict = "clean"
        elif returned:
            verdict = "OCS — RETURNED"
        else:
            verdict = "OCS? (no return)"

        classifications[r["id"]] = {
            "name": r["name"],
            "date": r["date"],
            "verdict": verdict,
            "pre_side": pre_side,
            "atgun_side": atgun_side,
            "returned": returned,
        }

        gun_local = gun_ts[11:16]
        print(
            f"{r['date']:<11} {r['name'][:33]:<35} {gun_local:<10} "
            f"{pre_side:>4} {atgun_side:>4} {('Y' if returned else '-'):>10} {verdict:<20}"
        )

    # Summary
    print()
    ocs_returned = [r for r in classifications.values() if r["verdict"] == "OCS — RETURNED"]
    ocs_no_clear = [r for r in classifications.values() if r["verdict"] == "OCS? (no return)"]
    clean = [r for r in classifications.values() if r["verdict"] == "clean"]
    print(f"{'CLASSIFIED':>20}: {len(classifications)}")
    print(f"{'CLEAN STARTS':>20}: {len(clean)}")
    print(f"{'OCS — returned':>20}: {len(ocs_returned)}")
    print(f"{'OCS? no return':>20}: {len(ocs_no_clear)}")

    print()
    print("OCS races (returned to pre-start side after gun):")
    for r in ocs_returned:
        print(f"  - {r['date']}  {r['name']}")
    if ocs_no_clear:
        print()
        print("Possible OCS but no clear return — review manually:")
        for r in ocs_no_clear:
            print(f"  - {r['date']}  {r['name']}")


if __name__ == "__main__":
    main()
