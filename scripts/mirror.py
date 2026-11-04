"""Mac mirror of the boat's HelmLog — web-only server + refresh helper.

Serves a read-mostly copy of the Pi's database so races can be reviewed (and
features demoed) while the Pi is offline on the water. The copy is kept fresh
by ``scripts/mirror-sync.sh``; see ``docs/mirror.md`` for the full setup.

Two subcommands, both run with the mirror root (``~/helmlog-mirror``) as the
working directory so the DB's relative ``data/audio/...`` paths resolve:

``serve``
    Start only the FastAPI app — no Signal K reader, audio, cameras, weather
    or deploy loops. Writes that would act on the host (deploy, network
    profiles) are refused with 403: on the mirror they would run ``git`` /
    ``nmcli`` against the Mac, not the boat.

``carry OLD NEW``
    Copy the rows the mirror itself created (login sessions, coach grants)
    from the previously served DB into the freshly synced one, so a refresh
    doesn't log everyone out or kill a coach link mid-demo. Rows the Pi also
    has win; mirror-side edits to Pi rows are dropped. Notes and other data
    written on the mirror are not carried back — see docs/mirror.md.

Usage::

    cd ~/helmlog-mirror && uv run --project <checkout> python <tools>/scripts/mirror.py serve
    uv run --project <checkout> python <tools>/scripts/mirror.py carry old.db new.db

The script only imports ``helmlog.storage`` / ``helmlog.web`` / ``helmlog.cache``,
so it can serve any branch's checkout (e.g. a feature branch being demoed).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from fastapi import Request

# Non-GET requests under these prefixes are refused on the mirror.
BLOCKED_WRITE_PREFIXES: tuple[str, ...] = ("/api/deployment", "/api/network")

# Coach grants have no stable id across Pi and mirror (AUTOINCREMENT), so a
# grant is matched on these columns instead.
_GRANT_KEY = ("kind", "label", "created_at")


def _columns(db: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")]


def _common_columns(db: sqlite3.Connection, table: str) -> list[str]:
    new_cols = _columns(db, "main", table)
    old_cols = set(_columns(db, "old", table))
    return [c for c in new_cols if c in old_cols]


def carry_mirror_rows(old_db: Path, new_db: Path) -> dict[str, int]:
    """Copy mirror-created auth sessions and coach grants from *old_db* into *new_db*.

    Returns the number of rows inserted per table. Tables missing from either
    DB (e.g. a Pi on a schema older than the coach-access feature) are skipped.
    """
    counts: dict[str, int] = {}
    db = sqlite3.connect(new_db, uri=True)
    try:
        db.execute("ATTACH DATABASE ? AS old", (f"file:{old_db.resolve()}?mode=ro",))
        tables_new = {r[0] for r in db.execute("SELECT name FROM main.sqlite_master")}
        tables_old = {r[0] for r in db.execute("SELECT name FROM old.sqlite_master")}

        def both(t: str) -> bool:
            return t in tables_new and t in tables_old

        if both("auth_sessions"):
            cols = ", ".join(_common_columns(db, "auth_sessions"))
            cur = db.execute(
                f"INSERT OR IGNORE INTO main.auth_sessions ({cols}) "
                f"SELECT {cols} FROM old.auth_sessions "
                "WHERE user_id IN (SELECT id FROM main.users)"
            )
            counts["auth_sessions"] = cur.rowcount

        if both("coach_grants"):
            match = " AND ".join(f"m.{k} = g.{k}" for k in _GRANT_KEY)
            db.execute(
                "CREATE TEMP TABLE local_grants AS SELECT * FROM old.coach_grants g "
                f"WHERE NOT EXISTS (SELECT 1 FROM main.coach_grants m WHERE {match})"
            )
            cols = ", ".join(c for c in _common_columns(db, "coach_grants") if c != "id")
            cur = db.execute(
                f"INSERT OR IGNORE INTO main.coach_grants ({cols}) "
                f"SELECT {cols} FROM temp.local_grants "
                "WHERE created_by IS NULL OR created_by IN (SELECT id FROM main.users)"
            )
            counts["coach_grants"] = cur.rowcount
            if both("coach_grant_sessions"):
                join = " AND ".join(f"m.{k} = l.{k}" for k in _GRANT_KEY)
                cur = db.execute(
                    "INSERT OR IGNORE INTO main.coach_grant_sessions (grant_id, session_id) "
                    "SELECT m.id, s.session_id FROM temp.local_grants l "
                    "JOIN old.coach_grant_sessions s ON s.grant_id = l.id "
                    f"JOIN main.coach_grants m ON {join} "
                    "WHERE s.session_id IN (SELECT id FROM main.races)"
                )
                counts["coach_grant_sessions"] = cur.rowcount
        db.commit()
    finally:
        db.close()
    return counts


def blocked_on_mirror(method: str, path: str) -> bool:
    """True if a request would act on the host machine rather than the boat."""
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return False
    return any(path == p or path.startswith(p + "/") for p in BLOCKED_WRITE_PREFIXES)


async def serve() -> None:
    """Run the web app alone against the local DB (DB_PATH, WEB_HOST, WEB_PORT)."""
    import uvicorn
    from fastapi.responses import JSONResponse

    from helmlog.cache import WebCache
    from helmlog.races import RaceConfig
    from helmlog.storage import Storage, StorageConfig
    from helmlog.web import create_app

    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    assert current is not None
    loop.add_signal_handler(signal.SIGTERM, current.cancel)

    storage = Storage(StorageConfig())
    await storage.connect()
    # Same settings seeding as `helmlog run`, so display prefs (TIMEZONE,
    # colour scheme, audio channel labels) match the boat. mirror.env wins.
    for row in await storage.list_settings():
        os.environ.setdefault(row["key"], row["value"])
    storage.bind_race_cache(WebCache(storage))

    app = create_app(storage)

    @app.middleware("http")
    async def _mirror_guard(request: Request, call_next: Any) -> Any:  # noqa: ANN401
        if blocked_on_mirror(request.method, request.url.path):
            return JSONResponse({"detail": "Disabled on the Mac mirror"}, status_code=403)
        return await call_next(request)

    cfg = RaceConfig()
    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")
    )
    server.install_signal_handlers = False  # type: ignore[attr-defined]
    logger.info(
        "Mirror web interface: http://{}:{} (db={})",
        cfg.web_host,
        cfg.web_port,
        os.environ.get("DB_PATH", "data/logger.db"),
    )
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
    finally:
        await storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Serve the web UI only, against the local mirror DB")
    carry = sub.add_parser("carry", help="Carry mirror-created rows into a refreshed DB")
    carry.add_argument("old_db", type=Path)
    carry.add_argument("new_db", type=Path)
    args = parser.parse_args()

    match args.command:
        case "serve":
            from dotenv import load_dotenv

            load_dotenv(Path.cwd() / ".env")  # the mirror root's .env, not the checkout's
            asyncio.run(serve())
        case "carry":
            counts = carry_mirror_rows(args.old_db, args.new_db)
            logger.info("Carried mirror rows: {}", counts or "none")


if __name__ == "__main__":
    main()
