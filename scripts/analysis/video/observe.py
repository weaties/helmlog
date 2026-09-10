"""Read instants into structured observations (L4) with a vision model.

The reading packet for an instant is the four horizon strips, the whole-frame
thumbnail, and the telemetry stamp. The prompt is fixed and versioned (its
hash is stored with every observation), the reply is schema v1 JSON, and
observations are append-only: a re-read inserts a new row and marks the old
one superseded.

Readers:
  claude-api   Anthropic Messages API (``ANTHROPIC_API_KEY``), model claude-opus-5
  file         record a JSON payload produced elsewhere (spot checks)

    uv run python -m scripts.analysis.video observe --race 254 [--kinds start,rounding]
    uv run python -m scripts.analysis.video observe --race 254 --instant gun --reader file --payload read.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from scripts.analysis.video import common, frames

if TYPE_CHECKING:
    import sqlite3

SCHEMA_VERSION = 1
MODEL = "claude-opus-5"
ENDPOINT = "https://api.anthropic.com/v1/messages"
PRICE_USD_PER_MTOK = {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)

SYSTEM_PROMPT_V1 = """\
You are reading frames from a boat-locked 360-degree camera mounted on the stern of a J/105 \
sailboat (sail number 475, hull name Corvo) during a one-design fleet race of about 10-16 J/105s. \
Other classes may be racing on the same water. Your job is to report fleet-relative facts as \
JSON. Be literal and conservative: count what is visible, and say when you cannot tell.

GEOMETRY
- The panorama is equirectangular and boat-locked: the bow is at the horizontal centre, the \
stern at both edges. Horizontal position maps linearly to relative bearing (-180..+180, bow = 0, \
starboard positive).
- You receive four 90-degree horizon strips in order: PORT QUARTER (rel -180..-90), PORT BOW \
(rel -90..0), STBD BOW (rel 0..+90), STBD QUARTER (rel +90..+180), then a small whole-frame \
thumbnail for context. Use the thumbnail to count distant sails the strips may cut off.
- Yellow tick labels along the top of each strip are absolute compass bearings (from the logged \
heading). A red tick labelled W is the true wind direction: where the wind blows FROM. When the \
heading is unreliable (boat turning fast at the start) trust relative bearings over absolute.
- Our own boat fills the bottom of every strip: our deck, crew, boom, sails, backstay, and \
lifelines are NOT other boats. Our own headsail and mainsail can hide part of the horizon.

WHAT TO COUNT
- "boats" are other racing sailboats with sails up that could be in our race: J/105s (35 ft, \
fractional rig, often with a spinnaker) or boats you cannot distinguish from one. Exclude moored \
boats, anchored boats, powerboats, boats tied to the shore, and boats clearly of another type. \
The committee boat (an anchored boat, often flying flags, at one end of the start line) and marks \
(inflatable buoys, usually orange or yellow; sometimes a navigation buoy) are reported separately.
- Geometric counts (bearing only): forward = relative bearing strictly between -90 and +90; \
aft = the rest; to_weather = the bearing to the boat is within 90 degrees of the wind direction \
W; to_leeward = otherwise; close = within about 3 boat lengths.
- Race-position counts (who is winning): race.ahead = boats that are ahead of us IN THE RACE at \
this instant - at a start, boats that are already across or will cross the line before us; at a \
mark, boats that have already rounded plus boats between us and the mark; on a leg, boats further \
along the leg (further upwind on a beat, further downwind on a run). race.behind = the rest of \
the counted boats. A boat astern of us in bearing can still be ahead in the race and vice versa. \
Use sail state to tell the legs apart: on a windward-leeward course a boat flying a spinnaker is \
on a run and a boat with only main and jib is on a beat. If we are sailing upwind after a \
leeward mark, boats still under spinnaker have not rounded yet and are BEHIND us. If we are \
running and other boats are already upwind on the next beat, they are AHEAD of us. At a windward \
mark, boats already under spinnaker have rounded and are ahead; boats still beating are behind. \
Say which basis you used and lower race.confidence when it is a judgement call.
- between_us_and_line: at a start, hulls that sit between us and the start line on the way to \
the line (they would have to clear before we could cross). 0 when we are on the front row.
- range: "close" under about 3 boat lengths (sail numbers or hull names legible), "mid" 3-8 \
lengths, "far" beyond that.
- Identify a boat only if a sail number, hull name, or a distinctive spinnaker colour is \
actually legible; otherwise id is null. Never guess a number.

OUTPUT
Return ONLY a JSON object, no prose, no code fences, with exactly these keys:
{
 "own": {"tack": "stbd|port|unknown", "kite": "up|down|hoisting|dousing|unknown",
         "point_of_sail": "upwind|reaching|downwind|luffing|unknown"},
 "line": {"visible": true|false,
          "committee_boat": {"rel_brg": <int>, "range": "close|mid|far"} | null,
          "pin": {"rel_brg": <int>, "range": "close|mid|far"} | null,
          "position": "boat_third|middle|pin_third|unknown",
          "status": "behind|on|over|unknown"},
 "marks": [{"name": "windward|leeward|offset|unknown", "rel_brg": <int>, "range": "close|mid|far"}],
 "boats": [{"rel_brg": <int>, "range": "close|mid|far", "tack": "stbd|port|unknown",
            "kite": "up|down|unknown", "id": <string|null>,
            "id_kind": "sail|hull_name|kite_colour|null", "conf": <0..1>,
            "race": "ahead|behind|unknown"}],
 "counts": {"forward": <int>, "aft": <int>, "to_weather": <int>, "to_leeward": <int>,
            "between_us_and_line": <int>, "close": <int>},
 "race": {"ahead": <int>, "behind": <int>, "basis": "line|mark|leg|unknown", "confidence": <0..1>},
 "fleet": {"mass_bearing_rel": <int|null>, "split": "left|right|even|unknown"},
 "notes": "<one or two short sentences>",
 "confidence": <0..1>
}
Counts must equal what the boats list implies. rel_brg is an integer relative bearing. \
confidence is your confidence in the geometric counts (0.9 = clear view, 0.5 = cluttered).
"""

KIND_HINTS = {
    "prestart": "Pre-start: the fleet is manoeuvring near the line. Report the line ends if "
    "visible and where the mass of the fleet is relative to the two ends (fleet.mass_bearing_rel).",
    "start": "Start window. Report line.position (which third of the line we are in), "
    "line.status (behind, on, or over the line), between_us_and_line, and the boats ahead and to "
    "weather. At gun+120 and gun+240 also judge fleet.split: is more of the fleet to our left or "
    "right of the course axis?",
    "rounding": "Mark rounding window ({mark_desc}). Report the mark if visible, boats between us "
    "and the mark, boats already past it, any close boats overlapped with us, and our kite state. "
    "Use spinnakers up/down to tell who has rounded.",
    "leg": "Mid-leg. Report boats ahead and their tacks, fleet split, and our kite state.",
    "setdouse": "Spinnaker set or douse. Report our kite state precisely.",
    "finish": "Finish window. Report the committee boat or finish line if visible and boats "
    "ahead of us (between us and the line, or already finished).",
}


@dataclass(frozen=True)
class Packet:
    race_id: int
    instant: str
    kind: str
    utc: common.datetime
    video_id: str
    video_t: float
    strips: frames.StripSet
    stamp: common.Stamp
    tack: str | None


@dataclass(frozen=True)
class ReadResult:
    payload: dict[str, Any]
    usage: dict[str, int]
    cost_usd: float
    model: str


class ObserveError(RuntimeError):
    pass


def prompt_version() -> str:
    return f"v{SCHEMA_VERSION}-" + hashlib.sha256(SYSTEM_PROMPT_V1.encode()).hexdigest()[:10]


def _b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode()


def user_text(p: Packet) -> str:
    st = p.stamp

    def f(v: float | None, fmt: str) -> str:
        return "unknown" if v is None else format(v, fmt)

    return (
        f"Race {p.race_id}, instant {p.instant} (kind: {p.kind}), UTC {p.utc.strftime('%H:%M:%S')}, "
        f"video t={p.video_t:.1f}s.\n"
        f"Telemetry: heading {f(st.hdg, '03.0f')}, SOG {f(st.sog, '.1f')} kt, BSP {f(st.bsp, '.1f')} kt, "
        f"true wind direction {f(st.twd, '03.0f')}, TWA {f(st.twa, '+.0f')}, TWS {f(st.tws, '.1f')} kt, "
        f"logged tack {p.tack or 'unknown'}.\n"
        f"{kind_hint(p)}\nReturn the JSON object."
    )


def kind_hint(p: Packet) -> str:
    hint = KIND_HINTS.get(p.kind, "")
    if p.kind != "rounding":
        return hint
    mark = "windward" if p.instant.startswith("W") else "leeward"
    off = p.instant.lstrip("WL0123456789")
    when = "at the mark" if not off else f"{off} s from the mark"
    desc = f"{mark} mark, {when}; we should be {'hoisting after' if mark == 'windward' else 'dousing before'} it"
    return hint.format(mark_desc=desc)


def build_request(p: Packet) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for label, path in zip(frames.QUADRANTS, p.strips.quadrants, strict=True):
        content.append({"type": "text", "text": f"{label} strip:"})
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(path)},
            }
        )
    content.append({"type": "text", "text": "Whole-frame thumbnail:"})
    content.append(
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(p.strips.thumb)},
        }
    )
    content.append({"type": "text", "text": user_text(p)})
    return {
        "model": MODEL,
        "max_tokens": 4096,
        "system": [
            {"type": "text", "text": SYSTEM_PROMPT_V1, "cache_control": {"type": "ephemeral"}}
        ],
        "output_config": {"effort": "medium"},
        "messages": [{"role": "user", "content": content}],
    }


def parse_payload(text: str) -> dict[str, Any]:
    body = text.strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ObserveError(f"reader returned non-JSON: {text[:200]!r}") from exc
    if not isinstance(data, dict) or "counts" not in data:
        raise ObserveError("reader JSON lacks 'counts'")
    return data


def compute_cost(usage: dict[str, Any]) -> float:
    return (
        usage.get("input_tokens", 0) * PRICE_USD_PER_MTOK["input"]
        + usage.get("output_tokens", 0) * PRICE_USD_PER_MTOK["output"]
        + usage.get("cache_read_input_tokens", 0) * PRICE_USD_PER_MTOK["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * PRICE_USD_PER_MTOK["cache_write"]
    ) / 1_000_000


def post_messages(body: dict[str, Any], api_key: str) -> dict[str, Any]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    for attempt in range(4):
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(ENDPOINT, headers=headers, json=body)
        if resp.status_code in (429, 500, 502, 503, 529) and attempt < 3:
            time.sleep(5 * 2**attempt)
            continue
        if resp.status_code >= 400:
            raise ObserveError(f"API {resp.status_code}: {resp.text[:500]}")
        return dict(resp.json())
    raise ObserveError("API retries exhausted")


def read_with_claude(p: Packet, api_key: str) -> ReadResult:
    reply = post_messages(build_request(p), api_key)
    if reply.get("stop_reason") == "refusal":
        raise ObserveError(f"reader refused: {reply.get('stop_details')}")
    text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
    usage = {k: int(v) for k, v in (reply.get("usage") or {}).items() if isinstance(v, int)}
    return ReadResult(
        parse_payload(text), usage, compute_cost(usage), str(reply.get("model", MODEL))
    )


def store_observation(
    ledger: sqlite3.Connection,
    p: Packet,
    payload: dict[str, Any],
    reader: str,
    reader_version: str,
    cost_usd: float = 0.0,
) -> int:
    """Append an observation and supersede any earlier row for the same (race, instant, reader)."""
    conf = payload.get("confidence")
    cur = ledger.execute(
        "INSERT INTO observations (race_id, instant, utc, video_id, video_t, schema_version, reader,"
        " reader_version, prompt_version, confidence, payload_json, cost_usd, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            p.race_id, p.instant, p.utc.isoformat(), p.video_id, p.video_t, SCHEMA_VERSION, reader,
            reader_version, prompt_version(), float(conf) if conf is not None else None,
            common.dumps(payload), cost_usd, common.utcnow_iso(),
        ),
    )  # fmt: skip
    new_id = int(cur.lastrowid or 0)
    ledger.execute(
        "UPDATE observations SET superseded_by = ? WHERE race_id = ? AND instant = ? AND reader = ?"
        " AND id != ? AND superseded_by IS NULL",
        (new_id, p.race_id, p.instant, reader, new_id),
    )
    ledger.commit()
    return new_id


def latest_observations(
    ledger: sqlite3.Connection, race_id: int, reader: str | None = None
) -> dict[str, dict[str, Any]]:
    """instant → payload for the newest non-superseded observation (optionally one reader)."""
    q = "SELECT instant, payload_json FROM observations WHERE race_id = ? AND superseded_by IS NULL"
    args: list[Any] = [race_id]
    if reader:
        q += " AND reader = ?"
        args.append(reader)
    q += " ORDER BY id"
    out: dict[str, dict[str, Any]] = {}
    for r in ledger.execute(q, args):
        out[r["instant"]] = json.loads(r["payload_json"])
    return out


def packets_for_race(
    ledger: sqlite3.Connection,
    meta: sqlite3.Connection,
    tel_db: sqlite3.Connection,
    race_id: int,
    kinds: tuple[str, ...],
    only: set[str] | None = None,
) -> list[Packet]:
    race = common.load_race(meta, race_id)
    video = common.effective_video(ledger, race)
    if video is None:
        raise ObserveError(f"race {race_id}: no video")
    rows = ledger.execute(
        "SELECT name, kind, utc, video_t FROM instants WHERE race_id = ? ORDER BY video_t",
        (race_id,),
    ).fetchall()
    start, end = common.race_window(race, pre_s=6 * 60)
    tel = common.load_telemetry(tel_db, start, end)
    out: list[Packet] = []
    for r in rows:
        if r["kind"] not in kinds or (only and r["name"] not in only):
            continue
        sd = frames.strips_dir(video.video_id)
        stem = f"{race_id}_{r['name']}"
        strips = frames.StripSet(
            sd / f"{stem}_strips.jpg",
            tuple(sd / f"{stem}_q{i}.jpg" for i in range(4)),
            sd / f"{stem}_thumb.jpg",
        )  # type: ignore[arg-type]
        if not strips.composite.exists():
            raise ObserveError(f"race {race_id}: strips missing for {r['name']} (run frames first)")
        utc = common.parse_utc(r["utc"])
        out.append(
            Packet(
                race_id,
                r["name"],
                r["kind"],
                utc,
                video.video_id,
                float(r["video_t"]),
                strips,
                tel.at(utc),
                tel.tack_at(utc),
            )
        )
    return out


def run_claude(
    ledger: sqlite3.Connection,
    packets: list[Packet],
    api_key: str,
    max_usd: float,
    workers: int,
    force: bool,
) -> float:
    done = {
        (r["race_id"], r["instant"])
        for r in ledger.execute(
            "SELECT race_id, instant FROM observations WHERE reader = 'claude-api' AND superseded_by IS NULL"
            " AND prompt_version = ?",
            (prompt_version(),),
        )
    }
    todo = [p for p in packets if force or (p.race_id, p.instant) not in done]
    spent = 0.0
    print(f"{len(todo)} instants to read ({len(packets) - len(todo)} already read)")

    def work(p: Packet) -> tuple[Packet, ReadResult | Exception]:
        try:
            return p, read_with_claude(p, api_key)
        except (ObserveError, httpx.HTTPError) as exc:
            return p, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for p, res in pool.map(work, todo):
            if isinstance(res, Exception):
                print(f"  race {p.race_id} {p.instant}: FAILED — {res}")
                continue
            store_observation(ledger, p, res.payload, "claude-api", res.model, res.cost_usd)
            spent += res.cost_usd
            c = res.payload.get("counts", {})
            rc = res.payload.get("race", {})
            print(
                f"  race {p.race_id} {p.instant:>8}: fwd={c.get('forward')} aft={c.get('aft')}"
                f" race_ahead={rc.get('ahead')} conf={res.payload.get('confidence')}"
                f" ${res.cost_usd:.3f}"
            )
            if spent > max_usd:
                print(f"stopping: spent ${spent:.2f} > --max-usd {max_usd:.2f}")
                pool.shutdown(cancel_futures=True)
                break
    print(f"spent ${spent:.2f}")
    return spent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[], help="race id (repeatable)")
    ap.add_argument("--season", action="store_true", help="every CYC Wednesday race with video")
    ap.add_argument("--kinds", default="prestart,start,rounding,finish")
    ap.add_argument("--instant", action="append", default=[], help="only these instant names")
    ap.add_argument("--reader", choices=("claude-api", "file"), default="claude-api")
    ap.add_argument("--payload", type=Path, help="JSON file for --reader file")
    ap.add_argument("--reader-version", default="claude-code-session")
    ap.add_argument("--max-usd", type=float, default=60.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="re-read instants already observed")
    ap.add_argument("--dry-run", action="store_true", help="list packets, call nothing")
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    tel_db = common.open_ro(common.telemetry_db_path())
    ids = list(args.race)
    if args.season:
        ids += [r.id for r in common.list_cyc_wednesday_races(meta) if r.id not in ids]
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    only = set(args.instant) or None
    packets: list[Packet] = []
    for rid in ids:
        try:
            packets += packets_for_race(ledger, meta, tel_db, rid, kinds, only)
        except ObserveError as exc:
            print(f"race {rid}: skipped — {exc}")
    if args.dry_run:
        for p in packets:
            print(f"race {p.race_id} {p.instant} t={p.video_t:.1f} {p.strips.composite}")
        return 0
    if args.reader == "file":
        if args.payload is None or len(packets) != 1:
            print("--reader file needs --payload and exactly one instant")
            return 2
        payload = parse_payload(args.payload.read_text())
        oid = store_observation(ledger, packets[0], payload, "file", args.reader_version)
        print(f"stored observation {oid}")
        return 0
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set")
        return 2
    run_claude(ledger, packets, api_key, args.max_usd, args.workers, args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
