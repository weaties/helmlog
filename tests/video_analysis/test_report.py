"""Tests for season aggregates (scripts/analysis/video/report.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scripts.analysis.video import observe, report

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def row(rid: int, date: str, **kw: Any) -> dict[str, Any]:  # noqa: ANN401
    base: dict[str, Any] = {
        "race_id": rid,
        "date": date,
        "final_place": 5,
        "start_end": "boat_third",
        "start_row": "front",
        "late_s": 20,
        "side": "left",
        "fleet_split": "left",
        "mean_conf": 0.6,
        "place_gun": 8,
        "place_gun+120": 6,
        "place_W1": 4,
        "place_L1": 5,
        "place_fin": 5,
        "delta_gun->gun+120": -2,
        "delta_gun+120->W1": -2,
        "delta_W1->L1": 1,
        "delta_L1->fin": 0,
        "W1_set_by_s": 45,
        "L1_douse_by_s": 20,
        "_facts": {"ladder_spread": {"W1": 2, "L1": 0}},
    }
    base.update(kw)
    return base


ROWS = [
    row(1, "2026-05-06"),
    row(2, "2026-06-03", place_W1=2, final_place=2, start_end="pin_third", late_s=5, side="right"),
    row(3, "2026-07-15", place_gun=12, place_W1=10, final_place=11, start_row="second", late_s=40),
    row(
        4,
        "2026-08-12",
        place_gun=None,
        place_W1=None,
        final_place=9,
        side="left",
        fleet_split="right",
    ),
]


def test_median_ladder_and_leg_deltas() -> None:
    med = report.median_ladder(ROWS)
    assert med["gun"] == 8 and med["W1"] == 4 and med["fin"] == 5
    assert med["W2"] is None
    ld = report.leg_deltas(ROWS)
    assert ld["W1->L1"] == [1, 1, 1, 1] and "L2->fin" not in ld


def test_start_matrix_and_lateness() -> None:
    m = report.start_matrix(ROWS)
    assert m[("boat_third", "front")] == [5, 9]
    assert m[("boat_third", "second")] == [11]
    assert m[("pin_third", "front")] == [2]
    late = report.lateness(ROWS)
    assert late["boat_third"] == {"n": 3, "late_gt_15": 3, "median_s": 20}
    assert late["pin_third"]["late_gt_15"] == 0


def test_side_record_and_split() -> None:
    rec = report.side_record(ROWS)
    assert [r["with_fleet"] for r in rec] == [True, False, True, False]
    before, after = report.split_rows(ROWS, "2026-07-13")
    assert [r["race_id"] for r in before] == [1, 2] and [r["race_id"] for r in after] == [3, 4]


def test_aggregates_markdown_sections() -> None:
    md = report.aggregates_markdown(ROWS, "2026-07-13")
    for heading in (
        "Position ladder",
        "Place change per leg",
        "Start position vs finish",
        "Lateness by end",
        "Side of the first beat",
        "Set and douse",
        "Read quality",
    ):
        assert heading in md
    assert "| **median** | | 8 | 6 | 4 | 5 | – | – | 5 |" in md
    assert "Went left 3, right 1, middle 0" in md
    assert "spread inside a rounding window: median 1" in md


def test_charts_write_pngs(tmp_path: Path) -> None:
    written = report.charts(ROWS, tmp_path, "2026-07-13")
    assert [p.name for p in written] == ["ladder.png", "leg_deltas.png", "start_vs_finish.png"]
    assert all(p.stat().st_size > 1000 for p in written)


def test_spot_checks_pair_model_and_human(ledger: sqlite3.Connection, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    def pkt(name: str) -> observe.Packet:
        return observe.Packet(
            254, name, "start", datetime(2026, 9, 10, 1, 25, 1, tzinfo=UTC), "v", 468.0,
            None, None, None,  # type: ignore[arg-type]
        )  # fmt: skip

    def pay(ahead: int, fwd: int) -> dict[str, Any]:
        return {"counts": {"forward": fwd}, "race": {"ahead": ahead}, "confidence": 0.5}

    observe.store_observation(ledger, pkt("gun"), pay(2, 5), "claude-api", "m")
    observe.store_observation(ledger, pkt("gun"), pay(4, 5), "file", "h")
    observe.store_observation(ledger, pkt("W1"), pay(1, 5), "claude-api", "m")  # no human read
    checks = report.spot_checks(ledger)
    assert len(checks) == 1 and checks[0]["instant"] == "gun"
    md = report.spot_check_markdown(checks)
    assert "| 254 | gun | 2 | 4 | -2 | 5 | 5 | +0 |" in md
    assert "within ±1 boat on 0 of 1" in md
    assert "No human reads" in report.spot_check_markdown([])


def test_method_markdown_lists_sync_yaw_and_flags(ledger: sqlite3.Connection) -> None:
    ledger.execute(
        "INSERT INTO sync VALUES (254,'v','horn+vakaros','2026-09-10T01:25:01+00:00',468.0,"
        "'2026-09-10T01:25:01+00:00',468.0,5.6,3,'now')"
    )
    ledger.execute("INSERT INTO sync VALUES (22,'w','stored',NULL,NULL,'x',0.0,0.0,0,'now')")
    ledger.executescript(
        "CREATE TABLE yaw_calibration (video_id TEXT PRIMARY KEY, yaw_offset_deg REAL,"
        " spread_deg REAL, n INTEGER, created_at TEXT);"
        "INSERT INTO yaw_calibration VALUES ('v', -7.9, 3.6, 21, 'now');"
    )
    ledger.execute("INSERT INTO video_flags VALUES ('w', 'unlevelled horizon', 'now')")
    md = report.method_markdown(ledger, [])
    assert "| 254 | horn+vakaros | +5.6 | 3/3 |" in md
    assert "| 22 | stored | +0.0 | 0/3 |" in md
    assert "1 races anchored on horn + Vakaros: median Δ +5.6 s" in md
    assert "| v | -7.9 | 3.6 | 21 |" in md
    assert "`w`: unlevelled horizon" in md
    assert "| 22 |" not in report.method_markdown(ledger, [254])
