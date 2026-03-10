# CLAUDE.md — HelmLog

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
| Storage | SQLite via `aiosqlite` (schema v28) |
| Web interface | `fastapi` + `uvicorn` + `jinja2` templates |
| Audio recording | `sounddevice`, `soundfile` |
| Audio transcription | `faster-whisper`; optional diarisation via `pyannote-audio` |
| System monitoring | `psutil` + InfluxDB via `influxdb-client` |
| Linting + formatting | `ruff` (line length 100; `E501` suppressed only in `web.py`) |
| Type checking | `mypy` (strict) |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Logging | `loguru` |
| External data | `httpx` — Open-Meteo weather, NOAA CO-OPS tides |
| YouTube metadata | `yt-dlp` |
| Reverse proxy | `nginx` — single-port (80) access to all services |

---

## Project Structure

```
helmlog/
├── CLAUDE.md
├── README.md
├── pyproject.toml          # uv-managed; single source of truth for deps & config
├── .python-version         # pins Python 3.12
├── .env.example            # canonical env var reference
│
├── src/
│   └── helmlog/
│       ├── __init__.py
│       ├── main.py         # CLI entry point; wires modules together, starts async loop
│       ├── audio.py        # USB audio recording (Gordik / any UAC device)
│       ├── auth.py         # Magic-link auth middleware; require_auth() dependency
│       ├── cameras.py      # Insta360 X4 camera control via OSC HTTP API
│       ├── can_reader.py   # CAN bus interface — legacy direct-CAN path only
│       ├── deploy.py       # Self-update / deploy management logic
│       ├── email.py        # SMTP email sending (welcome, new-device alerts)
│       ├── export.py       # Export to CSV / GPX / JSON for regatta tools
│       ├── external.py     # Open-Meteo weather + NOAA CO-OPS tide fetching
│       ├── federation.py   # Boat identity (Ed25519), co-op membership, signing
│       ├── gaigps.py       # GaiGPS integration
│       ├── influx.py       # InfluxDB write helpers for system health metrics
│       ├── insta360.py     # Insta360 / local video metadata extraction + race matching
│       ├── monitor.py      # psutil background task → InfluxDB every 60 s
│       ├── nmea2000.py     # PGN decoding dataclasses (used by both paths)
│       ├── peer_api.py     # FastAPI router for inter-boat peer API endpoints
│       ├── peer_auth.py    # Ed25519 request signing and verification middleware
│       ├── peer_client.py  # Async HTTP client for querying peer boats
│       ├── pipeline.py     # Video processing pipeline orchestration
│       ├── polar.py        # Polar performance baseline builder
│       ├── race_classifier.py  # Automated race/practice session classification
│       ├── races.py        # Race naming logic + RaceConfig dataclass
│       ├── sk_reader.py    # Signal K WebSocket reader — primary data source
│       ├── storage.py      # SQLite read/write; schema migrations
│       ├── transcribe.py   # faster-whisper transcription + pyannote diarisation
│       ├── triggers.py     # Event triggers (auto-start/stop sessions)
│       ├── video.py        # YouTube video metadata / sync-point logic
│       ├── web.py          # FastAPI app — route handlers and API endpoints
│       ├── youtube.py      # YouTube upload and API integration
│       │
│       ├── templates/      # Jinja2 HTML templates (extends base.html)
│       │   ├── base.html   # Base layout — nav, footer, CSS/JS includes
│       │   ├── home.html   # Home / race control page
│       │   ├── history.html
│       │   ├── login.html  # Standalone (no base.html)
│       │   ├── profile.html
│       │   ├── session.html  # Dedicated session detail page
│       │   └── admin/      # Admin pages (boats, users, audit, cameras, events, settings)
│       │
│       └── static/         # CSS and JS served by FastAPI StaticFiles
│           ├── base.css    # Shared styles for all pages
│           ├── shared.js   # Shared JS utilities (fmtTime, initNav, etc.)
│           ├── home.js     # Home page logic
│           ├── history.js  # History page logic
│           └── session.js  # Session detail page logic
│
├── tests/                  # pytest suite — runs on any machine, no hardware required
│   └── integration/        # Federation integration tests (two-boat simulation)
│       ├── conftest.py     # Fleet fixture — two boats with real Ed25519 keypairs
│       ├── seed.py         # Test data seeding (co-op, sessions, instrument data)
│       ├── test_federation_e2e.py   # Co-op lifecycle, session list, track fetch
│       ├── test_auth_e2e.py         # Signing, replay, forgery, non-member
│       ├── test_embargo_e2e.py      # Embargo enforcement and sharing lifecycle
│       ├── test_data_license_e2e.py # Field allowlist, PII protection, audit
│       ├── Dockerfile       # Minimal helmlog image for Docker-based testing
│       ├── docker-compose.yml  # Two-container boat-a + boat-b + test-runner
│       └── serve.py         # Entry point for Docker container web server
├── data/                   # SQLite DB, WAV files, exports (gitignored)
├── scripts/                # deploy.sh, setup.sh, transcribe_worker.py
│   └── integration_smoke.py  # Pi-to-Pi smoke tests over Tailscale
└── docs/                   # Guides, policies, and technical specs
```

---

## Common Commands

```bash
uv sync                     # install dependencies
uv run pytest               # run tests (coverage printed by default)
uv run pytest tests/integration/ -v  # run federation integration tests
uv run ruff check .         # lint check
uv run ruff format --check .  # format check
uv run mypy src/            # type check
uv run ruff check --fix . && uv run ruff format .  # auto-fix

helmlog run             # start the logger
helmlog status          # show database row counts
helmlog list-cameras    # show configured cameras and ping status
helmlog identity init   # generate Ed25519 keypair + boat card
helmlog identity show   # display current boat identity and fingerprint
helmlog co-op create    # create a new co-op with this boat as moderator
helmlog co-op status    # show co-op membership and peers
helmlog co-op invite    # generate an invite bundle for a new boat
helmlog --help          # full subcommand list
```

---

## Development Workflow

### Mac setup (one time)

```bash
brew install portaudio libsndfile
uv sync
cp .env.example .env        # edit DB_PATH, LOG_LEVEL as needed
```

### Daily dev loop

Follow TDD (see `/tdd` skill): write a failing test, implement, pass, lint, commit.

```bash
uv run pytest               # all tests pass
uv run ruff check .         # lint clean
uv run ruff format --check .  # format clean
uv run mypy src/            # types clean
```

### Issue → PR workflow

When starting work on a GitHub issue:

1. **Mark the issue in-progress**: add a comment with the branch name and who/what is working on it:
   ```bash
   gh issue comment <number> --body "In progress on \`<branch-name>\` (Claude Code on <hostname>)"
   ```
2. Branch off `main`: `git checkout -b feature/my-feature main`
3. Develop with TDD until tests + lint + types pass
4. Push and create PR: `git push -u origin feature/my-feature && gh pr create`
5. Merge to `main` and delete the branch

**All changes to `main` must come through merged PRs.** Never push directly to `main`.

### Environment & Configuration

All config is via environment variables or `.env`. The canonical reference
is `.env.example` — read it for the full list of available settings.

---

## Coding Conventions

- **Python 3.12+** — use modern syntax: `match`, `X | Y` unions, `tomllib`, etc.
- **Type hints everywhere** — all functions must have fully annotated signatures. Run mypy clean.
- **Ruff is the single formatter and linter** — do not introduce black, isort, or flake8. Line length is 100 chars; `E501` is suppressed only in `web.py` for inline HTML.
- **Modules are small and single-purpose** — if a module is growing beyond ~200 lines, split it.
- **Use `loguru` for all logging** — never use `print()` for operational output.
- **Dataclasses or `typing.TypedDict`** for structured data — avoid raw dicts with unknown shapes.
- **Keep hardware-dependent code isolated** — direct CAN bus access lives only in `can_reader.py`; Signal K WebSocket access only in `sk_reader.py`; camera HTTP control only in `cameras.py`. All other modules work with decoded data structures and can be tested without hardware.

---

## Architecture Principles

- **Signal K is the primary data source**: `sk_reader.py` connects to the Signal K WebSocket. Signal K owns the CAN bus. The legacy direct-CAN path (`can_reader.py`) is available via `DATA_SOURCE=can` but not the default.
- **Hardware isolation**: hardware modules are only imported by `main.py`. All other modules receive decoded data structures, not raw frames or SK deltas.
- **Decode early, store clean**: raw instrument data is decoded to named dataclasses as soon as it arrives. Nothing downstream handles raw bytes or SK JSON.
- **SQLite is the single source of truth**: all data is written to SQLite with a UTC timestamp. Export and web functions read from SQLite, never from live data.
- **Timestamps are always UTC**: store and compute in UTC. Convert to local time only at display/export boundaries.
- **External data is async-friendly**: use `httpx` with async for weather/tide fetching during logging runs.

---

## Data Licensing Policy

The data licensing policy (`docs/data-licensing.md`) governs all data ownership,
sharing, and privacy. **All code that touches user data must comply with this
policy.** Key constraints that affect development:

- **Boat owns its data** — never restrict a boat's ability to export its own
  data in CSV, GPX, JSON, or WAV
- **PII categories** — audio, photos, emails, biometrics, and diarized
  transcripts are PII with deletion/anonymization rights. Code handling PII
  must support these rights
- **Co-op data is view-only** — API endpoints serving other boats' co-op data
  must not support bulk export. Audit logging and rate limiting required
- **Temporal sharing** — session data may be under co-op embargo. Check embargo
  timestamps before serving track data
- **Gambling prohibition** — no feature may facilitate betting or wagering use
  of co-op data
- **Protest firewall** — do not build export formats for co-op data designed for
  protest committee submission
- **Biometric data** — requires per-person consent separate from instrument
  data. Cannot be used in personnel decisions. Coaches need separate
  authorization from the individual, not just the boat owner

Use `/data-license` to review code changes against the full policy.

---

## Dos and Don'ts

**Do:**
- **All changes to `main` must come through merged PRs** — never push directly to `main`
- **Follow TDD** — write a failing test before implementing new functionality (see `/tdd` skill)
- **Commit and push every change** — after editing any file (code, config, scripts), always commit and push to the current branch immediately. This is especially critical for hotfixes on the Pi — uncommitted changes on the device will be lost on the next deploy. Never leave work uncommitted.
- Write tests for all decoding and export logic
- Run integration tests (`uv run pytest tests/integration/ -v`) for any federation/co-op/peer API changes
- Use `uv add <package>` to add dependencies — never edit `pyproject.toml` manually for deps
- **After adding a dependency**, always run `uv sync` to install it, then verify the import works. On the Pi, also restart the helmlog service (`sudo systemctl restart helmlog`). Never use `uv pip install` for project dependencies — it bypasses the lockfile
- **After pulling or switching branches**, always run `uv sync` (or `./scripts/deploy.sh` on the Pi) to ensure new dependencies are installed. The helmlog service runs with `--no-sync` and trusts the venv is already correct
- Keep the SQLite schema versioned with simple integer migrations in `storage.py`
- Log every read error and decode failure with `loguru` at `WARNING` or above

**Don't:**
- **Never push directly to `main`** — `main` is sacrosanct. Always work on a feature branch and merge via PR. If on the Pi and a hotfix is needed, create or use an existing branch, commit and push there, then merge through GitHub.
- Don't parse NMEA 2000 PGNs manually from scratch — use `canboat` or a library; only write custom decoders when necessary
- Don't store data in memory across long runs — flush to SQLite frequently to survive crashes/reboots
- Don't hardcode device paths (e.g., `/dev/can0`) — use config or environment variables
- Don't mix business logic into `main.py` — it should only wire things together and start the loop
- Don't commit the `data/` directory or any `.db` files
- Don't use `uv pip install` to install project dependencies — always use `uv add` (to add) or `uv sync` (to install from lockfile). `uv pip install` bypasses the lockfile and won't persist across `uv sync` runs

---

## Testing Strategy

### Unit tests (`tests/`)

- Run on any machine (no Pi hardware required)
- `conftest.py` provides in-memory SQLite fixtures and sample decoded data structures
- Hardware-dependent modules are mocked in tests
- `test_web.py` uses `httpx.AsyncClient` with `ASGITransport` to exercise all API routes
- **Pre-existing mypy errors in `web.py`** (do not fix unless explicitly asked):
  - `Item "None" of "datetime | None" has no attribute "isoformat"`
  - `Item "None" of "AudioRecorder | None" has no attribute "stop"` (x2)

### Integration tests (`tests/integration/`)

Three-layer strategy for validating inter-Pi federation, co-op, and data licensing:

**Layer 1 — In-process pytest** (runs in CI, ~5 seconds):
Two boats with real Ed25519 keypairs and in-memory SQLite, communicating via
`httpx.ASGITransport`. No mocking of crypto or auth — real signing, real
verification, real nonce replay protection. 32 tests covering:
- Co-op lifecycle (create, join, share, unshare, revoke)
- Ed25519 request auth (valid, tampered, forged, replayed, non-member)
- Embargo enforcement (blocked while active, accessible after lift)
- Data licensing (field allowlist, PII exclusion, private session isolation, audit)

```bash
uv run pytest tests/integration/ -v
```

**Layer 2 — Pi smoke tests** (corvopi-tst1 → corvopi-live over Tailscale):
Lightweight script that runs on one Pi and tests the real running helmlog
service on a peer Pi. Validates Tailscale networking, systemd, NTP sync.

```bash
ssh weaties@corvopi-tst1 "cd ~/helmlog && uv run python scripts/integration_smoke.py --peer corvopi-live"
```

**Layer 3 — Docker compose** (two containers on Mac, arm64 capable):
Two real helmlog instances on an isolated Docker network. Useful for testing
process isolation, network failure scenarios, and Pi-matching architecture.

```bash
docker compose -f tests/integration/docker-compose.yml up --build --abort-on-container-exit
```

**When to run integration tests:**
- Any PR touching `federation.py`, `peer_api.py`, `peer_auth.py`, `peer_client.py`,
  or federation-related storage code → Layer 1 runs automatically in CI
- Federation PRs before merge → Layer 2 (Pi smoke) as manual validation
- Use `/integration-test` skill to run the appropriate layer

---

## Skills (on-demand workflows)

| Skill | Purpose |
|---|---|
| `/tdd` | Test-driven development cycle (red-green-refactor) |
| `/new-module` | Scaffold a new hardware-isolated module with tests |
| `/new-migration` | Add a SQLite schema migration to storage.py |
| `/deploy-pi` | Pi deployment reference and service architecture |
| `/pr-checklist` | Pre-PR verification (tests, lint, types, docs) |
| `/data-license` | Review changes against the data licensing policy |
| `/integration-test` | Run federation integration tests (Layer 1/2/3) |
