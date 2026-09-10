"""Frame extraction (L2) and annotated views (L3) for the planned instants.

* ``ensure_frame``   full-resolution JPEG at a video time, idempotent via the ledger
* ``render_strips``  four 90° horizon strips with absolute-bearing ticks, the true-wind
                     bearing marked, and the telemetry stamp — the reading packet
* ``render_view``    ffmpeg ``v360`` perspective view pointed at a relative bearing

The frame is boat-locked equirectangular: x → relative bearing (bow at the
centre), so absolute bearing = logged heading + relative bearing.

    uv run python -m scripts.analysis.video frames --race 254 [--kinds start,rounding]
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from scripts.analysis.video import common
from scripts.analysis.video.sources import resolve_source

if TYPE_CHECKING:
    import sqlite3

HORIZON_BAND = (0.42, 0.58)  # fraction of frame height holding hulls, marks, shoreline
QUADRANTS = ("PORT QUARTER", "PORT BOW", "STBD BOW", "STBD QUARTER")  # rel -180..-90 … 90..180
TICK_DEG = 15
STRIP_W = 1920  # output strip width; each strip is one quarter of the frame, resampled
HEADER_H = 70
LABEL_H = 40
DEFAULT_KINDS = ("prestart", "start", "rounding", "finish")


@dataclass(frozen=True)
class StripSet:
    composite: Path
    quadrants: tuple[Path, Path, Path, Path]
    thumb: Path


def frame_path(video_id: str, video_t: float) -> Path:
    return common.va_dir() / "frames" / video_id / f"{video_t:.1f}.jpg"


def strips_dir(video_id: str) -> Path:
    return common.va_dir() / "strips" / video_id


def extract_frame(source: Path, video_t: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-ss", f"{video_t:.2f}", "-i", str(source),
            "-frames:v", "1", "-q:v", "2", str(out),
        ],
        check=True,
    )  # fmt: skip


def ensure_frame(ledger: sqlite3.Connection, video_id: str, source: Path, video_t: float) -> Path:
    """Return the cached frame for (video_id, video_t), extracting it once."""
    video_t = round(video_t, 1)
    row = ledger.execute(
        "SELECT path FROM frames WHERE video_id = ? AND video_t = ?", (video_id, video_t)
    ).fetchone()
    if row is not None and Path(row["path"]).exists():
        return Path(row["path"])
    out = frame_path(video_id, video_t)
    extract_frame(source, video_t, out)
    with Image.open(out) as im:
        w, h = im.size
    ledger.execute(
        "INSERT OR REPLACE INTO frames (video_id, video_t, path, width, height, source, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (video_id, video_t, str(out), w, h, str(source), common.utcnow_iso()),
    )
    ledger.commit()
    return out


def horizon_rows(height: int) -> tuple[int, int]:
    return int(height * HORIZON_BAND[0]), int(height * HORIZON_BAND[1])


def stamp_text(label: str, utc_hms: str, video_t: float, st: common.Stamp) -> str:
    def f(v: float | None, fmt: str) -> str:
        return "?" if v is None else format(v, fmt)

    return (
        f"{label}  UTC {utc_hms}  t={video_t:.1f}s   HDG {f(st.hdg, '03.0f')}  "
        f"SOG {f(st.sog, '.1f')}kt  BSP {f(st.bsp, '.1f')}kt  TWD {f(st.twd, '03.0f')}  "
        f"TWA {f(st.twa, '+.0f')}  TWS {f(st.tws, '.1f')}kt"
    )


def render_strips(frame: Path, st: common.Stamp, header: str, out_dir: Path, stem: str) -> StripSet:
    """Write the composite, the four quadrant strips, and a small whole-frame thumbnail."""
    out_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default(size=26)
    big = ImageFont.load_default(size=32)
    with Image.open(frame) as im:
        w, h = im.size
        y0, y1 = horizon_rows(h)
        scale = STRIP_W / (w / 4)
        band_h = int((y1 - y0) * scale)
        row = band_h + LABEL_H
        composite = Image.new("RGB", (STRIP_W, HEADER_H + 4 * row), "black")
        dr = ImageDraw.Draw(composite)
        dr.text((10, 8), header, font=big, fill="yellow")
        hdg = st.hdg if st.hdg is not None else 0.0
        quads: list[Path] = []
        for i in range(4):
            strip = im.crop((i * w // 4, y0, (i + 1) * w // 4, y1)).resize((STRIP_W, band_h))
            panel = Image.new("RGB", (STRIP_W, row), "black")
            panel.paste(strip, (0, LABEL_H))
            pd = ImageDraw.Draw(panel)
            lo = -180 + i * 90
            pd.text((10, 6), f"{QUADRANTS[i]} (rel {lo:+d}..{lo + 90:+d})", font=font, fill="cyan")
            for rel in range(lo, lo + 91, TICK_DEG):
                x = int((common.rel_bearing_to_x(rel, w) - i * w // 4) * scale)
                if not 0 <= x < STRIP_W:
                    continue
                ab = common.abs_bearing(hdg, rel)
                is_wind = st.twd is not None and abs(common.angle_diff(ab, st.twd)) < TICK_DEG / 2
                colour = "red" if is_wind else "yellow"
                pd.line([(x, LABEL_H), (x, LABEL_H + 18)], fill=colour, width=2)
                pd.text(
                    (x + 3, LABEL_H + 2),
                    f"{ab:03.0f}{' W' if is_wind else ''}",
                    font=font,
                    fill=colour,
                )
            if st.hdg is None:
                pd.text((STRIP_W - 260, 6), "no heading: ticks are relative", font=font, fill="red")
            qpath = out_dir / f"{stem}_q{i}.jpg"
            panel.save(qpath, quality=88)
            quads.append(qpath)
            composite.paste(panel, (0, HEADER_H + i * row))
        cpath = out_dir / f"{stem}_strips.jpg"
        composite.save(cpath, quality=88)
        tpath = out_dir / f"{stem}_thumb.jpg"
        im.resize((1536, 1536 * h // w)).save(tpath, quality=80)
    return StripSet(cpath, (quads[0], quads[1], quads[2], quads[3]), tpath)


def view_command(
    source: Path, video_t: float, yaw_rel: float, out: Path, h_fov: int = 90, v_fov: int = 60
) -> list[str]:
    """ffmpeg command for a flat perspective frame looking at a relative bearing."""
    vf = f"v360=e:flat:yaw={yaw_rel:.1f}:pitch=0:h_fov={h_fov}:v_fov={v_fov}:w=1600:h=900"
    return [
        "ffmpeg", "-v", "error", "-y", "-ss", f"{video_t:.2f}", "-i", str(source),
        "-frames:v", "1", "-vf", vf, "-q:v", "2", str(out),
    ]  # fmt: skip


def render_view(source: Path, video_t: float, yaw_rel: float, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(view_command(source, video_t, yaw_rel, out), check=True)
    return out


def run_race(
    ledger: sqlite3.Connection,
    meta: sqlite3.Connection,
    tel_db: sqlite3.Connection,
    race_id: int,
    kinds: tuple[str, ...],
) -> int:
    race = common.load_race(meta, race_id)
    video = common.effective_video(ledger, race)
    if video is None:
        raise FileNotFoundError(f"race {race_id}: no video")
    src = resolve_source(ledger, video)
    rows = ledger.execute(
        "SELECT name, kind, utc, video_t FROM instants WHERE race_id = ? ORDER BY video_t",
        (race_id,),
    ).fetchall()
    rows = [r for r in rows if r["kind"] in kinds]
    if not rows:
        raise FileNotFoundError(
            f"race {race_id}: no instants of kinds {kinds} (run instants first)"
        )
    start, end = common.race_window(race, pre_s=6 * 60)
    tel = common.load_telemetry(tel_db, start, end)
    n = 0
    for r in rows:
        utc = common.parse_utc(r["utc"])
        st = tel.at(utc)
        frame = ensure_frame(ledger, video.video_id, src.path, float(r["video_t"]))
        stem = f"{race_id}_{r['name']}"
        if (strips_dir(video.video_id) / f"{stem}_strips.jpg").exists():
            continue
        header = stamp_text(
            f"race {race_id} {r['name']}", utc.strftime("%H:%M:%S"), float(r["video_t"]), st
        )
        render_strips(frame, st, header, strips_dir(video.video_id), stem)
        n += 1
    print(f"race {race_id}: {len(rows)} instants, {n} new strip sets")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[], help="race id (repeatable)")
    ap.add_argument("--season", action="store_true", help="every CYC Wednesday race with video")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS), help="instant kinds to render")
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    tel_db = common.open_ro(common.telemetry_db_path())
    ids = list(args.race)
    if args.season:
        ids += [r.id for r in common.list_cyc_wednesday_races(meta) if r.id not in ids]
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    failures = 0
    for rid in ids:
        try:
            run_race(ledger, meta, tel_db, rid, kinds)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            failures += 1
            print(f"race {rid}: FAILED — {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
