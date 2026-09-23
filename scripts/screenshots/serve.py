"""Boot the HelmLog web UI headlessly for shot-scraper (prototype).

The FastAPI app is factory-built (`create_app(storage)`) and normally only
runs inside `helmlog run`, which also starts CAN/Signal K/audio hardware. For
screenshots we want the web layer *without* the hardware, so this launcher
mirrors `main._web_loop`: build a Storage, connect it, `create_app`, serve.

- Auth is bypassed with AUTH_DISABLED=true (every request is treated as admin),
  so shot-scraper reaches every page without a magic-link session.
- The DB is *copied to a temp file* first, so screenshotting never mutates the
  real logger DB. Point SHOTS_DB at a real/sample DB for populated screenshots;
  with no DB present the schema auto-creates and pages render (mostly) empty.

Run standalone to eyeball it:

    AUTH_DISABLED=true SHOTS_DB=data/logger.db uv run python scripts/screenshots/serve.py

shot-scraper drives it via the `server:` block in shots.yml — you don't call
this directly for a screenshot run.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

import uvicorn
from loguru import logger


def _prepare_db() -> str:
    """Return a temp DB path: a WAL-consistent copy of SHOTS_DB/DB_PATH if it
    exists, else a fresh file the schema will auto-create into. Never serve the
    live DB.

    Two footguns handled here:
    - The live DB runs in WAL mode, so a plain file copy misses data still in
      the ``-wal`` file. We use SQLite's backup API for a consistent snapshot.
    - The temp path is fixed, so a stale ``-wal``/``-shm`` from a prior run pairs
      with a fresh main file and yields "malformed database schema". Clear the
      sidecars first.
    """
    src = os.environ.get("SHOTS_DB") or os.environ.get("DB_PATH", "data/logger.db")
    tmp = Path(tempfile.gettempdir()) / "helmlog-shots.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)

    if Path(src).exists():
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(tmp))
        try:
            src_conn.backup(dst_conn)  # WAL-consistent single-file snapshot
        finally:
            dst_conn.close()
            src_conn.close()
        logger.info("shots: serving a snapshot of {} ({} bytes)", src, tmp.stat().st_size)
    else:
        logger.warning("shots: {} not found — serving an empty auto-created DB", src)
    return str(tmp)


async def main() -> None:
    os.environ.setdefault("AUTH_DISABLED", "true")

    from helmlog.storage import Storage, StorageConfig
    from helmlog.web import create_app

    storage = Storage(StorageConfig(db_path=_prepare_db()))

    # A live Pi DB is always at the current schema, so connect()'s migrate step
    # is a no-op there. Old *snapshots* (e.g. a months-old logger.db) are behind
    # and running the migration/schema-repair chain against them can fail on
    # non-idempotent DDL. SHOTS_SKIP_MIGRATE serves the snapshot as-is so you can
    # still screenshot it — pages that need newer columns may 500, most won't.
    if os.environ.get("SHOTS_SKIP_MIGRATE"):

        async def _no_migrate() -> None:
            logger.warning("shots: SHOTS_SKIP_MIGRATE set — serving DB as-is, migrations skipped")

        storage.migrate = _no_migrate  # type: ignore[method-assign]

    await storage.connect()

    # Log the latest session URL so you know what to point a /session shot at.
    try:
        db = storage._read_conn()  # noqa: SLF001 — prototype convenience
        cur = await db.execute("SELECT id, slug FROM races ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        if row is not None:
            slug = row["slug"]
            path = f"/session/{row['id']}/{slug}" if slug else f"/session/{row['id']}"
            logger.info("shots: latest session page → {}", path)
        else:
            logger.info("shots: no races in DB — /session/N shots will 404")
    except Exception as exc:  # pragma: no cover - diagnostics only
        logger.warning("shots: could not resolve latest session ({})", exc)

    port = int(os.environ.get("WEB_PORT", "3002"))
    app = create_app(storage)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    logger.info("shots: HelmLog web UI on http://127.0.0.1:{}  (AUTH_DISABLED)", port)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
