# Web UI screenshots (shot-scraper)

Headless, reproducible screenshots of the HelmLog web UI using
[shot-scraper](https://shot-scraper.datasette.io/). Dev/CI only — never install
on the Pi (it pulls Playwright + a browser).

## Usage

```bash
uv sync                                # the venv must exist (serve.py uses .venv/bin/python)
uvx shot-scraper install               # one-time: download the Playwright browser

# Empty auto-created DB (structure only, no data):
uvx shot-scraper multi scripts/screenshots/shots.yml

# Against a real logger.db (populated pages):
SHOTS_DB=/path/to/logger.db uvx shot-scraper multi scripts/screenshots/shots.yml
```

PNGs land in the current directory by the `output:` names in `shots.yml`. Move
them to `docs/screenshots/` (gitignored) if you want to keep them locally.

## How it works

`shots.yml`'s first entry is a `server:` block that runs `serve.py`, which:

- builds a `Storage`, connects, and serves `create_app(storage)` on `WEB_PORT`
  (default 3010) — mirroring `main._web_loop`, but without any hardware readers;
- sets `AUTH_DISABLED=true` so every page is reachable with no magic-link login;
- **snapshots the DB read-only** via SQLite's backup API into a temp file, so a
  run never mutates the live logger DB.

A `/healthz` gate then waits for boot (schema migrations on an empty DB take a
few seconds) before the shots run.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `SHOTS_DB` | `data/logger.db` | DB to snapshot and serve. Point at a real DB for populated shots. |
| `WEB_PORT` | `3010` | Port `serve.py` binds (must match the URLs in `shots.yml`). |
| `SHOTS_SKIP_MIGRATE` | unset | Serve the snapshot **as-is**, skipping migrations. Only needed for very old snapshots that predate a breaking migration; some pages may 500. A current Pi DB is already at head, so migrate is a no-op there. |

## ⚠️ Output is not committed

Screenshots shot against a populated DB contain **real crew names and audio
transcripts** — PII under `docs/data-licensing.md`. `docs/screenshots/` is
gitignored; regenerate locally rather than committing images.

## Gotchas baked into serve.py

- **WAL-mode copy** — the live DB runs in WAL; a plain file copy misses data in
  the `-wal` file. `serve.py` uses the backup API for a consistent snapshot.
- **Stale sidecars** — the temp path is fixed, so leftover `-wal`/`-shm` from a
  prior run pair with a fresh main file and yield `malformed database schema`.
  `serve.py` clears the sidecars before each copy.
- **Server cleanup** — shot-scraper kills only the direct child of a `server:`
  command, so `shots.yml` `exec`s the venv python directly (not via `uv run`),
  otherwise the python grandchild orphans and squats the port.
- **Client-side charts** — map/canvas pages fetch JSON after load with no ready
  flag, so shots use a fixed `wait:`. For sharper CI shots against a known DB,
  swap in a selector wait (e.g. `.leaflet-tile-loaded`).
