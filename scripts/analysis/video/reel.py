"""Scenario reels (L6): every instance of a situation in the season, cut together.

A scenario is a query over the derived facts plus a camera direction and a
caption. Each matching race contributes one clip: a ``v360`` perspective view
pointed at the bearing of interest, a data strip along the bottom, and a
caption card before it. Clips and the reel land under the sidecar; a
markdown page lists every clip with its YouTube deep link.

    uv run python -m scripts.analysis.video reel --scenario late_boat_end [--season]
    uv run python -m scripts.analysis.video reel --list
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont

from scripts.analysis.video import common
from scripts.analysis.video.observe import latest_observations
from scripts.analysis.video.sources import resolve_source

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

CLIP_W, CLIP_H = 1280, 720
CARD_S = 2.0
FactsT = dict[str, Any]
ObsT = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    select: Callable[[FactsT, ObsT], bool]
    anchor: str  # instant name the window is relative to (W1/L1 resolved per race)
    window_s: tuple[int, int]
    look_at: str  # committee_boat | bow | mark | weather | foredeck
    caption: Callable[[FactsT], str]


def _late(f: FactsT) -> int | None:
    return f["start"].get("late_s")


def _row(f: FactsT) -> str | None:
    return f["start"].get("row")


SCENARIOS: dict[str, Scenario] = {
    "late_boat_end": Scenario(
        "late_boat_end",
        "Late at the boat end",
        lambda f, o: f["start"].get("end") == "boat_third" and (_late(f) or 0) > 15,
        "gun",
        (-45, 30),
        "committee_boat",
        lambda f: (
            f"{f['meta']['name']} — crossed ~{_late(f)}s late, {f['start'].get('ahead_at_gun')} boats ahead"
        ),
    ),
    "second_row": Scenario(
        "second_row",
        "Second row",
        lambda f, o: _row(f) in ("second", "buried"),
        "gun",
        (-30, 30),
        "bow",
        lambda f: (
            f"{f['meta']['name']} — {f['start'].get('between_us_and_line')} boat(s) between us and the line"
        ),
    ),
    "wrong_side": Scenario(
        "wrong_side",
        "Wrong side of the first beat",
        lambda f, o: (
            bool(f["side"].get("side"))
            and f["side"].get("fleet_split") in ("left", "right")
            and f["side"]["side"] != f["side"]["fleet_split"]
            and (f["deltas"].get("gun->W1") or 0) > 0
        ),
        "gun+120",
        (-10, 50),
        "weather",
        lambda f: (
            f"{f['meta']['name']} — went {f['side'].get('side')}, fleet {f['side'].get('fleet_split')}, "
            f"{f['deltas'].get('gun->W1'):+d} places to W1"
        ),
    ),
    "slow_set": Scenario(
        "slow_set",
        "Slow set",
        lambda f, o: f["setdouse"].get("W1_set_by_s") in (90, None) and "W1" in f["meta"]["marks"],
        "W1",
        (-15, 90),
        "foredeck",
        lambda f: (
            f"{f['meta']['name']} — kite drawing after {f['setdouse'].get('W1_set_by_s') or '>90'} s"
        ),
    ),
    "slow_douse": Scenario(
        "slow_douse",
        "Slow douse",
        lambda f, o: (
            f["setdouse"].get("L1_douse_by_s") in (45, 90, None) and "L1" in f["meta"]["marks"]
        ),
        "L1",
        (-60, 30),
        "foredeck",
        lambda f: (
            f"{f['meta']['name']} — kite down {f['setdouse'].get('L1_douse_by_s') or '>90'} s after the mark"
        ),
    ),
    "outside_leeward": Scenario(
        "outside_leeward",
        "Outside at the leeward mark",
        lambda f, o: any(
            b.get("range") == "close" and -120 <= (b.get("rel_brg") or 0) <= -60
            for b in (o.get("L1-20") or {}).get("boats") or []
        ),
        "L1",
        (-45, 20),
        "mark",
        lambda f: f"{f['meta']['name']} — close boat inside us approaching L1",
    ),
    "good_ones": Scenario(
        "good_ones",
        "The good ones",
        lambda f, o: (
            _row(f) == "front"
            and (_late(f) if _late(f) is not None else 99) <= 15
            and (f["ladder"].get("W1") or 99) <= 3
        ),
        "gun",
        (-30, 45),
        "bow",
        lambda f: (
            f"{f['meta']['name']} — front row, on time, {f['ladder'].get('W1')}{'rd' if f['ladder'].get('W1') == 3 else 'st' if f['ladder'].get('W1') == 1 else 'nd'} at W1"
        ),
    ),
}


def yaw_for(
    look_at: str, anchor_obs: dict[str, Any] | None, stamp: common.Stamp
) -> tuple[float, float]:
    """(yaw, pitch) in relative-bearing degrees for the camera direction."""
    o = anchor_obs or {}
    if look_at == "bow":
        return 0.0, 0.0
    if look_at == "foredeck":
        return 0.0, -12.0
    if look_at == "committee_boat":
        cb = (o.get("line") or {}).get("committee_boat")
        return (float(cb["rel_brg"]), 0.0) if cb else (90.0, 0.0)
    if look_at == "mark":
        marks = o.get("marks") or []
        return (float(marks[0]["rel_brg"]), 0.0) if marks else (-30.0, 0.0)
    if look_at == "weather":
        return (float(stamp.twa), 0.0) if stamp.twa is not None else (0.0, 0.0)
    return 0.0, 0.0


def data_strip(text: str, path: Path) -> Path:
    im = Image.new("RGBA", (CLIP_W, 48), (0, 0, 0, 170))
    ImageDraw.Draw(im).text((12, 10), text, font=ImageFont.load_default(size=24), fill="yellow")
    im.save(path)
    return path


def caption_card(title: str, caption: str, path: Path) -> Path:
    im = Image.new("RGB", (CLIP_W, CLIP_H), "black")
    dr = ImageDraw.Draw(im)
    dr.text((60, 260), title, font=ImageFont.load_default(size=48), fill="white")
    dr.text((60, 340), caption, font=ImageFont.load_default(size=30), fill="yellow")
    im.save(path)
    return path


def clip_command(
    source: Path, t0: float, dur: float, yaw: float, pitch: float, strip: Path, out: Path
) -> list[str]:
    vf = (
        f"[0:v]v360=e:flat:yaw={yaw:.1f}:pitch={pitch:.1f}:h_fov=100:v_fov=60:w={CLIP_W}:h={CLIP_H}[v];"
        f"[v][1:v]overlay=0:{CLIP_H - 48}[o]"
    )
    return [
        "ffmpeg", "-v", "error", "-y", "-ss", f"{t0:.2f}", "-t", f"{dur:.1f}", "-i", str(source),
        "-i", str(strip), "-filter_complex", vf, "-map", "[o]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-ar", "48000", str(out),
    ]  # fmt: skip


def card_command(card: Path, out: Path) -> list[str]:
    return [
        "ffmpeg", "-v", "error", "-y", "-loop", "1", "-t", f"{CARD_S}", "-i", str(card),
        "-f", "lavfi", "-t", f"{CARD_S}", "-i", "anullsrc=r=48000:cl=stereo",
        "-c:v", "libx264", "-preset", "veryfast", "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(out),
    ]  # fmt: skip


def concat(parts: list[Path], out: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c",
            "copy",
            str(out),
        ],
        check=True,
    )


def load_facts(ledger: sqlite3.Connection, race_id: int) -> FactsT | None:
    rows = ledger.execute(
        "SELECT key, value_json FROM facts WHERE race_id = ?", (race_id,)
    ).fetchall()
    if not rows:
        return None
    return {r["key"]: json.loads(r["value_json"]) for r in rows}


def matches(
    ledger: sqlite3.Connection, scenario: Scenario, race_ids: list[int]
) -> list[tuple[int, FactsT, ObsT]]:
    out = []
    for rid in race_ids:
        f = load_facts(ledger, rid)
        if f is None:
            continue
        o = latest_observations(ledger, rid, "claude-api")
        if scenario.select(f, o):
            out.append((rid, f, o))
    return out


def build_reel(
    ledger: sqlite3.Connection,
    meta: sqlite3.Connection,
    tel_db: sqlite3.Connection,
    scenario: Scenario,
    race_ids: list[int],
) -> Path | None:
    hits = matches(ledger, scenario, race_ids)
    print(f"{scenario.name}: {len(hits)} race(s) match")
    if not hits:
        return None
    reel_dir = common.va_dir() / "reels"
    clip_dir = common.va_dir() / "clips" / scenario.name
    reel_dir.mkdir(parents=True, exist_ok=True)
    clip_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    links: list[str] = []
    for rid, f, o in hits:
        race = common.load_race(meta, rid)
        video = common.effective_video(ledger, race)
        if video is None:
            continue
        try:
            src = resolve_source(ledger, video, download=False)
        except FileNotFoundError as exc:
            print(f"  race {rid}: skipped — {exc}")
            continue
        anchor = ledger.execute(
            "SELECT utc, video_t FROM instants WHERE race_id = ? AND name = ?",
            (rid, scenario.anchor),
        ).fetchone()
        if anchor is None:
            continue
        utc = common.parse_utc(anchor["utc"])
        t0 = float(anchor["video_t"]) + scenario.window_s[0]
        dur = float(scenario.window_s[1] - scenario.window_s[0])
        start, end = common.race_window(race)
        stamp = common.load_telemetry(tel_db, start, end).at(utc)
        yaw, pitch = yaw_for(scenario.look_at, o.get(scenario.anchor), stamp)
        caption = scenario.caption(f)
        card = caption_card(scenario.title, caption, clip_dir / f"{rid}_card.png")
        card_mp4 = clip_dir / f"{rid}_card.mp4"
        subprocess.run(card_command(card, card_mp4), check=True)
        strip = data_strip(
            f"{race.name}  {scenario.anchor}{scenario.window_s[0]:+d}s  TWS {stamp.tws or 0:.0f}kt  "
            f"SOG {stamp.sog or 0:.1f}kt  TWA {stamp.twa or 0:+.0f}",
            clip_dir / f"{rid}_strip.png",
        )
        clip = clip_dir / f"{rid}.mp4"
        subprocess.run(
            clip_command(src.path, max(0.0, t0), dur, yaw, pitch, strip, clip), check=True
        )
        parts += [card_mp4, clip]
        deep = (
            video.url_at(utc + timedelta(seconds=scenario.window_s[0]))
            if hasattr(video, "url_at")
            else None
        )
        yt = f"https://youtu.be/{video.video_id}?t={int(max(0.0, t0))}"
        links.append(f"- **{race.name}** ({race.local_date}): {caption} — [video]({deep or yt})")
    if not parts:
        print(f"  {scenario.name}: no clips could be cut")
        return None
    out = reel_dir / f"{scenario.name}.mp4"
    concat(parts, out)
    (reel_dir / f"{scenario.name}.md").write_text(
        f"# {scenario.title}\n\n{len(links)} clip(s), camera: {scenario.look_at}, window "
        f"{scenario.anchor}{scenario.window_s[0]:+d}s..{scenario.window_s[1]:+d}s.\n\n"
        + "\n".join(links)
        + "\n"
    )
    print(f"  wrote {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--scenario", action="append", default=[], help="scenario name (repeatable)")
    ap.add_argument("--all", action="store_true", help="build every scenario")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--race", type=int, action="append", default=[])
    ap.add_argument("--season", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        for s in SCENARIOS.values():
            print(
                f"{s.name:18} {s.title:32} anchor={s.anchor} window={s.window_s} camera={s.look_at}"
            )
        return 0
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    tel_db = common.open_ro(common.telemetry_db_path())
    ids = list(args.race)
    if args.season:
        ids += [r.id for r in common.list_cyc_wednesday_races(meta) if r.id not in ids]
    names = list(SCENARIOS) if args.all else args.scenario
    for name in names:
        build_reel(ledger, meta, tel_db, SCENARIOS[name], ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
