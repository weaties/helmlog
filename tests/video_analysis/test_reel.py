"""Tests for scenario reels (scripts/analysis/video/reel.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PIL import Image
from scripts.analysis.video import common, reel

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    import pytest


def facts(**over: dict[str, Any]) -> dict[str, Any]:
    f: dict[str, Any] = {
        "meta": {"name": "20260909-Cyc-1", "marks": ["W1", "L1"]},
        "start": {"end": "boat_third", "row": "front", "late_s": 20, "ahead_at_gun": 8,
                  "between_us_and_line": 0},
        "side": {"side": "right", "fleet_split": "left"},
        "deltas": {"gun->W1": 3},
        "ladder": {"gun": 9, "W1": 2},
        "setdouse": {"W1_set_by_s": 90, "L1_douse_by_s": 20},
    }  # fmt: skip
    for k, v in over.items():
        f[k] = {**f[k], **v}
    return f


def test_scenario_selection() -> None:
    s = reel.SCENARIOS
    assert s["late_boat_end"].select(facts(), {})
    assert not s["late_boat_end"].select(facts(start={"late_s": 10}), {})
    assert not s["late_boat_end"].select(facts(start={"end": "pin_third"}), {})
    assert not s["second_row"].select(facts(), {})
    assert s["second_row"].select(facts(start={"row": "second"}), {})
    assert s["wrong_side"].select(facts(), {})
    assert not s["wrong_side"].select(facts(side={"fleet_split": "right"}), {})
    assert s["slow_set"].select(facts(), {})
    assert not s["slow_set"].select(facts(setdouse={"W1_set_by_s": 20}), {})
    assert not s["slow_douse"].select(facts(), {})
    close_inside = {"L1-20": {"boats": [{"range": "close", "rel_brg": -80}]}}
    assert s["outside_leeward"].select(facts(), close_inside)
    assert not s["outside_leeward"].select(
        facts(), {"L1-20": {"boats": [{"range": "far", "rel_brg": -80}]}}
    )
    assert not s["good_ones"].select(facts(), {})
    assert s["good_ones"].select(facts(start={"late_s": 0}), {})
    assert "crossed ~20s late" in s["late_boat_end"].caption(facts())
    assert "2nd at W1" in s["good_ones"].caption(facts(start={"late_s": 0}))


def test_yaw_for_each_camera() -> None:
    st = common.Stamp(hdg=10.0, sog=5.0, bsp=5.0, twa=-40.0, tws=9.0, twd=330.0, lat=None, lon=None)
    obs = {"line": {"committee_boat": {"rel_brg": 110}}, "marks": [{"rel_brg": -25}]}
    assert reel.yaw_for("bow", obs, st) == (0.0, 0.0)
    assert reel.yaw_for("foredeck", obs, st) == (0.0, -12.0)
    assert reel.yaw_for("committee_boat", obs, st) == (110.0, 0.0)
    assert reel.yaw_for("committee_boat", {}, st) == (90.0, 0.0)
    assert reel.yaw_for("mark", obs, st) == (-25.0, 0.0)
    assert reel.yaw_for("weather", None, st) == (-40.0, 0.0)


def test_clip_and_card_commands(tmp_path: Path) -> None:
    cmd = reel.clip_command(
        tmp_path / "s.webm", 400.0, 75.0, 110.0, 0.0, tmp_path / "strip.png", tmp_path / "c.mp4"
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "yaw=110.0" in fc and "overlay=0:672" in fc
    assert cmd[cmd.index("-ss") + 1] == "400.00" and cmd[cmd.index("-t") + 1] == "75.0"
    card = reel.card_command(tmp_path / "card.png", tmp_path / "card.mp4")
    assert "-loop" in card and card[card.index("-t") + 1] == "2.0"


def test_caption_card_and_strip_render(tmp_path: Path) -> None:
    card = reel.caption_card("Late at the boat end", "race — 20 s late", tmp_path / "card.png")
    with Image.open(card) as im:
        assert im.size == (reel.CLIP_W, reel.CLIP_H)
    strip = reel.data_strip("race 254", tmp_path / "strip.png")
    with Image.open(strip) as im:
        assert im.size == (reel.CLIP_W, 48) and im.mode == "RGBA"


def test_build_reel_skips_races_without_a_source(
    ledger: sqlite3.Connection,
    meta_db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setenv("VIDEO_ANALYSIS_DIR", str(tmp_path / "va"))
    monkeypatch.setenv("INSTA360_EXPORTS", str(tmp_path / "none"))
    f = facts()
    for k, v in f.items():
        ledger.execute(
            "INSERT INTO facts VALUES (254, ?, ?, 'claude-api', 'now')", (k, json.dumps(v))
        )
    ledger.execute(
        "INSERT INTO instants VALUES (254, 'gun', 'start', '2026-09-10T01:25:01+00:00',"
        " 'jygj-NbqFJE', 474.0)"
    )
    ledger.commit()
    out = reel.build_reel(ledger, meta_db, meta_db, reel.SCENARIOS["late_boat_end"], [254])
    assert out is None  # the race matches but its video is not on disk: skipped, no crash
