# CLAUDE.md — J105 Sailboat Data Logger

## Project Overview

A Raspberry Pi-based sailing data logger that reads from a B&G instrument system via Signal K
Server (which owns the NMEA 2000 / CAN bus), stores time-series sailing data in SQLite, and
provides a web interface for race marking, history, debrief audio, and performance exports.
Data can be exported as CSV, GPX, or JSON for use in Sailmon and other regatta analysis tools.

---

## Stack & Tooling

| Concern | Tool |
|---|---|
| Dependency management | `uv` |
| Data source (primary) | Signal K WebSocket via `websockets` (`sk_reader.py`) |
| NMEA 2000 / CAN (legacy) | `python-can`, `canboat` — `can_reader.py`, `DATA_SOURCE=can` |
| Storage | SQLite via `aiosqlite` (schema v18) |
| Web interface | `fastapi` + `uvicorn` |
| Audio recording | `sounddevice`, `soundfile` |
| Audio transcription | `faster-whisper`; optional diarisation via `pyannote-audio` |
| System monitoring | `psutil` + InfluxDB via `influxdb-client` |
| Linting + formatting | `ruff` (line length 100; `E501` suppressed only in `web.py`) |
| Type checking | `mypy` (strict) |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Logging | `loguru` |
| External data | `httpx` — Open-Meteo weather, NOAA CO-OPS tides |
| YouTube metadata | `yt-dlp` |

---

## Project Structure

```
j105-logger/
├── CLAUDE.md
├── README.md
├── pyproject.toml          # uv-managed; single source of truth for deps & config
├── .python-version         # pins Python 3.12
├── .env.example            # canonical env var reference
│
├── src/
│   └── logger/
│       ├── __init__.py
│       ├── main.py         # CLI entry point; wires modules together, starts async loop
│       ├── audio.py        # USB audio recording (Gordik / any UAC device)
│       ├── can_reader.py   # CAN bus interface — legacy direct-CAN path only
│       ├── export.py       # Export to CSV / GPX / JSON for regatta tools
│       ├── external.py     # Open-Meteo weather + NOAA CO-OPS tide fetching
│       ├── influx.py       # InfluxDB write helpers for system health metrics
│       ├── monitor.py      # psutil background task → InfluxDB every 60 s
│       ├── nmea2000.py     # PGN decoding dataclasses (used by both paths)
│       ├── races.py        # Race naming logic + RaceConfig dataclass
│       ├── auth.py         # Magic-link auth middleware; require_auth() dependency
│       ├── polar.py        # Polar performance baseline builder
│       ├── sk_reader.py    # Signal K WebSocket reader — primary data source
│       ├── storage.py      # SQLite read/write; schema migrations (currently v18)
│       ├── transcribe.py   # faster-whisper transcription + pyannote diarisation
│       ├── video.py        # YouTube video metadata / sync-point logic
│       └── web.py          # FastAPI app — race marker, history, boats, admin UI
│
├── tests/
│   ├── conftest.py
│   ├── test_audio.py
│   ├── test_export.py
│   ├── test_external.py
│   ├── test_nmea2000.py
│   ├── test_races.py
│   ├── test_sk_reader.py
│   ├── test_storage.py
│   ├── test_transcribe.py
│   ├── test_video.py
│   └── test_web.py
│
├── data/                   # SQLite DB, WAV files, exports (gitignored)
├── scripts/                # deploy.sh, setup.sh, grafana provisioning
└── docs/                   # Architecture notes, PGN mappings, guides
```

---

## Common Commands

```bash
# Install dependencies
uv sync

# Run the logger (installed CLI entry point — preferred)
j105-logger run

# Equivalently (also works without installing):
uv run python -m logger.main run

# Other CLI subcommands
j105-logger status            # show database row counts and last timestamps
j105-logger export --start "2025-08-10T13:00:00" --end "2025-08-10T15:30:00" --out data/race1.csv
j105-logger list-audio        # list recorded WAV sessions
j105-logger list-devices      # list available audio input devices
j105-logger list-videos       # list linked YouTube videos
j105-logger link-video --url <url> --sync-utc <utc> --sync-offset <seconds>
j105-logger add-user --email <email> --name <name> --role admin|crew|viewer  # create user (no email required)
j105-logger build-polar --min-sessions 3  # rebuild polar performance baseline
j105-logger --help            # full subcommand list

# Run tests (coverage report printed by default via pyproject.toml addopts)
uv run pytest

# Lint & format check
uv run ruff check .
uv run ruff format --check .

# Auto-fix lint + format
uv run ruff check --fix .
uv run ruff format .

# Type check
uv run mypy src/
```

---

## Development Workflow

### Mac setup (one time)

```bash
# System audio libraries required by sounddevice / soundfile
brew install portaudio libsndfile

# Install Python dependencies
uv sync

# Create a local .env (no CAN hardware or Signal K needed for tests)
cp .env.example .env
```

The `.env` values that matter for local dev:

```
DB_PATH=data/logger.db
LOG_LEVEL=DEBUG
DATA_SOURCE=signalk   # tests mock the SK connection; value doesn't affect test runs
```

CAN bus and audio device env vars are ignored in tests — hardware access is isolated
in `can_reader.py` and `audio.py`, both of which are mocked.

### Daily dev loop

```bash
# Run the full test suite (no hardware required)
uv run pytest

# Lint + format check before pushing
uv run ruff check .
uv run ruff format --check .
uv run mypy src/

# Auto-fix lint and formatting
uv run ruff check --fix .
uv run ruff format .
```

All of the above run cleanly on a Mac with no Pi, CAN bus, Signal K, or
InfluxDB/Grafana required.

### PR workflow

1. Create a feature branch off `main`:
   ```bash
   git checkout main && git pull
   git checkout -b feature/my-feature
   ```
2. Develop and test locally until `uv run pytest` and the lint/type checks pass.
3. Push and open a pull request:
   ```bash
   git push -u origin feature/my-feature
   gh pr create
   ```
4. When the PR is ready and tests pass, merge to `main` and delete the branch.

### Deploying to the Raspberry Pi

After a PR merges to `main`, SSH into the Pi and run the deploy script:

```bash
ssh weaties@corvopi
cd ~/j105-logger
./scripts/deploy.sh
```

The script pulls `main`, syncs Python dependencies, re-applies Tailscale Funnel
routes, updates `PUBLIC_URL` in `.env`, and restarts `j105-logger`. It prints
the service status at the end so you can confirm everything came up cleanly.

> **Heads up**: if systemd service unit files or apt packages changed (rare),
> run the full idempotent setup script instead:
> ```bash
> ./scripts/setup.sh && sudo systemctl daemon-reload && sudo systemctl restart j105-logger
> ```

---

## Coding Conventions

- **Python 3.12+** — use modern syntax: `match`, `X | Y` unions, `tomllib`, etc.
- **Type hints everywhere** — all functions must have fully annotated signatures. Run mypy clean.
- **Ruff is the single formatter and linter** — do not introduce black, isort, or flake8. Line length is 100 chars; `E501` is suppressed only in `web.py` for inline HTML.
- **Modules are small and single-purpose** — if a module is growing beyond ~200 lines, split it.
- **Use `loguru` for all logging** — never use `print()` for operational output.
- **Dataclasses or `typing.TypedDict`** for structured data (e.g., decoded PGN records) — avoid raw dicts with unknown shapes.
- **Keep hardware-dependent code isolated** — direct CAN bus access lives only in `can_reader.py`; Signal K WebSocket access only in `sk_reader.py`. All other modules work with decoded data structures and can be tested without hardware.

---

## Deployed Service Architecture (Pi-specific)

On the Pi (`corvopi`), the service runs as a dedicated `j105logger` system account
(not as `weaties`). Key implications:

- The systemd unit has `User=j105logger` + `UV_CACHE_DIR=/var/cache/j105-logger` + `--no-sync`
- `data/` is owned by `j105logger:j105logger`; the rest of the project tree is read-only for it
- `.env` is `chmod 600 weaties:weaties`; systemd reads it as root before dropping privileges
- `sudo` access for `weaties` is scoped to specific service commands (see `/etc/sudoers.d/j105-logger-allowed`)
- InfluxDB binds to `127.0.0.1:8086` only; Grafana binds to `127.0.0.1:3001` only
- Signal K is on `*:3000`; exposed publicly via Tailscale Funnel at `/signalk/`
- **Two public ingress paths** — Tailscale Funnel (path stripping built-in) and Cloudflare Tunnel (routes via nginx on `127.0.0.1:8080` which strips `/grafana/` and `/signalk/` prefixes)
- nginx config for Cloudflare Tunnel: `/etc/nginx/conf.d/cloudflare-tunnel.conf` (managed by `deploy.sh`)
- Grafana auth: anonymous disabled; `GF_AUTH_ANONYMOUS_ENABLED=false` via systemd `Environment=`
- Signal K auth: `@signalk/sk-simple-token-security`; admin password in `~/.signalk-admin-pass.txt`

---

## Architecture Principles

- **Signal K is the primary data source**: `sk_reader.py` connects to the Signal K WebSocket (`ws://localhost:3000/signalk/v1/stream`). Signal K owns the CAN bus. The legacy direct-CAN path (`can_reader.py`) is available via `DATA_SOURCE=can` but not the default.
- **Hardware isolation**: neither `sk_reader.py` nor `can_reader.py` is imported outside of `main.py`. All other modules receive decoded data structures, not raw frames or SK deltas.
- **Decode early, store clean**: raw instrument data is decoded to named dataclasses as soon as it arrives. Nothing downstream handles raw bytes or SK JSON.
- **SQLite is the single source of truth**: all data — instrument, external, video metadata, audio sessions, transcripts — is written to SQLite with a UTC timestamp. Export and web functions read from SQLite, never from live data.
- **Timestamps are always UTC**: store and compute in UTC. Convert to local time only at display/export boundaries.
- **External data is async-friendly**: use `httpx` with async for weather/tide fetching during logging runs.

---

## Key Data: Important NMEA 2000 PGNs (B&G)

| PGN | Description |
|---|---|
| 127250 | Vessel Heading |
| 128259 | Speed Through Water (boatspeed) |
| 128267 | Water Depth |
| 129025 | Position (Lat/Lon rapid update) |
| 129026 | COG & SOG (GPS) |
| 130306 | Wind Data (speed + angle) |
| 130310 | Environmental (water temp) |

These PGNs arrive via Signal K deltas (as `value` fields on SK paths). The
`nmea2000.py` dataclasses are used in both the Signal K and direct-CAN paths.
Document any B&G-specific proprietary PGNs in `docs/pgn-notes.md` as discovered.

---

## Dos and Don'ts

**Do:**
- **Commit and push every change** — after editing any file (code, config, scripts), always commit and push to the current branch immediately. This is especially critical for hotfixes on the Pi — uncommitted changes on the device will be lost on the next deploy. Never leave work uncommitted.
- Write tests for all decoding and export logic
- Use `uv add <package>` to add dependencies — never edit `pyproject.toml` manually for deps
- Keep the SQLite schema versioned with simple integer migrations in `storage.py`
- Log every read error and decode failure with `loguru` at `WARNING` or above
- Export data in standard formats first (CSV with standard column names) before custom formats

**Don't:**
- Don't parse NMEA 2000 PGNs manually from scratch — use `canboat` or a library; only write custom decoders when necessary
- Don't store data in memory across long runs — flush to SQLite frequently to survive crashes/reboots
- Don't hardcode device paths (e.g., `/dev/can0`) — use config or environment variables
- Don't mix business logic into `main.py` — it should only wire things together and start the loop
- Don't commit the `data/` directory or any `.db` files

---

## Environment & Configuration

Configuration is via environment variables or a `.env` file (loaded with `python-dotenv`).
The canonical reference is `.env.example`.

```
# CAN / Signal K
CAN_INTERFACE=can0          # CAN bus interface name (legacy direct-CAN path)
CAN_BITRATE=250000          # NMEA 2000 standard bitrate
DATA_SOURCE=signalk         # signalk (default) or can (legacy)
SK_HOST=localhost            # Signal K server hostname
SK_PORT=3000                 # Signal K WebSocket port

# Storage
DB_PATH=data/logger.db      # SQLite database path
LOG_LEVEL=INFO              # loguru log level

# Audio recording
# AUDIO_DEVICE=Gordik       # name substring or integer index; omit to auto-detect
AUDIO_DIR=data/audio        # where WAV files are saved
AUDIO_SAMPLE_RATE=48000     # sample rate in Hz
AUDIO_CHANNELS=1            # 1=mono, 2=stereo

# Audio transcription
WHISPER_MODEL=base          # faster-whisper model: tiny, base, small, medium, large
# HF_TOKEN=hf_...           # Hugging Face token — enables speaker diarisation (optional)

# Photo notes
NOTES_DIR=data/notes        # where uploaded photo notes are saved

# Web interface
WEB_HOST=0.0.0.0            # bind address
WEB_PORT=3002               # http://corvopi:3002 on Tailscale
# WEB_PIN=                  # reserved, not yet implemented

# Authentication
# AUTH_DISABLED=true          # bypass auth (local dev only; never with Tailscale Funnel active)
AUTH_SESSION_TTL_DAYS=90      # session cookie lifetime
# ADMIN_EMAIL=you@example.com # auto-create admin user on startup

# Grafana deep-link buttons in the web UI
GRAFANA_URL=http://corvopi:3001
GRAFANA_DASHBOARD_UID=j105-sailing

# InfluxDB — required only for system health metrics; omit if not using InfluxDB
# INFLUX_URL=http://localhost:8086
# INFLUX_TOKEN=<token from ~/influx-token.txt>
# INFLUX_ORG=j105
# INFLUX_BUCKET=signalk
```

---

## Testing Strategy

- Unit tests live in `tests/` and run on any machine (no Pi hardware required)
- `conftest.py` provides in-memory SQLite fixtures and sample decoded data structures
- Hardware-dependent modules (`audio.py`, `can_reader.py`, `sk_reader.py`) are mocked in tests
- `test_web.py` uses `httpx.AsyncClient` with `ASGITransport` to exercise all API routes
- High-coverage targets: `storage.py`, `web.py`, `export.py`, `races.py`, `sk_reader.py`, `transcribe.py`
- **Pre-existing mypy errors in `web.py`** (do not fix unless explicitly asked):
  - `Item "None" of "datetime | None" has no attribute "isoformat"`
  - `Item "None" of "AudioRecorder | None" has no attribute "stop"` (×2)
