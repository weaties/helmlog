"""Tests for scripts/mirror.py — Mac mirror refresh carry-over + host guard."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "scripts" / "mirror.py"

spec = importlib.util.spec_from_file_location("mirror", _SCRIPT)
assert spec is not None and spec.loader is not None
mirror = importlib.util.module_from_spec(spec)
sys.modules["mirror"] = mirror
spec.loader.exec_module(mirror)

_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
CREATE TABLE races (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE auth_sessions (
    session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, ip TEXT, user_agent TEXT
);
CREATE TABLE coach_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, label TEXT NOT NULL,
    coach_email TEXT, token_hash TEXT UNIQUE, all_sessions INTEGER NOT NULL DEFAULT 0,
    allow_video INTEGER NOT NULL DEFAULT 0, landing_path TEXT, starts_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, revoked_at TEXT, created_by INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE coach_grant_sessions (
    grant_id INTEGER NOT NULL, session_id INTEGER NOT NULL, PRIMARY KEY (grant_id, session_id)
);
INSERT INTO users VALUES (1, 'skipper@example.com');
INSERT INTO races VALUES (10, 'R10'), (11, 'R11');
"""


def _grant(db: sqlite3.Connection, gid: int, label: str, token: str, created: str) -> None:
    db.execute(
        "INSERT INTO coach_grants (id, kind, label, token_hash, starts_at, expires_at,"
        " created_by, created_at) VALUES (?, 'link', ?, ?, 's', 'e', 1, ?)",
        (gid, label, token, created),
    )


@pytest.fixture
def dbs(tmp_path: Path) -> tuple[Path, Path]:
    """old = previously served mirror DB, new = fresh copy of the Pi DB."""
    old, new = tmp_path / "old.db", tmp_path / "new.db"
    for p in (old, new):
        db = sqlite3.connect(p)
        db.executescript(_SCHEMA)
        # Grant 1 came from the Pi and is in both.
        _grant(db, 1, "pi grant", "tok-pi", "2026-10-01")
        db.execute("INSERT INTO coach_grant_sessions VALUES (1, 10)")
        db.commit()
        db.close()
    db = sqlite3.connect(old)
    db.execute("INSERT INTO auth_sessions VALUES ('sess-mirror', 1, 'c', 'e', NULL, NULL)")
    db.execute("INSERT INTO auth_sessions VALUES ('sess-gone-user', 99, 'c', 'e', NULL, NULL)")
    # Grant 2 was created on the mirror.
    _grant(db, 2, "demo link", "tok-demo", "2026-10-17")
    db.execute("INSERT INTO coach_grant_sessions VALUES (2, 11)")
    db.commit()
    db.close()
    db = sqlite3.connect(new)
    # Meanwhile the Pi created its own grant 2 — same id, different grant.
    _grant(db, 2, "pi grant b", "tok-pi-b", "2026-10-16")
    db.commit()
    db.close()
    return old, new


def test_carry_keeps_mirror_logins(dbs: tuple[Path, Path]) -> None:
    old, new = dbs
    mirror.carry_mirror_rows(old, new)
    db = sqlite3.connect(new)
    ids = {r[0] for r in db.execute("SELECT session_id FROM auth_sessions")}
    assert ids == {"sess-mirror"}  # session for a user the Pi no longer has is dropped


def test_carry_remaps_mirror_grant_ids(dbs: tuple[Path, Path]) -> None:
    old, new = dbs
    counts = mirror.carry_mirror_rows(old, new)
    db = sqlite3.connect(new)
    grants = dict(db.execute("SELECT label, id FROM coach_grants").fetchall())
    assert set(grants) == {"pi grant", "pi grant b", "demo link"}
    assert grants["demo link"] not in (1, 2)  # no clash with the Pi's ids
    sessions = set(db.execute("SELECT grant_id, session_id FROM coach_grant_sessions"))
    assert sessions == {(1, 10), (grants["demo link"], 11)}
    assert counts == {"auth_sessions": 1, "coach_grants": 1, "coach_grant_sessions": 1}


def test_carry_is_idempotent(dbs: tuple[Path, Path]) -> None:
    old, new = dbs
    mirror.carry_mirror_rows(old, new)
    assert mirror.carry_mirror_rows(old, new) == {
        "auth_sessions": 0,
        "coach_grants": 0,
        "coach_grant_sessions": 0,
    }


def test_carry_skips_tables_missing_on_pi(tmp_path: Path) -> None:
    old, new = tmp_path / "old.db", tmp_path / "new.db"
    sqlite3.connect(old).executescript(_SCHEMA)
    sqlite3.connect(new).executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY); CREATE TABLE races (id INTEGER);"
    )
    assert mirror.carry_mirror_rows(old, new) == {}


@pytest.mark.parametrize(
    ("method", "path", "blocked"),
    [
        ("POST", "/api/deployment/deploy", True),
        ("PUT", "/api/deployment/config", True),
        ("POST", "/api/network/connect", True),
        ("DELETE", "/api/network/profiles/3", True),
        ("GET", "/api/deployment/status", False),
        ("POST", "/api/deploymentx", False),
        ("POST", "/api/races/1/notes", False),
    ],
)
def test_blocked_on_mirror(method: str, path: str, blocked: bool) -> None:
    assert mirror.blocked_on_mirror(method, path) is blocked
