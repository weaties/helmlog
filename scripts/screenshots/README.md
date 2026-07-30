# Web UI screenshots (shot-scraper)

Headless, reproducible screenshots of the HelmLog web UI using
[shot-scraper](https://shot-scraper.datasette.io/). Dev/CI only — never install
on the Pi (it pulls Playwright + a browser).

## Usage

```bash
uv sync                                # the venv must exist (serve.py uses .venv/bin/python)
uvx shot-scraper install               # one-time: download the Playwright browser

# Populated pages from synthetic, PII-free data (recommended — same as CI):
uv run python scripts/screenshots/seed_demo.py --db /tmp/demo.db --sessions 4
SHOTS_DB=/tmp/demo.db uvx shot-scraper multi scripts/screenshots/shots.yml

# Structure only, no data (empty auto-created DB):
uvx shot-scraper multi scripts/screenshots/shots.yml

# Against a real logger.db (contains PII — local eyeballing only, never commit):
SHOTS_DB=/path/to/logger.db uvx shot-scraper multi scripts/screenshots/shots.yml
```

PNGs land in the current directory by the `output:` names in `shots.yml`. Move
them to `docs/screenshots/` (gitignored) if you want to keep them locally.

## Synthetic demo data (`seed_demo.py`)

`seed_demo.py` builds a disposable DB populated entirely with **synthesised**
data using HelmLog's own simulator (the same pipeline as the "Synthesize Race"
web action) — track, gauges, wind, maneuvers, polar baseline, sails, and a few
moments — so pages render realistically with **zero PII**. This is what CI
shoots. No real crew names or audio transcripts are ever produced.

## CI (`.github/workflows/screenshots.yml`)

On push to `main`, CI seeds a synthetic DB, captures the pages, and commits the
PNGs to the orphan **`screenshots`** branch — one commit per build. That gives a
git-diffable visual history: `git diff` between two builds (or against the last
`stage/*` tag) shows exactly which pages changed, which is how release notes can
point at where a feature landed. The `screenshots` branch renders as a gallery
(`README.md` with all shots) on GitHub.

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
