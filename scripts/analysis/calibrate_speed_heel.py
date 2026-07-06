"""Fit the heel-dependent STW correction coefficients (#810).

Corvo's paddlewheel is offset ~10 cm to port, so under heel it over-reads and
the error is tack-dependent. This script fits the read-time model applied by
``helmlog.speed_cal.corrected_stw``:

    k(heel) = a + b * heel_signed          # heel_signed = attitudes.heel_deg,
                                           # +ve = starboard-down = port tack
    corrected_STW = raw_STW / k(heel)

Method
------
For each completed race, the true water current is estimated from steady legs
(heading stable, BSP > threshold) by the same least-squares vector average
``full_analysis.py`` uses — averaging over both tacks lets the paddlewheel's
tack-dependent error largely cancel, leaving a clean current vector. Removing
that current from the GPS ground vector gives GPS-through-water, and the ratio

    k_observed = paddlewheel_STW / GPS-through-water

is regressed on signed heel to recover ``a`` (intercept) and ``b`` (slope).

The heel-linear fit is reported on a train/holdout split with R² and a sign
check. In practice the live data shows the tack bias *reverses sign* with wind
speed, so the heel slope fits poorly (low R²); the script therefore also emits
a **TWS × tack table** (per-(TWS bin, tack) mean ``k``) plus a suggested
**breeze gate** — the model the data actually supports.

Usage
-----
    uv run python scripts/analysis/calibrate_speed_heel.py [path/to/logger.db]

Writes nothing; prints the fit. Persist the results:
  * ``speed_cal_table`` (JSON) → app_settings,
  * ``speed_cal_gate_min_tws`` → boat-settings,
  * optionally ``speed_cal_base`` / ``speed_cal_heel_slope`` → boat-settings.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HELMLOG_DB", "data/logger.db")

# Steady-leg gates (mirror full_analysis.py so the current estimate matches).
MIN_DURATION_S = 60
MAX_HDG_SPREAD = 15.0
MIN_BSP = 2.0
# Drop implausible per-sample current vectors before averaging (kt).
MAX_CURRENT_KT = 4.0
# GPS-through-water below this is too slow to trust the ratio (kt).
MIN_GPS_STW = 3.0


def angle_diff(a: float, b: float) -> float:
    return (a - b + 540) % 360 - 180


def hdg_spread(values: list[float]) -> float:
    """Peak-to-peak heading spread, wrap-aware."""
    if len(values) < 2:
        return 0.0
    xs = [math.sin(math.radians(v)) for v in values]
    ys = [math.cos(math.radians(v)) for v in values]
    mean_dir = math.degrees(math.atan2(sum(xs) / len(xs), sum(ys) / len(ys)))
    deltas = [abs(angle_diff(v, mean_dir)) for v in values]
    return 2 * max(deltas)


def load_aligned_with_heel(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """Per-second aligned BSP / SOG / COG / HDG / heel over [start, end]."""
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
    hl = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT substr(ts,1,19), AVG(heel_deg) FROM attitudes"
            " WHERE ts BETWEEN ? AND ? GROUP BY substr(ts,1,19)",
            (start, end),
        )
    }
    # Corvo logs true wind as reference=0 (wind_angle_deg IS signed TWA); TWS
    # feeds the per-bin table fit.
    wb = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT substr(ts,1,19), AVG(wind_speed_kts) FROM winds"
            " WHERE ts BETWEEN ? AND ? AND reference=0 GROUP BY substr(ts,1,19)",
            (start, end),
        )
    }
    out = []
    for k, bsp in sp.items():
        cog, sog = cg.get(k, (None, None))
        out.append(
            {
                "k": k,
                "bsp": bsp,
                "sog": sog,
                "cog": cog,
                "hdg": hd.get(k),
                "heel": hl.get(k),
                "tws": wb.get(k),
            }
        )
    out.sort(key=lambda r: r["k"])
    return out


def get_effective_start(conn: sqlite3.Connection, race: sqlite3.Row) -> str:
    """Vakaros gun when available, else start_utc + 5 min (skip pre-start)."""
    if race["vakaros_session_id"] is not None:
        gun = conn.execute(
            "SELECT ts FROM vakaros_race_events"
            " WHERE session_id=? AND event_type='race_start' AND ts BETWEEN ? AND ?"
            " ORDER BY ts DESC LIMIT 1",
            (race["vakaros_session_id"], race["start_utc"], race["end_utc"]),
        ).fetchone()
        if gun is not None:
            return str(gun["ts"])
    db_start = datetime.fromisoformat(race["start_utc"])
    return (db_start + timedelta(minutes=5)).isoformat()


def detect_steady_legs(rows: list[dict]):
    """Yield (start_idx, end_idx) of contiguous steady-heading segments."""
    n = len(rows)
    if n < MIN_DURATION_S:
        return
    i = 0
    while i < n - MIN_DURATION_S:
        if rows[i]["hdg"] is None or rows[i]["bsp"] < MIN_BSP or rows[i]["sog"] is None:
            i += 1
            continue
        j = i + 1
        while j < n:
            if rows[j]["hdg"] is None or rows[j]["bsp"] < MIN_BSP or rows[j]["sog"] is None:
                break
            window = [rows[k]["hdg"] for k in range(i, j + 1) if rows[k]["hdg"] is not None]
            if hdg_spread(window) > MAX_HDG_SPREAD:
                break
            j += 1
        if j - i >= MIN_DURATION_S:
            yield (i, j)
            i = j
        else:
            i += 1


def race_current(rows: list[dict], legs: list[tuple[int, int]]) -> tuple[float, float] | None:
    """Least-squares mean current vector (east, north) over the race's steady
    legs — the same averaging full_analysis.py uses, so the paddlewheel's
    tack-dependent error largely cancels across port/starboard."""
    xs, ys = [], []
    for a, b in legs:
        lxs, lys = [], []
        for x in rows[a:b]:
            if x["sog"] is None or x["cog"] is None or x["hdg"] is None:
                continue
            cx = x["sog"] * math.sin(math.radians(x["cog"])) - x["bsp"] * math.sin(
                math.radians(x["hdg"])
            )
            cy = x["sog"] * math.cos(math.radians(x["cog"])) - x["bsp"] * math.cos(
                math.radians(x["hdg"])
            )
            if math.hypot(cx, cy) < MAX_CURRENT_KT:
                lxs.append(cx)
                lys.append(cy)
        if lxs:
            xs.append(sum(lxs) / len(lxs))
            ys.append(sum(lys) / len(lys))
    if not xs:
        return None
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def collect_samples(
    conn: sqlite3.Connection, race: sqlite3.Row
) -> list[tuple[float, float, float]]:
    """Return (heel_signed, k_observed, tws) triples for one race's steady legs.

    ``tws`` is ``nan`` when no true-wind sample aligns to that second."""
    start = get_effective_start(conn, race)
    rows = load_aligned_with_heel(conn, start, str(race["end_utc"]))
    legs = list(detect_steady_legs(rows))
    if not legs:
        return []
    cur = race_current(rows, legs)
    if cur is None:
        return []
    cx, cy = cur
    samples = []
    for a, b in legs:
        for x in rows[a:b]:
            if x["sog"] is None or x["cog"] is None or x["heel"] is None:
                continue
            # GPS-through-water = ground velocity minus the estimated current.
            gx = x["sog"] * math.sin(math.radians(x["cog"])) - cx
            gy = x["sog"] * math.cos(math.radians(x["cog"])) - cy
            gps_stw = math.hypot(gx, gy)
            if gps_stw < MIN_GPS_STW:
                continue
            k_obs = x["bsp"] / gps_stw
            tws = x["tws"] if x["tws"] is not None else float("nan")
            samples.append((x["heel"], k_obs, tws))
    return samples


# TWS bins for the emitted table (kt); tws_max exclusive.
TABLE_TWS_BINS = [(0, 6), (6, 9), (9, 12), (12, 15), (15, 30)]
# Minimum per-(bin, tack) samples before a bin is trusted / emitted.
MIN_BIN_SAMPLES = 100


def fit_table(
    samples: list[tuple[float, float, float]],
) -> tuple[list[dict[str, float]], float | None]:
    """Fit per-(TWS bin, tack) mean k and return (table_rows, suggested_gate).

    Tack is taken from heel sign (matching runtime ``resolve_stw``): heel > 0 =
    port. A bin is emitted only when both tacks clear ``MIN_BIN_SAMPLES``. The
    suggested gate is the lower edge of the lowest bin where port over-reads
    starboard (``port_k > stbd_k``) — i.e. where the breeze regime begins and
    the correction stops flipping sign.
    """
    rows: list[dict[str, float]] = []
    gate: float | None = None
    for lo, hi in TABLE_TWS_BINS:
        port = [k for h, k, t in samples if t == t and lo <= t < hi and h > 0]
        stbd = [k for h, k, t in samples if t == t and lo <= t < hi and h < 0]
        if len(port) < MIN_BIN_SAMPLES or len(stbd) < MIN_BIN_SAMPLES:
            continue
        pk = sum(port) / len(port)
        sk = sum(stbd) / len(stbd)
        rows.append(
            {"tws_min": float(lo), "tws_max": float(hi), "port": round(pk, 4), "stbd": round(sk, 4)}
        )
        if gate is None and pk > sk:
            gate = float(lo)
    return rows, gate


def linreg(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Ordinary least squares. Returns (intercept a, slope b, R²)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return (my, 0.0, 0.0)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return (a, b, r2)


def r2_on(xs: list[float], ys: list[float], a: float, b: float) -> float:
    """R² of the (a, b) model evaluated on a holdout set."""
    n = len(ys)
    if n == 0:
        return 0.0
    my = sum(ys) / n
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return 1 - ss_res / ss_tot if ss_tot > 0 else 0.0


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    races = conn.execute(
        "SELECT id, name, start_utc, end_utc, vakaros_session_id FROM races"
        " WHERE end_utc IS NOT NULL AND source='live' ORDER BY start_utc"
    ).fetchall()
    if not races:
        print("No completed source='live' races found — nothing to fit.")
        return 1

    # Split races into train / holdout (odd/even) so the reported fit is
    # validated out-of-sample rather than just echoing its own data.
    train: list[tuple[float, float, float]] = []
    holdout: list[tuple[float, float, float]] = []
    used_races = 0
    for idx, race in enumerate(races):
        s = collect_samples(conn, race)
        if not s:
            continue
        used_races += 1
        (holdout if idx % 4 == 0 else train).extend(s)
    conn.close()

    if len(train) < 100 or len(holdout) < 30:
        print(
            f"Too few steady-leg samples (train={len(train)}, holdout={len(holdout)})"
            f" from {used_races} races — need cleaner/longer data. Aborting."
        )
        return 1

    hx = [h for h, _, _ in train]
    hy = [k for _, k, _ in train]
    a, b, r2_train = linreg(hx, hy)
    r2_holdout = r2_on([h for h, _, _ in holdout], [k for _, k, _ in holdout], a, b)

    # Per-tack means as a sanity read on the residual the model should remove.
    port = [k for h, k, _ in train if h > 0]
    stbd = [k for h, k, _ in train if h < 0]
    port_mean = sum(port) / len(port) if port else float("nan")
    stbd_mean = sum(stbd) / len(stbd) if stbd else float("nan")

    print(f"Races used:        {used_races}")
    print(f"Samples:           train={len(train)}  holdout={len(holdout)}")
    print(f"Observed k (port): {port_mean:.4f}   (starboard): {stbd_mean:.4f}")
    print()
    print("Heel-linear fit  k(heel) = a + b * heel_deg   (corrected_STW = raw / k)")
    print(f"  a (speed_cal_base)        = {a:.5f}")
    print(f"  b (speed_cal_heel_slope)  = {b:.6f}   per-deg")
    print(f"  R^2 (train)               = {r2_train:.4f}")
    print(f"  R^2 (holdout)             = {r2_holdout:.4f}")
    print()

    if b <= 0:
        print(
            "WARNING: slope b <= 0 contradicts the expected sign (port tack should"
            " over-read most). Check the heel sign convention and the current"
            " estimate before trusting this fit."
        )
    if r2_holdout < 0.05:
        print(
            "NOTE: near-zero holdout R^2 — heel is a poor *continuous* predictor."
            " Prefer the TWS x tack table below (speed_cal_table) + breeze gate"
            " over deploying the heel slope b."
        )
    print()

    # TWS x tack table — the model the data actually supports. Fit on the full
    # sample set (train+holdout) since this is the deployable artifact.
    table, gate = fit_table(train + holdout)
    print("TWS x tack table (k = paddlewheel / GPS-through-water):")
    print(f"  {'TWS bin':<10}{'port_k':>9}{'stbd_k':>9}{'port-stbd':>11}")
    for r in table:
        diff = (r["port"] - r["stbd"]) / r["stbd"] * 100
        lo, hi = int(r["tws_min"]), int(r["tws_max"])
        print(f"  {f'{lo}-{hi}':<10}{r['port']:>9.4f}{r['stbd']:>9.4f}{diff:>+10.1f}%")
    if not table:
        print("  (no bin cleared the sample threshold — need more data)")
    print()
    print("Paste into app_settings 'speed_cal_table':")
    print(f"  {json.dumps(table)}")
    if gate is not None:
        print(f"Suggested boat_settings 'speed_cal_gate_min_tws': {gate:.0f}")
        print("  (below this TWS the tack bias reverses sign; gate suppresses it.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
