"""Tests for derived facts (scripts/analysis/video/facts.py)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from scripts.analysis.video import common, facts, instants, observe
from tests.video_analysis.conftest import GUN

if TYPE_CHECKING:
    import sqlite3


def obs(
    race_ahead: int | None = None,
    race_conf: float = 0.8,
    line: dict[str, Any] | None = None,
    kite: str = "down",
    split: str = "unknown",
    boats: list[dict[str, Any]] | None = None,
    conf: float = 0.8,
    forward: int = 3,
    to_weather: int = 1,
    between: int = 0,
) -> dict[str, Any]:
    return {
        "own": {"tack": "stbd", "kite": kite, "point_of_sail": "upwind"},
        "line": line or {"visible": False, "position": "unknown", "status": "unknown"},
        "marks": [],
        "boats": boats or [],
        "counts": {
            "forward": forward,
            "aft": 2,
            "to_weather": to_weather,
            "to_leeward": 4,
            "between_us_and_line": between,
            "close": 1,
        },
        "race": {"ahead": race_ahead, "behind": 5, "basis": "line", "confidence": race_conf},
        "fleet": {"mass_bearing_rel": -40, "split": split},
        "notes": "",
        "confidence": conf,
    }


def test_ladder_uses_race_ahead_and_final_result() -> None:
    o = {
        "gun": obs(race_ahead=9),
        "gun+120": obs(race_ahead=7),
        "W1": obs(race_ahead=3),
        "L1": obs(race_ahead=4, race_conf=0.2),  # too uncertain → dropped
        "fin": obs(race_ahead=4),
    }
    lad = facts.ladder(o, final_place=5)
    assert lad == {"gun": 10, "gun+120": 8, "W1": 4, "L1": None, "fin": 5}
    assert facts.ladder(o, None)["fin"] == 5  # falls back to the fin observation
    d = facts.deltas(lad)
    assert d == {"gun->gun+120": -2, "gun+120->W1": -4, "W1->L1": None, "L1->fin": None}


def test_start_facts_end_row_and_lateness() -> None:
    line_gun = {"visible": True, "position": "unknown", "status": "behind"}
    line_15 = {"visible": True, "position": "boat_third", "status": "behind"}
    line_30 = {"visible": True, "position": "boat_third", "status": "over"}
    o = {
        "gun": obs(race_ahead=9, line=line_gun, between=1),
        "gun+15": obs(line=line_15),
        "gun+30": obs(line=line_30),
    }
    st = facts.start_facts(o)
    assert st["end"] == "boat_third"  # taken from gun+15 when the gun read is unknown
    assert st["row"] == "second" and st["between_us_and_line"] == 1
    assert st["late_s"] == 30 and st["ahead_at_gun"] == 9
    # On the line at the gun → 0 s late; front row when nothing is between us and the line.
    o2 = {"gun": obs(line={"visible": True, "position": "pin_third", "status": "on"}, between=0)}
    st2 = facts.start_facts(o2)
    assert st2["late_s"] == 0 and st2["row"] == "front" and st2["end"] == "pin_third"
    # Still behind at gun+60 → capped at 60; no status seen → None.
    o3 = {
        n: obs(line={"visible": True, "position": "middle", "status": "behind"})
        for n in ("gun", "gun+60")
    }
    assert facts.start_facts(o3)["late_s"] == 60
    assert facts.start_facts({"gun": obs()})["late_s"] is None


def test_side_facts_from_tack_time(meta_db: sqlite3.Connection) -> None:
    tel = common.load_telemetry(meta_db, GUN - timedelta(minutes=1), GUN + timedelta(minutes=12))
    w1 = GUN + timedelta(minutes=8)
    o = {"gun+120": obs(split="left"), "gun+240": obs(split="right")}
    side = facts.side_facts(tel, GUN, w1, o)
    assert side["side"] == "left"  # fixture telemetry is all starboard tack (TWA +55)
    assert side["stbd_s"] > 0 and side["port_s"] == 0
    assert side["fleet_split"] == "right"  # the later observation wins
    assert facts.side_facts(tel, GUN, None, o)["side"] is None


def test_setdouse_facts_from_kite_states() -> None:
    o = {
        "W1": obs(kite="down"),
        "W1+20": obs(kite="hoisting"),
        "W1+45": obs(kite="up"),
        "W1+90": obs(kite="up"),
        "L1-20": obs(kite="up"),
        "L1": obs(kite="dousing"),
        "L1+20": obs(kite="down"),
    }
    sd = facts.setdouse_facts(o, ["W1", "L1"])
    assert sd == {"W1_set_by_s": 45, "L1_douse_by_s": 20}


def test_boats_near_and_quality() -> None:
    o = {
        "gun": obs(boats=[{"id": "412", "id_kind": "sail", "range": "close"}], conf=0.4),
        "W1": obs(boats=[{"id": "412", "id_kind": "sail", "range": "close"},
                         {"id": "229", "id_kind": "sail", "range": "mid"}], conf=0.9),
    }  # fmt: skip
    near = facts.boats_near(o)
    assert [b["id"] for b in near] == ["412"] and near[0]["instants"] == ["gun", "W1"]
    q = facts.quality(o)
    assert q["n_instants"] == 2 and q["mean_conf"] == 0.65 and q["low_conf_instants"] == ["gun"]


def test_compute_for_race_end_to_end(
    meta_db: sqlite3.Connection, ledger: sqlite3.Connection
) -> None:
    race = common.load_race(meta_db, 254)
    instants.compute_for_race(ledger, race, tier=1)
    rows = ledger.execute(
        "SELECT name, kind, utc, video_t FROM instants WHERE race_id = 254"
    ).fetchall()
    on_line = {"visible": True, "position": "boat_third", "status": "on"}
    reads = {"gun": obs(race_ahead=9, line=on_line),
             "W1": obs(race_ahead=3), "L1": obs(race_ahead=4)}  # fmt: skip
    for r in rows:
        if r["name"] in reads:
            p = observe.Packet(
                254,
                r["name"],
                r["kind"],
                common.parse_utc(r["utc"]),
                "jygj-NbqFJE",
                float(r["video_t"]),
                None,
                None,
                None,
            )  # type: ignore[arg-type]
            observe.store_observation(ledger, p, reads[r["name"]], "claude-api", "claude-opus-5")
    f = facts.compute_for_race(ledger, meta_db, race)
    assert f["ladder"] == {"gun": 10, "W1": 4, "L1": 5, "fin": 5}
    assert f["deltas"]["gun->W1"] == -6 and f["deltas"]["L1->fin"] == 0
    assert f["start"]["end"] == "boat_third" and f["start"]["late_s"] == 0
    assert f["meta"]["marks"] == ["W1", "L1"] and f["meta"]["final_place"] == 5
    stored = {r["key"] for r in ledger.execute("SELECT key FROM facts WHERE race_id = 254")}
    assert {
        "ladder",
        "deltas",
        "start",
        "side",
        "setdouse",
        "boats_near",
        "quality",
        "meta",
    } == stored
    row = facts.flat_row(254, f)
    assert row["place_W1"] == 4 and row["delta_gun->W1"] == -6 and row["final_place"] == 5
    assert row["tags"] == "start-2nd-row"
