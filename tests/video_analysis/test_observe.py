"""Tests for the observation reader (scripts/analysis/video/observe.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image
from scripts.analysis.video import common, frames, observe

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def make_packet(tmp_path: Path, instant: str = "gun", kind: str = "start") -> observe.Packet:
    paths = []
    for name in ("q0", "q1", "q2", "q3", "thumb"):
        p = tmp_path / f"254_{instant}_{name}.jpg"
        Image.new("RGB", (64, 32), (10, 20, 30)).save(p)
        paths.append(p)
    strips = frames.StripSet(tmp_path / "c.jpg", (paths[0], paths[1], paths[2], paths[3]), paths[4])
    st = common.Stamp(hdg=337.0, sog=4.7, bsp=4.5, twa=55.0, tws=8.6, twd=32.0, lat=None, lon=None)
    return observe.Packet(
        254,
        instant,
        kind,
        datetime(2026, 9, 10, 1, 25, 1, tzinfo=UTC),
        "jygj-NbqFJE",
        468.0,
        strips,
        st,
        "stbd",
    )


PAYLOAD = {
    "own": {"tack": "stbd", "kite": "down", "point_of_sail": "upwind"},
    "line": {"visible": True, "committee_boat": {"rel_brg": 110, "range": "close"}, "pin": None,
             "position": "boat_third", "status": "behind"},
    "marks": [],
    "boats": [{"rel_brg": -70, "range": "close", "tack": "stbd", "kite": "down", "id": "412",
               "id_kind": "sail", "conf": 0.9, "race": "ahead"}],
    "counts": {"forward": 1, "aft": 9, "to_weather": 0, "to_leeward": 10,
               "between_us_and_line": 0, "close": 1},
    "race": {"ahead": 1, "behind": 9, "basis": "line", "confidence": 0.7},
    "fleet": {"mass_bearing_rel": -60, "split": "unknown"},
    "notes": "Fleet in a line to leeward.",
    "confidence": 0.8,
}  # fmt: skip


def test_build_request_shape(tmp_path: Path) -> None:
    body = observe.build_request(make_packet(tmp_path))
    assert body["model"] == observe.MODEL
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = body["messages"][0]["content"]
    images = [c for c in content if c["type"] == "image"]
    assert len(images) == 5  # four strips + thumbnail
    text = content[-1]["text"]
    assert "instant gun" in text and "heading 337" in text and "logged tack stbd" in text
    assert "line.position" in text  # the start-kind hint
    assert "thinking" not in body  # Opus 5 runs adaptive thinking by default


def test_parse_payload_strips_fences_and_validates() -> None:
    assert observe.parse_payload("```json\n" + json.dumps(PAYLOAD) + "\n```")["race"]["ahead"] == 1
    with pytest.raises(observe.ObserveError):
        observe.parse_payload("Sure! Here is the JSON: {")
    with pytest.raises(observe.ObserveError):
        observe.parse_payload('{"boats": []}')


def test_compute_cost_uses_opus_5_prices() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert observe.compute_cost(usage) == pytest.approx(5.0)
    usage = {"input_tokens": 0, "output_tokens": 100_000, "cache_read_input_tokens": 1_000_000}
    assert observe.compute_cost(usage) == pytest.approx(2.5 + 0.5)


def test_read_with_claude_parses_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(body: dict[str, Any], api_key: str) -> dict[str, Any]:
        assert api_key == "k"
        return {
            "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(PAYLOAD)}],
            "usage": {"input_tokens": 6000, "output_tokens": 400},
        }

    monkeypatch.setattr(observe, "post_messages", fake_post)
    res = observe.read_with_claude(make_packet(tmp_path), "k")
    assert res.payload["counts"]["aft"] == 9
    assert res.cost_usd == pytest.approx(6000 * 5 / 1e6 + 400 * 25 / 1e6)


def test_read_with_claude_surfaces_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        observe,
        "post_messages",
        lambda body, key: {
            "stop_reason": "refusal",
            "stop_details": {"category": "x"},
            "content": [],
        },
    )
    with pytest.raises(observe.ObserveError):
        observe.read_with_claude(make_packet(tmp_path), "k")


def test_store_observation_is_append_only_and_supersedes(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    p = make_packet(tmp_path)
    first = observe.store_observation(ledger, p, PAYLOAD, "claude-api", "claude-opus-5", 0.05)
    second_payload = dict(PAYLOAD, race=dict(PAYLOAD["race"], ahead=2))
    second = observe.store_observation(
        ledger, p, second_payload, "claude-api", "claude-opus-5", 0.05
    )
    human = observe.store_observation(ledger, p, PAYLOAD, "file", "spotcheck")
    rows = {r["id"]: r for r in ledger.execute("SELECT * FROM observations")}
    assert rows[first]["superseded_by"] == second
    assert rows[second]["superseded_by"] is None and rows[human]["superseded_by"] is None
    assert rows[second]["prompt_version"] == observe.prompt_version()
    assert rows[second]["confidence"] == 0.8
    latest = observe.latest_observations(ledger, 254, reader="claude-api")
    assert latest["gun"]["race"]["ahead"] == 2
    assert observe.latest_observations(ledger, 254, reader="file")["gun"]["race"]["ahead"] == 1


def test_run_claude_skips_done_and_respects_budget(
    ledger: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_read(p: observe.Packet, key: str) -> observe.ReadResult:
        calls.append(p.instant)
        return observe.ReadResult(PAYLOAD, {"input_tokens": 1}, 0.4, "claude-opus-5")

    monkeypatch.setattr(observe, "read_with_claude", fake_read)
    pk = [make_packet(tmp_path, name) for name in ("gun-30", "gun", "gun+15", "gun+30")]
    observe.store_observation(ledger, pk[1], PAYLOAD, "claude-api", "claude-opus-5")
    spent = observe.run_claude(ledger, pk, "k", max_usd=0.5, workers=1, force=False)
    # 'gun' is already read; the budget stops the run after the second charged read.
    assert calls[:2] == ["gun-30", "gun+15"] and "gun" not in calls
    assert spent == pytest.approx(0.8)
