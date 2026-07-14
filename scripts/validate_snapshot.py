#!/usr/bin/env python3
"""Validate that every on-disk artifact referenced by logger.db exists.

Issue #676: backup snapshots silently lose photo/audio/avatar files when
the DB is copied but the attachment tree isn't (or when paths in the DB
point outside the backed-up root). This script cross-checks:

  moment_attachments.path  → <data_root>/notes/<path>
  audio_sessions.file_path → absolute, or <data_root>/audio/<path>
  users.avatar_path        → <data_root>/avatars/<path>

It also guards the DB itself (#807): a snapshot is only good if logger.db is
readable AND holds the data it should. Two real failures motivated this — a
corrupt copy (sqlite3 .backup ran out of room on the Pi's tmpfs and left a
zeroed header) and, worse, a *torn* copy that passed every gate with race rows
from 07-08 whose telemetry stopped at 06-30. Pass --expect-min-ts with the live
DB's per-table maxima to catch the latter.

Usage:
  python3 scripts/validate_snapshot.py <data_root>
    data_root should contain logger.db plus the notes/, audio/,
    avatars/ subdirectories (i.e. the equivalent of ~/helmlog/data/
    on the Pi, or <snapshot>/data/ in a backup tree).

Exit codes:
  0   all referenced files present
  1   one or more orphaned DB rows (missing files)
  2   could not open the DB or data root, or the DB is corrupt
  3   the DB is stale — some table lags its live counterpart in --expect-min-ts
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Tables whose newest row tells us how current the snapshot is. positions is
# the high-rate one; the others cover a DB captured outside a sail. Missing
# tables/columns are skipped, so this stays safe across schema versions.
_RECENCY_TABLES = (("positions", "ts"), ("winds", "ts"), ("races", "start_utc"))


@dataclass
class OrphanReport:
    kind: str
    total: int
    missing: int
    samples: list[str]


def _check_attachments(db: sqlite3.Connection, notes_root: Path) -> OrphanReport:
    try:
        cur = db.execute(
            "SELECT path FROM moment_attachments WHERE path IS NOT NULL AND path != ''"
        )
    except sqlite3.OperationalError:
        # Pre-moments schema, or a non-standard test DB.
        return OrphanReport("moment_attachments", 0, 0, [])
    rows = cur.fetchall()
    missing: list[str] = []
    for (path,) in rows:
        full = notes_root / str(path)
        if not full.is_file():
            missing.append(str(path))
    return OrphanReport("moment_attachments", len(rows), len(missing), missing[:5])


def _check_audio(db: sqlite3.Connection, data_root: Path) -> OrphanReport:
    # file_path is typically absolute (/home/weaties/helmlog/data/audio/...)
    # but older rows may be relative to the helmlog CWD.
    try:
        cur = db.execute(
            "SELECT file_path FROM audio_sessions WHERE file_path IS NOT NULL AND file_path != ''"
        )
    except sqlite3.OperationalError:
        # Pre-audio schema
        return OrphanReport("audio_sessions", 0, 0, [])
    rows = cur.fetchall()
    missing: list[str] = []
    for (fp,) in rows:
        p = Path(fp)
        candidates = [p] if p.is_absolute() else [data_root / p, data_root / "audio" / p.name]
        if not any(c.is_file() for c in candidates):
            missing.append(str(fp))
    return OrphanReport("audio_sessions", len(rows), len(missing), missing[:5])


def _check_avatars(db: sqlite3.Connection, avatars_root: Path) -> OrphanReport:
    try:
        cur = db.execute(
            "SELECT avatar_path FROM users WHERE avatar_path IS NOT NULL AND avatar_path != ''"
        )
    except sqlite3.OperationalError:
        return OrphanReport("users.avatar_path", 0, 0, [])
    rows = cur.fetchall()
    missing: list[str] = []
    for (ap,) in rows:
        p = Path(ap)
        full = p if p.is_absolute() else avatars_root / p
        if not full.is_file():
            missing.append(str(ap))
    return OrphanReport("users.avatar_path", len(rows), len(missing), missing[:5])


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def newest_per_table(db: sqlite3.Connection) -> dict[str, str]:
    """Newest timestamp in each recency table that exists in this DB."""
    out: dict[str, str] = {}
    for table, column in _RECENCY_TABLES:
        try:
            row = db.execute(f"SELECT max({column}) FROM {table}").fetchone()  # noqa: S608
        except sqlite3.OperationalError:
            continue  # table/column absent in this schema version
        if row and row[0] is not None:
            out[table] = str(row[0])
    return out


def parse_expectation(spec: str) -> dict[str, str]:
    """Parse `positions=<ts>,winds=<ts>` into a per-table expectation."""
    want: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        table, _, ts = part.partition("=")
        if ts:
            want[table.strip()] = ts.strip()
    return want


def _stale_tables(have: dict[str, str], want: dict[str, str]) -> list[str]:
    """Tables whose snapshot max lags the live DB's max.

    Compared PER TABLE, never as a single max across tables: 20260708T100002Z
    had races current to 07-08 while positions and winds stopped at 06-30, so a
    whole-DB max would have called that torn copy fresh. (#807)
    """
    stale = []
    for table, want_raw in want.items():
        if table not in have:
            continue
        want_dt, have_dt = _parse_ts(want_raw), _parse_ts(have[table])
        if want_dt is None or have_dt is None:
            continue
        if have_dt < want_dt:
            stale.append(f"{table}: snapshot {have[table]} < live {want_raw}")
    return stale


def validate(data_root: Path, expect_min_ts: str | None = None) -> tuple[int, list[OrphanReport]]:
    db_path = data_root / "logger.db"
    if not db_path.is_file():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 2, []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # A corrupt DB (zeroed header, truncated .backup) only raises when we
        # actually read it — connect() is lazy. Report it as rc=2 rather than
        # letting a DatabaseError traceback escape, which is how the 2026-07-14
        # corruption got logged as a WARN on an otherwise "SUCCESS" run. (#807)
        try:
            reports = [
                _check_attachments(db, data_root / "notes"),
                _check_audio(db, data_root),
                _check_avatars(db, data_root / "avatars"),
            ]
            have = newest_per_table(db)
        except sqlite3.DatabaseError as exc:
            print(f"ERROR: {db_path} is not a readable SQLite database: {exc}", file=sys.stderr)
            return 2, []
    finally:
        db.close()

    # Faithfulness to the source, not absolute age: the boat sits idle for
    # weeks between regattas, so "old data" is only a fault when the LIVE DB
    # has newer records than the copy we just took. (#807)
    if expect_min_ts:
        stale = _stale_tables(have, parse_expectation(expect_min_ts))
        if stale:
            print(
                f"ERROR: {db_path} is stale — it is valid SQLite but lags the live DB:",
                file=sys.stderr,
            )
            for line in stale:
                print(f"  {line}", file=sys.stderr)
            return 3, reports

    worst = 0 if all(r.missing == 0 for r in reports) else 1
    return worst, reports


def _format_report(reports: list[OrphanReport]) -> str:
    lines = []
    for r in reports:
        pct = f"{(r.total - r.missing) / r.total * 100:.1f}%" if r.total else "n/a"
        status = "OK" if r.missing == 0 else f"MISSING {r.missing}"
        lines.append(f"- {r.kind}: {r.total} rows, {status} ({pct} present)")
        for s in r.samples:
            lines.append(f"    missing: {s}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("data_root", type=Path, help="Directory containing logger.db")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Only print output on failure or when orphans are found",
    )
    p.add_argument(
        "--expect-min-ts",
        default=None,
        metavar="TABLE=ISO_TS,...",
        help=(
            "Per-table newest record in the LIVE DB at staging time, e.g. "
            "'positions=2026-07-14T03:45:55+00:00,races=...'. Any table in the "
            "snapshot that lags its live counterpart fails the run as stale (rc=3)."
        ),
    )
    args = p.parse_args()

    if not args.data_root.is_dir():
        print(f"ERROR: {args.data_root} is not a directory", file=sys.stderr)
        return 2

    rc, reports = validate(args.data_root, expect_min_ts=args.expect_min_ts)
    if rc == 0 and args.quiet:
        return 0
    headers = {
        0: "OK",
        1: "ORPHANED ROWS FOUND",
        2: "DB UNREADABLE / CORRUPT",
        3: "DB STALE — missing recent data",
    }
    print("Snapshot validation: " + headers.get(rc, "FAILED"))
    print(_format_report(reports))
    return rc


if __name__ == "__main__":
    sys.exit(main())
