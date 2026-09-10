"""Derived facts (L5) per race from the latest observations and telemetry.

Recomputed from scratch every run, so a re-read or a prompt change flows
through. Facts are stored per (race, key) in the ledger and exported as one
CSV row per race for the report.

Keys: ladder, ladder_spread, deltas, start, side, setdouse, boats_near, quality, meta.

    uv run python -m scripts.analysis.video facts --season [--csv out.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from scripts.analysis.video import common, instants
from scripts.analysis.video.observe import latest_observations

if TYPE_CHECKING:
    import sqlite3

MIN_RACE_CONF = 0.4  # race-position counts below this are dropped, not averaged in
LADDER_STEPS = ("gun", "gun+120", "W1", "L1", "W2", "L2", "W3", "L3", "fin")
START_STATUS_OFFSETS = (("gun", 0), ("gun+15", 15), ("gun+30", 30), ("gun+60", 60))


def _race_ahead(obs: dict[str, dict[str, Any]], name: str) -> int | None:
    o = obs.get(name)
    if not o:
        return None
    race = o.get("race") or {}
    conf = race.get("confidence")
    if race.get("ahead") is None or (conf is not None and conf < MIN_RACE_CONF):
        return None
    return int(race["ahead"])


WINDOW_OFFSETS = {
    "gun": ("gun", "gun+15", "gun+30"),
    "gun+120": ("gun+120",),
    "fin": ("fin-60", "fin"),
}


def window_names(step: str) -> tuple[str, ...]:
    """Instants pooled for a ladder step: a rounding uses its -45..+45 window."""
    if step in WINDOW_OFFSETS:
        return WINDOW_OFFSETS[step]
    return (f"{step}-45", f"{step}-20", step, f"{step}+20", f"{step}+45")


def window_ahead(obs: dict[str, dict[str, Any]], step: str) -> tuple[int | None, int | None, int]:
    """Median race.ahead over the step's window, with its range and sample count.

    Single frames mis-count by a few boats in a cluster; the median over the
    window is the number the ladder uses and the range is reported alongside.
    """
    vals = [v for v in (_race_ahead(obs, n) for n in window_names(step)) if v is not None]
    if not vals:
        return None, None, 0
    vals.sort()
    mid = (
        vals[len(vals) // 2]
        if len(vals) % 2
        else round((vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2)
    )
    return int(mid), vals[-1] - vals[0], len(vals)


def ladder(obs: dict[str, dict[str, Any]], final_place: int | None) -> dict[str, int | None]:
    """Place (median boats ahead over the step's window + 1) at each ladder step."""
    out: dict[str, int | None] = {}
    for step in LADDER_STEPS:
        ahead, _, n = window_ahead(obs, step)
        if step == "fin":
            out["fin"] = (
                final_place
                if final_place is not None
                else (ahead + 1 if ahead is not None else None)
            )
            continue
        if n == 0 and not any(name in obs for name in window_names(step)):
            continue
        out[step] = ahead + 1 if ahead is not None else None
    return out


def ladder_spread(obs: dict[str, dict[str, Any]]) -> dict[str, int | None]:
    """Range of the per-frame counts behind each ladder step (0 = frames agree)."""
    out: dict[str, int | None] = {}
    for step in LADDER_STEPS:
        _, spread, n = window_ahead(obs, step)
        if n:
            out[step] = spread
    return out


def deltas(lad: dict[str, int | None]) -> dict[str, int | None]:
    """Place change per leg between consecutive known ladder steps (negative = gained)."""
    steps = [s for s in LADDER_STEPS if s in lad]
    out: dict[str, int | None] = {}
    for a, b in zip(steps, steps[1:], strict=False):
        pa, pb = lad.get(a), lad.get(b)
        out[f"{a}->{b}"] = (pb - pa) if pa is not None and pb is not None else None
    return out


def start_facts(obs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    gun = obs.get("gun") or {}
    line = gun.get("line") or {}
    counts = gun.get("counts") or {}
    end = line.get("position") or "unknown"
    if end == "unknown":
        for alt in ("gun-15", "gun+15", "gun-30"):
            pos = ((obs.get(alt) or {}).get("line") or {}).get("position")
            if pos and pos != "unknown":
                end = pos
                break
    between = counts.get("between_us_and_line")
    row = (
        None
        if between is None
        else ("front" if between == 0 else "second" if between <= 2 else "buried")
    )
    late_s: int | None = None
    status_seen = False
    for name, off in START_STATUS_OFFSETS:
        status = ((obs.get(name) or {}).get("line") or {}).get("status")
        if status in ("on", "over"):
            late_s = off
            status_seen = True
            break
        if status == "behind":
            status_seen = True
    if late_s is None and status_seen:
        late_s = 60  # behind at gun+60 still: more than a minute late (capped)
    return {
        "end": end,
        "row": row,
        "between_us_and_line": between,
        "late_s": late_s,
        "ahead_at_gun": _race_ahead(obs, "gun"),
        "to_weather_at_gun": counts.get("to_weather"),
        "forward_at_gun": counts.get("forward"),
        "fleet_mass_rel": (gun.get("fleet") or {}).get("mass_bearing_rel"),
    }


def side_facts(
    tel: common.Telemetry,
    gun: common.datetime,
    w1: common.datetime | None,
    obs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Which side of the first beat we sailed (time on each tack) and what the fleet did."""
    if w1 is None:
        return {"side": None, "stbd_s": None, "port_s": None, "fleet_split": None}
    t = gun + timedelta(seconds=60)
    end = w1 - timedelta(seconds=60)
    stbd = port = 0
    while t < end:
        tack = tel.tack_at(t)
        if tack == "stbd":
            stbd += 10
        elif tack == "port":
            port += 10
        t += timedelta(seconds=10)
    # Starboard tack (wind over the starboard side) points the bow to the left of the wind.
    side = None
    if stbd + port > 0:
        side = "left" if stbd > port * 1.25 else "right" if port > stbd * 1.25 else "middle"
    splits = [((obs.get(n) or {}).get("fleet") or {}).get("split") for n in ("gun+120", "gun+240")]
    splits = [s for s in splits if s and s != "unknown"]
    return {
        "side": side,
        "stbd_s": stbd,
        "port_s": port,
        "fleet_split": splits[-1] if splits else None,
    }


def setdouse_facts(obs: dict[str, dict[str, Any]], marks: list[str]) -> dict[str, Any]:
    """Coarse hoist/drop timing from kite state at the rounding instants (±20/45/90 s)."""
    out: dict[str, Any] = {}
    for m in marks:
        states = {}
        for off in instants.ROUNDING_S:
            name = m if off == 0 else f"{m}{off:+d}"
            k = ((obs.get(name) or {}).get("own") or {}).get("kite")
            if k:
                states[off] = k
        if m.startswith("W"):
            ups = [off for off, k in states.items() if k == "up" and off > 0]
            out[f"{m}_set_by_s"] = min(ups) if ups else None
        else:
            downs = [off for off, k in states.items() if k == "down" and off >= 0]
            out[f"{m}_douse_by_s"] = min(downs) if downs else None
    return out


def boats_near(obs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for name, o in obs.items():
        for b in o.get("boats") or []:
            bid = b.get("id")
            if bid and b.get("range") == "close":
                seen.setdefault(str(bid), {"id": bid, "kind": b.get("id_kind"), "instants": []})
                seen[str(bid)]["instants"].append(name)
    return sorted(seen.values(), key=lambda x: str(x["id"]))


def quality(obs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    confs = [o.get("confidence") for o in obs.values() if o.get("confidence") is not None]
    rconfs = [(o.get("race") or {}).get("confidence") for o in obs.values()]
    rconfs = [c for c in rconfs if c is not None]
    return {
        "n_instants": len(obs),
        "mean_conf": round(sum(confs) / len(confs), 2) if confs else None,
        "mean_race_conf": round(sum(rconfs) / len(rconfs), 2) if rconfs else None,
        "low_conf_instants": sorted(n for n, o in obs.items() if (o.get("confidence") or 0) < 0.5),
    }


DEFAULT_READERS = ("claude-api", "claude-api-480p")


def compute_for_race(
    ledger: sqlite3.Connection,
    tel_db: sqlite3.Connection,
    race: common.Race,
    reader: str | tuple[str, ...] = DEFAULT_READERS,
) -> dict[str, Any]:
    """Facts from the first reader (in preference order) that has observations for the race."""
    video = common.effective_video(ledger, race)
    flag = common.video_flag(ledger, video.video_id) if video else None
    if flag:
        raise LookupError(f"race {race.id}: video flagged — {flag}")
    readers = (reader,) if isinstance(reader, str) else reader
    obs: dict[str, dict[str, Any]] = {}
    reader_used = readers[0]
    for candidate in readers:
        obs = latest_observations(ledger, race.id, candidate)
        if obs:
            reader_used = candidate
            break
    if not obs:
        raise LookupError(f"race {race.id}: no observations by {', '.join(readers)}")
    reader = reader_used
    gun = common.effective_gun(ledger, race)
    marks = [
        r["name"]
        for r in ledger.execute(
            "SELECT name FROM instants WHERE race_id = ? AND kind = 'rounding' AND name NOT LIKE '%+%'"
            " AND name NOT LIKE '%-%' ORDER BY video_t",
            (race.id,),
        )
    ]
    w1_row = ledger.execute(
        "SELECT utc FROM instants WHERE race_id = ? AND name = 'W1'", (race.id,)
    ).fetchone()
    w1 = common.parse_utc(w1_row["utc"]) if w1_row else None
    start, end = common.race_window(race)
    tel = common.load_telemetry(tel_db, start, end)
    lad = ladder(obs, race.result_place)
    facts: dict[str, Any] = {
        "ladder": lad,
        "ladder_spread": ladder_spread(obs),
        "deltas": deltas(lad),
        "start": start_facts(obs),
        "side": side_facts(tel, gun, w1, obs) if gun else {},
        "setdouse": setdouse_facts(obs, marks),
        "boats_near": boats_near(obs),
        "quality": quality(obs),
        "meta": {
            "reader": reader,
            "name": race.name,
            "local_date": race.local_date,
            "final_place": race.result_place,
            "fleet_size": race.fleet_size,
            "tags": list(race.tags),
            "marks": marks,
        },
    }
    ledger.execute("DELETE FROM facts WHERE race_id = ?", (race.id,))
    ledger.executemany(
        "INSERT INTO facts (race_id, key, value_json, computed_from, created_at) VALUES (?,?,?,?,?)",
        [(race.id, k, common.dumps(v), reader, common.utcnow_iso()) for k, v in facts.items()],
    )
    ledger.commit()
    return facts


def flat_row(race_id: int, facts: dict[str, Any]) -> dict[str, Any]:
    """One CSV row per race: the fields the report tables use."""
    lad, st, side, meta = facts["ladder"], facts["start"], facts["side"], facts["meta"]
    row: dict[str, Any] = {
        "race_id": race_id,
        "name": meta["name"],
        "date": meta["local_date"],
        "final_place": meta["final_place"],
        "fleet_size": meta["fleet_size"],
        "start_end": st["end"],
        "start_row": st["row"],
        "late_s": st["late_s"],
        "ahead_at_gun": st["ahead_at_gun"],
        "to_weather_at_gun": st["to_weather_at_gun"],
        "side": side.get("side"),
        "fleet_split": side.get("fleet_split"),
        "mean_conf": facts["quality"]["mean_conf"],
        "reader": meta.get("reader"),
        "tags": " ".join(meta["tags"]),
    }
    for step in LADDER_STEPS:
        row[f"place_{step}"] = lad.get(step)
    for k, v in facts["deltas"].items():
        row[f"delta_{k}"] = v
    for k, v in facts["setdouse"].items():
        row[k] = v
    return row


def write_csv(rows: list[dict[str, Any]], path: str) -> None:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[], help="race id (repeatable)")
    ap.add_argument("--season", action="store_true", help="every CYC Wednesday race with video")
    ap.add_argument("--reader", action="append", default=[], help="reader(s) in preference order")
    ap.add_argument("--csv", help="write one row per race here")
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    tel_db = common.open_ro(common.telemetry_db_path())
    races = [common.load_race(meta, r) for r in args.race]
    if args.season:
        races += [r for r in common.list_cyc_wednesday_races(meta) if r.id not in args.race]
    rows = []
    for race in races:
        try:
            facts = compute_for_race(ledger, tel_db, race, tuple(args.reader) or DEFAULT_READERS)
        except LookupError as exc:
            print(f"race {race.id}: skipped — {exc}")
            continue
        rows.append(flat_row(race.id, facts))
        lad = " ".join(f"{k}={v}" for k, v in facts["ladder"].items())
        st = facts["start"]
        print(
            f"race {race.id} {race.local_date}: {lad} | start {st['end']}/{st['row']} late={st['late_s']}"
        )
    if args.csv and rows:
        write_csv(rows, args.csv)
        print(f"wrote {args.csv} ({len(rows)} races)")
    print(json.dumps({"races": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
