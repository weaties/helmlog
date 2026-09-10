"""Tests for season aggregates (scripts/analysis/video/report.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scripts.analysis.video import report

if TYPE_CHECKING:
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
