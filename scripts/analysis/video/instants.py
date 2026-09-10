"""Named instants per race from the sampling plan (docs/video-analysis.md §5.3).

Instants are named, not numbered, so ``gun+30`` lines up across races:

    pre-start   gun-300 … gun-15          start   gun, gun+15 … gun+240
    roundings   W1-90 … W1+90 (L1, W2 …)  finish  fin-120, fin-60, fin
    tier ≥ 2    leg frames every 60 s      tier 3  set/douse crops every 5 s

The gun is the ledger's effective gun (horn-refined Vakaros) and the
roundings are the maneuver detector's ``rounding`` events after the gun,
de-duplicated and named W1/L1/W2… in order.

    uv run python -m scripts.analysis.video instants --race 254 [--tier 1]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from scripts.analysis.video import common

if TYPE_CHECKING:
    import sqlite3

PRESTART_S = (300, 240, 180, 120, 60, 30, 15)
START_S = (0, 15, 30, 60, 120, 240)
ROUNDING_S = (-90, -45, -20, 0, 20, 45, 90)
FINISH_S = (120, 60, 0)
LEG_EVERY_S = 60
LEG_FIRST_S = 300  # leg frames start once the start window is over
LEG_ROUNDING_GAP_S = 90  # no leg frame within this of a rounding (covered by rounding frames)
SET_S = range(-15, 91, 5)  # W-15 … W+90 (hoist)
DOUSE_S = range(-90, 31, 5)  # L-90 … L+30 (drop)
MIN_MARK_AFTER_GUN_S = 180  # a "rounding" earlier than this is a pre-start turn or the start tack
MIN_MARK_SPACING_S = 240  # a beat or run is never shorter than this


class NoGunError(RuntimeError):
    """The race has neither a Vakaros gun nor a horn-derived gun."""


@dataclass(frozen=True)
class Instant:
    name: str
    kind: str  # prestart | start | rounding | leg | setdouse | finish
    utc: datetime


@dataclass
class PlanReport:
    gun: datetime
    marks: list[datetime]
    n_instants: int
    warnings: list[str] = field(default_factory=list)


def mark_roundings(gun: datetime, raw: list[datetime] | tuple[datetime, ...]) -> list[datetime]:
    """Keep detector roundings that can be marks: after the gun, well spaced."""
    marks: list[datetime] = []
    for ts in sorted(raw):
        if (ts - gun).total_seconds() < MIN_MARK_AFTER_GUN_S:
            continue
        if marks and (ts - marks[-1]).total_seconds() < MIN_MARK_SPACING_S:
            continue
        marks.append(ts)
    return marks


def mark_names(n: int) -> list[str]:
    return [f"{'W' if i % 2 == 0 else 'L'}{i // 2 + 1}" for i in range(n)]


def plan(gun: datetime, marks: list[datetime], finish: datetime, tier: int = 1) -> list[Instant]:
    out: list[Instant] = []
    for s in PRESTART_S:
        out.append(Instant(f"gun-{s}", "prestart", gun - timedelta(seconds=s)))
    for s in START_S:
        out.append(Instant("gun" if s == 0 else f"gun+{s}", "start", gun + timedelta(seconds=s)))
    names = mark_names(len(marks))
    for name, ts in zip(names, marks, strict=True):
        for s in ROUNDING_S:
            label = name if s == 0 else f"{name}{s:+d}"
            out.append(Instant(label, "rounding", ts + timedelta(seconds=s)))
    if tier >= 2:
        t = gun + timedelta(seconds=LEG_FIRST_S)
        while t < finish - timedelta(seconds=FINISH_S[0]):
            near_mark = any(abs((t - m).total_seconds()) <= LEG_ROUNDING_GAP_S for m in marks)
            if not near_mark:
                out.append(Instant(f"leg+{int((t - gun).total_seconds())}", "leg", t))
            t += timedelta(seconds=LEG_EVERY_S)
    if tier >= 3:
        for name, ts in zip(names, marks, strict=True):
            phase, offsets = ("set", SET_S) if name.startswith("W") else ("douse", DOUSE_S)
            for s in offsets:
                out.append(Instant(f"{name}{phase}{s:+d}", "setdouse", ts + timedelta(seconds=s)))
    for s in FINISH_S:
        out.append(
            Instant("fin" if s == 0 else f"fin-{s}", "finish", finish - timedelta(seconds=s))
        )
    return out


def compute_for_race(ledger: sqlite3.Connection, race: common.Race, tier: int = 1) -> PlanReport:
    """Plan the race's instants and (re)write them to the ledger with video times."""
    gun = common.effective_gun(ledger, race)
    if gun is None:
        raise NoGunError(f"race {race.id}: no gun (run horns first or import Vakaros)")
    video = common.effective_video(ledger, race)
    if video is None:
        raise NoGunError(f"race {race.id}: no linked video")
    marks = mark_roundings(gun, race.roundings)
    finish = race.end_utc or (
        marks[-1] + timedelta(minutes=10) if marks else gun + timedelta(minutes=40)
    )
    warnings: list[str] = []
    if len(marks) == 0:
        warnings.append("no mark roundings after the gun")
    elif len(marks) % 2 == 1:
        warnings.append(f"odd number of roundings ({len(marks)}): course or detector mismatch")
    if race.end_utc is None:
        warnings.append("no session end; finish estimated")
    items = plan(gun, marks, finish, tier)
    ledger.execute("DELETE FROM instants WHERE race_id = ?", (race.id,))
    ledger.executemany(
        "INSERT INTO instants (race_id, name, kind, utc, video_id, video_t) VALUES (?,?,?,?,?,?)",
        [
            (
                race.id,
                i.name,
                i.kind,
                i.utc.isoformat(),
                video.video_id,
                round(video.video_t(i.utc), 1),
            )
            for i in items
        ],
    )
    ledger.commit()
    return PlanReport(gun, marks, len(items), warnings)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[], help="race id (repeatable)")
    ap.add_argument("--season", action="store_true", help="every CYC Wednesday race with video")
    ap.add_argument("--tier", type=int, default=1, choices=(1, 2, 3))
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    races = [common.load_race(meta, r) for r in args.race]
    if args.season:
        races += [r for r in common.list_cyc_wednesday_races(meta) if r.id not in args.race]
    failures = 0
    for race in races:
        try:
            rep = compute_for_race(ledger, race, args.tier)
        except NoGunError as exc:
            failures += 1
            print(f"race {race.id}: FAILED — {exc}")
            continue
        marks = " ".join(
            f"{n}={m.strftime('%H:%M:%S')}"
            for n, m in zip(mark_names(len(rep.marks)), rep.marks, strict=True)
        )
        print(
            f"race {race.id}: gun={rep.gun.strftime('%H:%M:%S')} {marks} → {rep.n_instants} instants"
        )
        for w in rep.warnings:
            print(f"  warning: {w}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
