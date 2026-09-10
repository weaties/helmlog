"""Season aggregates for the coaching report: tables (markdown) and charts (PNG).

Reads the per-race facts from the ledger and writes ``report/`` under the
sidecar (or ``--out``): ``season.csv``, ``aggregates.md`` with the tables the
claim cards quote, and the charts. The narrative report is written by a
person from these; nothing here invents a claim.

    uv run python -m scripts.analysis.video report --season [--split 2026-07-13]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.analysis.video import common, facts as facts_mod

if TYPE_CHECKING:
    import sqlite3

STEPS = ("gun", "gun+120", "W1", "L1", "W2", "L2", "fin")
LEGS = ("gun->gun+120", "gun+120->W1", "W1->L1", "L1->W2", "W2->L2", "L2->fin", "L1->fin")
# Reference palette (dataviz skill): fixed categorical order, one hue for magnitude.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
INK, MUTED, SURFACE = "#0b0b0b", "#898781", "#fcfcfb"


def load_season(ledger: sqlite3.Connection, race_ids: list[int]) -> list[dict[str, Any]]:
    rows = []
    for rid in race_ids:
        f = {
            r["key"]: json.loads(r["value_json"])
            for r in ledger.execute("SELECT key, value_json FROM facts WHERE race_id = ?", (rid,))
        }
        if f:
            rows.append(facts_mod.flat_row(rid, f) | {"_facts": f})
    return rows


def _vals(rows: list[dict[str, Any]], key: str) -> list[int]:
    return [int(r[key]) for r in rows if r.get(key) is not None]


def median_ladder(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for step in STEPS:
        v = _vals(rows, f"place_{step}")
        out[step] = statistics.median(v) if v else None
    return out


def leg_deltas(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    return {leg: _vals(rows, f"delta_{leg}") for leg in LEGS if _vals(rows, f"delta_{leg}")}


def start_matrix(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[int]]:
    """(end, row) → finishing places."""
    m: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        if r.get("final_place") is None:
            continue
        m[(r.get("start_end") or "unknown", r.get("start_row") or "unknown")].append(
            int(r["final_place"])
        )
    return dict(m)


def lateness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_end: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r.get("late_s") is not None:
            by_end[r.get("start_end") or "unknown"].append(int(r["late_s"]))
    return {
        end: {
            "n": len(v),
            "late_gt_15": sum(1 for x in v if x > 15),
            "median_s": statistics.median(v),
        }
        for end, v in by_end.items()
    }


def side_record(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if not r.get("side"):
            continue
        out.append(
            {
                "race_id": r["race_id"],
                "date": r["date"],
                "side": r["side"],
                "fleet": r.get("fleet_split"),
                "with_fleet": (r["side"] == r.get("fleet_split"))
                if r.get("fleet_split") in ("left", "right")
                else None,
                "gun": r.get("place_gun"),
                "W1": r.get("place_W1"),
                "delta": r.get("delta_gun+120->W1")
                if r.get("delta_gun+120->W1") is not None
                else r.get("delta_gun->W1"),
                "final": r.get("final_place"),
            }
        )
    return out


def split_rows(
    rows: list[dict[str, Any]], split: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [r for r in rows if r["date"] < split], [r for r in rows if r["date"] >= split]


def _fmt(v: float | None) -> str:
    return "–" if v is None else (f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}")


def aggregates_markdown(rows: list[dict[str, Any]], split: str) -> str:
    before, after = split_rows(rows, split)
    lines = [f"# Season aggregates ({len(rows)} races)\n"]
    lines.append("## Position ladder (place at each step)\n")
    lines.append("| race | date | " + " | ".join(STEPS) + " | conf |")
    lines.append("|---|---|" + "---|" * len(STEPS) + "---|")
    for r in rows:
        lines.append(
            f"| {r['race_id']} | {r['date']} | "
            + " | ".join(_fmt(r.get(f"place_{s}")) for s in STEPS)
            + f" | {_fmt(r.get('mean_conf'))} |"
        )
    med = median_ladder(rows)
    lines.append("| **median** | | " + " | ".join(_fmt(med[s]) for s in STEPS) + " | |")
    for label, sub in (("before " + split, before), ("from " + split, after)):
        if sub:
            m = median_ladder(sub)
            lines.append(
                f"| median {label} ({len(sub)}) | | "
                + " | ".join(_fmt(m[s]) for s in STEPS)
                + " | |"
            )
    lines.append("\n## Place change per leg (negative = places gained)\n")
    lines.append("| leg | n | median | mean | gained | lost | held |")
    lines.append("|---|---|---|---|---|---|---|")
    for leg, v in leg_deltas(rows).items():
        lines.append(
            f"| {leg} | {len(v)} | {statistics.median(v):+.0f} | {statistics.mean(v):+.1f} | "
            f"{sum(1 for x in v if x < 0)} | {sum(1 for x in v if x > 0)} | {sum(1 for x in v if x == 0)} |"
        )
    lines.append("\n## Start position vs finish\n")
    lines.append("| end | row | races | median finish | finishes |")
    lines.append("|---|---|---|---|---|")
    for (end, row), places in sorted(start_matrix(rows).items()):
        lines.append(
            f"| {end} | {row} | {len(places)} | {statistics.median(places):.0f} | {' '.join(map(str, sorted(places)))} |"
        )
    lines.append("\n## Lateness by end\n")
    lines.append("| end | races with a read | late > 15 s | median late s |")
    lines.append("|---|---|---|---|")
    for end, d in sorted(lateness(rows).items()):
        lines.append(f"| {end} | {d['n']} | {d['late_gt_15']} | {d['median_s']:.0f} |")
    lines.append("\n## Side of the first beat\n")
    lines.append("| race | date | we went | fleet | with fleet | place gun→W1 | final |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in side_record(rows):
        wf = "–" if s["with_fleet"] is None else ("yes" if s["with_fleet"] else "no")
        lines.append(
            f"| {s['race_id']} | {s['date']} | {s['side']} | {s['fleet'] or '–'} | {wf} | "
            f"{_fmt(s['gun'])}→{_fmt(s['W1'])} | {_fmt(s['final'])} |"
        )
    sides = Counter(s["side"] for s in side_record(rows))
    with_fleet = [s for s in side_record(rows) if s["with_fleet"] is not None]
    lines.append(
        f"\nWent left {sides.get('left', 0)}, right {sides.get('right', 0)}, middle {sides.get('middle', 0)}. "
        f"With the fleet majority in {sum(1 for s in with_fleet if s['with_fleet'])} of {len(with_fleet)} "
        "races where the split was readable."
    )
    lines.append("\n## Set and douse (kite state at the rounding frames)\n")
    lines.append(
        "| race | date | W1 set by (s) | L1 douse by (s) | W2 set by (s) | L2 douse by (s) |"
    )
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['race_id']} | {r['date']} | {_fmt(r.get('W1_set_by_s'))} | {_fmt(r.get('L1_douse_by_s'))} | "
            f"{_fmt(r.get('W2_set_by_s'))} | {_fmt(r.get('L2_douse_by_s'))} |"
        )
    lines.append("\n## Read quality\n")
    confs = [r["mean_conf"] for r in rows if r.get("mean_conf") is not None]
    lines.append(
        f"Mean reader confidence {statistics.mean(confs):.2f} over {len(confs)} races. "
        if confs
        else ""
    )
    spreads = [
        v for r in rows for v in r["_facts"].get("ladder_spread", {}).values() if v is not None
    ]
    if spreads:
        lines.append(
            f"Per-frame count spread inside a rounding window: median {statistics.median(spreads):.0f}, "
            f"max {max(spreads)} boats (over {len(spreads)} windows)."
        )
    return "\n".join(lines) + "\n"


def charts(rows: list[dict[str, Any]], out: Path, split: str) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    written: list[Path] = []
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.edgecolor": MUTED,
            "axes.labelcolor": INK,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
        }
    )

    # 1. Ladder per race: one thin line each, median in bold.
    fig, ax = plt.subplots(figsize=(8, 4.5), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    xs = list(range(len(STEPS)))
    for r in rows:
        ys = [r.get(f"place_{s}") for s in STEPS]
        pts = [(x, y) for x, y in zip(xs, ys, strict=True) if y is not None]
        if len(pts) >= 2:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=SERIES[0], alpha=0.25, lw=1.5)
    med = median_ladder(rows)
    pts = [(x, med[s]) for x, s in zip(xs, STEPS, strict=True) if med[s] is not None]
    ax.plot(
        [p[0] for p in pts], [p[1] for p in pts], color=SERIES[0], lw=2.5, label="season median"
    )
    for x, y in pts:
        ax.annotate(
            _fmt(y), (x, y), textcoords="offset points", xytext=(0, -14), ha="center", color=INK
        )
    ax.set_xticks(xs, STEPS)
    ax.invert_yaxis()
    ax.set_ylabel("place")
    ax.set_title("Position ladder per race (thin) and season median")
    ax.grid(axis="y", color="#e6e5e1", lw=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    p = out / "ladder.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    written.append(p)

    # 2. Place change per leg: box per leg (one hue).
    ld = leg_deltas(rows)
    if ld:
        fig, ax = plt.subplots(figsize=(8, 4), facecolor=SURFACE)
        ax.set_facecolor(SURFACE)
        ax.boxplot(list(ld.values()), tick_labels=list(ld.keys()), widths=0.5, patch_artist=True,
                   boxprops={"facecolor": "#cde2fb", "edgecolor": SERIES[0]}, medianprops={"color": SERIES[0], "lw": 2},
                   whiskerprops={"color": MUTED}, capprops={"color": MUTED}, flierprops={"marker": "o", "markersize": 4, "markerfacecolor": MUTED, "markeredgecolor": MUTED})  # fmt: skip
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.set_ylabel("places lost (+) / gained (−)")
        ax.set_title("Place change per leg across the season")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        fig.tight_layout()
        p = out / "leg_deltas.png"
        fig.savefig(p, dpi=150)
        plt.close(fig)
        written.append(p)

    # 3. Start position vs finish: dot per race, x = end (jittered by row).
    ends = ("pin_third", "middle", "boat_third", "unknown")
    rowsym = {"front": "o", "second": "s", "buried": "^", "unknown": "x", None: "x"}
    fig, ax = plt.subplots(figsize=(8, 4), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    seen_rows: set[str] = set()
    for r in rows:
        if r.get("final_place") is None:
            continue
        x = ends.index(r.get("start_end") or "unknown")
        rk = r.get("start_row") or "unknown"
        ax.scatter(x + {"front": -0.15, "second": 0.0, "buried": 0.15}.get(rk, 0.0), r["final_place"],
                   marker=rowsym[rk], s=48, color=SERIES[0] if r["date"] < split else SERIES[1],
                   edgecolors=SURFACE, linewidths=1, label=rk if rk not in seen_rows else None)  # fmt: skip
        seen_rows.add(rk)
    ax.set_xticks(range(len(ends)), ends)
    ax.invert_yaxis()
    ax.set_ylabel("finish place")
    ax.set_title(f"Start end and row vs finish (blue before {split}, orange from {split})")
    ax.legend(title="row", frameon=False)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    p = out / "start_vs_finish.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    written.append(p)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[])
    ap.add_argument("--season", action="store_true")
    ap.add_argument("--split", default="2026-07-13", help="date that splits the season in two")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    ids = list(args.race)
    if args.season:
        ids += [r.id for r in common.list_cyc_wednesday_races(meta) if r.id not in ids]
    rows = load_season(ledger, ids)
    if not rows:
        print("no facts yet — run facts first")
        return 1
    out = args.out or (common.va_dir() / "report")
    out.mkdir(parents=True, exist_ok=True)
    facts_mod.write_csv(
        [{k: v for k, v in r.items() if k != "_facts"} for r in rows], str(out / "season.csv")
    )
    (out / "aggregates.md").write_text(aggregates_markdown(rows, args.split))
    for p in charts(rows, out, args.split):
        print(f"wrote {p}")
    print(f"wrote {out / 'aggregates.md'} ({len(rows)} races)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
