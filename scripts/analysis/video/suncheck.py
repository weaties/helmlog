"""Yaw calibration and heading check against the sun (docs/video-analysis.md §8).

For every cached frame of a race the sun, when it is up and visible, is the
brightest compact blob above the horizon. Its column gives a relative bearing;
the ephemeris gives its azimuth; the logged heading closes the triangle:

    yaw_error = (sun_azimuth - heading) - rel_bearing(x)      (wrapped to ±180)

The per-video median is the camera's yaw offset from the centreline (the
mount moves between race days); the spread says how much the logged heading
lags a turning boat. Stored in ``yaw_calibration``.

    uv run python -m scripts.analysis.video suncheck --race 254
"""

from __future__ import annotations

import argparse
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from scripts.analysis.video import common

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

MIN_ELEVATION_DEG = 2.0
MAX_BLOB_FRACTION = 0.04  # a sun disc is narrow; glare across clouds is not
MIN_SATURATED = 30  # pixels
MIN_FIXES = 5  # fewer frames than this cannot separate the mount from a turning boat
MAX_SPREAD_DEG = 15.0  # a larger spread means glare or the sun behind our sails, not the mount
MAX_ERROR_DEG = 60.0  # a single fix further off than this is not the sun
THUMB_W = 768

CALIBRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS yaw_calibration (
    video_id TEXT PRIMARY KEY, yaw_offset_deg REAL NOT NULL, spread_deg REAL NOT NULL,
    n INTEGER NOT NULL, created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SunFix:
    video_t: float
    utc: datetime
    heading: float
    sun_az: float
    sun_el: float
    rel_seen: float
    yaw_error: float


def sun_position(utc: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(azimuth, elevation) in degrees; low-precision solar ephemeris (±0.5°)."""
    jd = utc.timestamp() / 86400.0 + 2440587.5
    n = jd - 2451545.0
    mean_lon = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(mean_lon + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    lst = math.radians((gmst * 15 + lon) % 360)
    ha = lst - ra
    phi = math.radians(lat)
    el = math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha), math.tan(dec) * math.cos(phi) - math.sin(phi) * math.cos(ha))
    return math.degrees(az) % 360, math.degrees(el)


def find_sun_x(frame: Path) -> float | None:
    """Column (fraction of width) of the saturated blob above the horizon, or None."""
    with Image.open(frame) as im:
        w, h = im.size
        small = im.convert("L").resize((THUMB_W, max(1, THUMB_W * h // w)))
    a = np.asarray(small, dtype=np.uint8)
    top = a[: int(a.shape[0] * 0.55)]  # sky and horizon band only
    sat = top >= 250
    cols = sat.sum(axis=0)
    if cols.sum() < MIN_SATURATED:
        return None
    peak = int(np.argmax(cols))
    lit = np.flatnonzero(cols > max(2, cols[peak] * 0.2))
    # the blob around the peak must be compact
    left = peak
    while left - 1 in lit:
        left -= 1
    right = peak
    while right + 1 in lit:
        right += 1
    if (right - left + 1) / THUMB_W > MAX_BLOB_FRACTION:
        return None
    weights = cols[left : right + 1].astype(float)
    centre = (np.arange(left, right + 1) * weights).sum() / weights.sum()
    return float(centre / THUMB_W)


def fixes_for_race(
    ledger: sqlite3.Connection, tel_db: sqlite3.Connection, race: common.Race
) -> list[SunFix]:
    video = common.effective_video(ledger, race)
    if video is None:
        return []
    frames = ledger.execute(
        "SELECT video_t, path FROM frames WHERE video_id = ? ORDER BY video_t", (video.video_id,)
    ).fetchall()
    start, end = common.race_window(race, pre_s=6 * 60)
    tel = common.load_telemetry(tel_db, start, end)
    out: list[SunFix] = []
    for f in frames:
        utc = video.utc_at(float(f["video_t"]))
        st = tel.at(utc)
        if st.hdg is None or st.lat is None or st.lon is None:
            continue
        az, el = sun_position(utc, st.lat, st.lon)
        if el < MIN_ELEVATION_DEG:
            continue
        x = find_sun_x(Path(f["path"]))
        if x is None:
            continue
        rel_seen = x * 360.0 - 180.0
        err = common.angle_diff(az - st.hdg, rel_seen)
        out.append(SunFix(float(f["video_t"]), utc, st.hdg, az, el, rel_seen, err))
    return out


def summarise(fixes: list[SunFix]) -> tuple[float, float, int, bool]:
    """(median yaw error, spread, n used, reliable) after dropping implausible fixes."""
    errs = [f.yaw_error for f in fixes if abs(f.yaw_error) <= MAX_ERROR_DEG]
    if not errs:
        return 0.0, 0.0, 0, False
    med = statistics.median(errs)
    spread = statistics.pstdev(errs) if len(errs) > 1 else 0.0
    return med, spread, len(errs), len(errs) >= MIN_FIXES and spread <= MAX_SPREAD_DEG


def store(
    ledger: sqlite3.Connection, video_id: str, fixes: list[SunFix]
) -> tuple[float, float, int, bool]:
    """Store the calibration when it is reliable; otherwise drop any stale row."""
    ledger.executescript(CALIBRATION_SCHEMA)
    med, spread, n, ok = summarise(fixes)
    if ok:
        ledger.execute(
            "INSERT OR REPLACE INTO yaw_calibration VALUES (?,?,?,?,?)",
            (video_id, med, spread, n, common.utcnow_iso()),
        )
    else:
        ledger.execute("DELETE FROM yaw_calibration WHERE video_id = ?", (video_id,))
    ledger.commit()
    return med, spread, n, ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[])
    ap.add_argument("--season", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    tel_db = common.open_ro(common.telemetry_db_path())
    races = [common.load_race(meta, r) for r in args.race]
    if args.season:
        races += [r for r in common.list_cyc_wednesday_races(meta) if r.id not in args.race]
    for race in races:
        fixes = fixes_for_race(ledger, tel_db, race)
        if not fixes or race.video is None:
            print(f"race {race.id}: no sun fixes")
            continue
        video = common.effective_video(ledger, race)
        assert video is not None
        med, spread, n, ok = store(ledger, video.video_id, fixes)
        verdict = "stored" if ok else "unreliable (glare or sun behind the sails), not stored"
        print(
            f"race {race.id}: yaw offset {med:+.1f}° (σ {spread:.1f}°) from {n} frames — {verdict}"
        )
        if args.verbose:
            for f in fixes:
                print(
                    f"  t={f.video_t:7.1f} hdg={f.heading:5.1f} sun_az={f.sun_az:5.1f}"
                    f" el={f.sun_el:4.1f} seen_rel={f.rel_seen:+6.1f} err={f.yaw_error:+6.1f}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
