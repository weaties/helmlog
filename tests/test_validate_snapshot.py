"""Tests for scripts/validate_snapshot.py — backup completeness guard (#676)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "scripts" / "validate_snapshot.py"

spec = importlib.util.spec_from_file_location("validate_snapshot", _SCRIPT)
assert spec is not None and spec.loader is not None
validate_snapshot = importlib.util.module_from_spec(spec)
sys.modules["validate_snapshot"] = validate_snapshot
spec.loader.exec_module(validate_snapshot)


def _make_db(data_root: Path) -> None:
    db_path = data_root / "logger.db"
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE moment_attachments (
            id INTEGER PRIMARY KEY, moment_id INTEGER, kind TEXT, path TEXT
        );
        CREATE TABLE audio_sessions (
            id INTEGER PRIMARY KEY, file_path TEXT
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, avatar_path TEXT
        );
        """
    )
    db.commit()
    db.close()


def test_all_present(tmp_path: Path) -> None:
    _make_db(tmp_path)
    (tmp_path / "notes" / "108").mkdir(parents=True)
    (tmp_path / "notes" / "108" / "a.jpg").write_bytes(b"x")
    db = sqlite3.connect(tmp_path / "logger.db")
    db.execute(
        "INSERT INTO moment_attachments(moment_id, kind, path) VALUES (1, 'photo', ?)",
        ("108/a.jpg",),
    )
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 0
    (ma,) = [r for r in reports if r.kind == "moment_attachments"]
    assert ma.total == 1 and ma.missing == 0


def test_missing_attachment_reported(tmp_path: Path) -> None:
    _make_db(tmp_path)
    db = sqlite3.connect(tmp_path / "logger.db")
    db.execute(
        "INSERT INTO moment_attachments(moment_id, kind, path) VALUES (1, 'photo', ?)",
        ("108/missing.jpg",),
    )
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 1
    (ma,) = [r for r in reports if r.kind == "moment_attachments"]
    assert ma.missing == 1
    assert "108/missing.jpg" in ma.samples


def test_absent_tables_tolerated(tmp_path: Path) -> None:
    """Older backups may lack audio_sessions / users tables — validator must
    not crash; those checks return 0/0 and overall status stays OK."""
    db_path = tmp_path / "logger.db"
    db = sqlite3.connect(db_path)
    db.executescript("CREATE TABLE moment_attachments (id INTEGER PRIMARY KEY, path TEXT);")
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 0
    kinds = {r.kind for r in reports}
    assert {"moment_attachments", "audio_sessions", "users.avatar_path"} == kinds


def test_missing_db_returns_rc_2(tmp_path: Path) -> None:
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 2
    assert reports == []


def _add_telemetry(data_root: Path, positions: list[str], races: list[str] | None = None) -> None:
    """Give the DB positions(ts) and races(start_utc) rows."""
    db = sqlite3.connect(data_root / "logger.db")
    db.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, ts TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS races (id INTEGER PRIMARY KEY, start_utc TEXT)")
    db.executemany("INSERT INTO positions(ts) VALUES (?)", [(t,) for t in positions])
    db.executemany("INSERT INTO races(start_utc) VALUES (?)", [(t,) for t in races or []])
    db.commit()
    db.close()


# ── #807: a corrupt or stale logger.db must be caught, not shrugged off ──────


@pytest.mark.parametrize(
    ("name", "blob"),
    [
        # The exact shape the 2026-07-14 backup produced: sqlite3 .backup ran
        # out of room on the Pi's 4 GB /tmp tmpfs and left a zeroed header.
        ("zeroed_header", b"\x00" * 8192),
        ("not_a_db", b"garbage" * 512),
    ],
)
def test_corrupt_db_returns_rc_2_without_raising(tmp_path: Path, name: str, blob: bytes) -> None:
    """A malformed DB must exit 2, not escape as an sqlite3.DatabaseError.

    The 2026-07-14 run crashed with an unhandled `database disk image is
    malformed` traceback, which the backup script then logged as a mere
    WARN — so a corrupt snapshot was reported as SUCCESS. (#807)
    """
    (tmp_path / "logger.db").write_bytes(blob)
    rc, reports = validate_snapshot.validate(tmp_path)
    assert rc == 2
    assert reports == []


def test_stale_db_returns_rc_3(tmp_path: Path) -> None:
    """A valid DB whose records lag the live DB is a failed backup.

    Validity is not enough; the copy must be faithful. (#807)
    """
    _make_db(tmp_path)
    _add_telemetry(tmp_path, ["2026-06-30T03:08:28.278000+00:00"])
    rc, _ = validate_snapshot.validate(
        tmp_path, expect_min_ts="positions=2026-07-14T03:45:55.911000+00:00"
    )
    assert rc == 3


def test_torn_copy_caught_per_table(tmp_path: Path) -> None:
    """One current table must not mask another that lags — compare PER TABLE.

    This is the real shape of `20260708T100002Z`: races current to 07-08 while
    positions stopped at 06-30 (race rows whose telemetry isn't there). A single
    max() across the whole DB reads 07-08 and calls it fresh; only a per-table
    comparison catches it. (#807)
    """
    _make_db(tmp_path)
    _add_telemetry(
        tmp_path,
        positions=["2026-06-30T03:08:28.278000+00:00"],  # 8 days behind
        races=["2026-07-08T07:26:54.186102+00:00"],  # current
    )
    rc, _ = validate_snapshot.validate(
        tmp_path,
        expect_min_ts=(
            "positions=2026-07-08T07:26:54.186102+00:00,races=2026-07-08T07:26:54.186102+00:00"
        ),
    )
    assert rc == 3


def test_fresh_db_passes_recency_check(tmp_path: Path) -> None:
    _make_db(tmp_path)
    _add_telemetry(tmp_path, ["2026-07-14T03:45:55.911000+00:00"])
    rc, _ = validate_snapshot.validate(
        tmp_path, expect_min_ts="positions=2026-07-14T03:45:55.911000+00:00"
    )
    assert rc == 0


def test_recency_check_skipped_without_expectation(tmp_path: Path) -> None:
    """No --expect-min-ts (or no such table) → recency is simply not judged.

    The boat sits idle for weeks between regattas, so absolute data age is a
    useless signal; we only ever compare against the live DB's own maxima.
    """
    _make_db(tmp_path)
    _add_telemetry(tmp_path, ["2026-01-01T00:00:00+00:00"])
    rc, _ = validate_snapshot.validate(tmp_path)
    assert rc == 0

    # An expectation naming a table this DB doesn't have is not a failure.
    rc, _ = validate_snapshot.validate(tmp_path, expect_min_ts="winds=2026-07-14T00:00:00+00:00")
    assert rc == 0


@pytest.mark.parametrize("abs_path", [True, False])
def test_audio_absolute_and_relative(tmp_path: Path, abs_path: bool) -> None:
    _make_db(tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    wav = audio_dir / "race42.wav"
    wav.write_bytes(b"riff")
    stored = str(wav) if abs_path else "audio/race42.wav"
    db = sqlite3.connect(tmp_path / "logger.db")
    db.execute("INSERT INTO audio_sessions(file_path) VALUES (?)", (stored,))
    db.commit()
    db.close()
    rc, reports = validate_snapshot.validate(tmp_path)
    (a,) = [r for r in reports if r.kind == "audio_sessions"]
    assert a.total == 1 and a.missing == 0
    assert rc == 0
