# AGENTS.md — HelmLog

Raspberry Pi sailing data logger: reads B&G instruments via Signal K
(`sk_reader.py`), stores time-series data in SQLite, serves a FastAPI web UI
for race marking, history, debrief audio, and CSV/GPX/JSON export.

This file is the canonical project guide for **any** coding agent (Claude Code,
Cursor, Codex, Copilot, …) and for humans — the single source of truth for
conventions and workflow. Claude Code reads it via an `@AGENTS.md` import in
`CLAUDE.md`, which adds only the Claude-Code-specific mechanics (the worktree
tool, the skill catalog, file-based memory). References to `/name` below are
Claude Code skill shortcuts; the behavior they wrap is described inline or in
`docs/`, so any agent can follow the rule without them.

## Top rules — read first

- **Always work in a git worktree.** Multiple agents run concurrently in this
  repo. Two agents sharing a checkout collide on uncommitted changes and branch
  switches, and a deploy can pick up a half-finished hotfix from the wrong
  agent. Before any edit, check `git worktree list` and `ls .claude/worktrees/`;
  enter an existing worktree if the branch matches the task, otherwise create a
  new one (`git worktree add .claude/worktrees/<name> -b <branch>`). Read-only
  work (questions, architecture/diagnosis/domain lookups) does not need a
  worktree.
- **Never push directly to `main`.** All changes go through merged PRs on a
  feature branch. If a Pi hotfix is needed, branch + PR + merge — even then.
- **Always include `Closes #N`** (or `Fixes #N` for bugs) in the PR body so
  GitHub auto-closes the issue on merge and the tracker stays clean.
- **Apply the `in-progress` label** when starting work on an issue:
  `gh issue edit <N> --add-label "in-progress"` plus a comment naming the
  branch and host.
- **Commit and push every change immediately.** Uncommitted Pi edits are
  lost on the next deploy.

### Judgment rules (not just process)

- **Surface assumptions before building.** If the task is underspecified,
  state the assumption you're proceeding on rather than silently guessing.
- **Stop and ask when requirements conflict** — with an issue, with
  `docs/data-licensing.md`, or with existing behavior. Don't pick a side
  unasked.
- **Push back when warranted.** A worse plan you were handed is still worse;
  say so before implementing it.
- **Prefer boring, obvious solutions.** This is a Pi logger where a reboot
  must lose at most one record — reliability beats cleverness.
- **Touch only what you're asked to touch.** No drive-by refactors of
  adjacent code or unrelated TODOs; they widen the blast radius and the diff.

## Stack & tooling

| Concern | Tool |
|---|---|
| Dependency management | `uv` (never `uv pip install` for project deps — use `uv add` / `uv sync`) |
| Language | Python 3.12+ — modern syntax (`match`, `X | Y` unions, `tomllib`) |
| Data source (primary) | Signal K WebSocket via `websockets` (`sk_reader.py`) |
| NMEA 2000 / CAN (legacy) | `python-can` + `canboat` — `can_reader.py`, `DATA_SOURCE=can` |
| Storage | SQLite via `aiosqlite` |
| Web | `fastapi` + `uvicorn` + `jinja2`; `nginx` reverse proxy on :80 |
| Audio | `sounddevice`, `soundfile`; transcription `faster-whisper` (+ optional `pyannote-audio`) |
| Lint / format / types | `ruff` (line length 100), `mypy --strict` |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Logging | `loguru` (never `print()` for operational output) |
| External data | `httpx` — Open-Meteo weather, NOAA CO-OPS tides |
| Monitoring | `psutil` → InfluxDB |

## Project structure

```
src/helmlog/        # all Python; one module per concern. Key modules:
  main.py           #   CLI entry point — wiring only, no business logic
  sk_reader.py      #   Signal K WebSocket reader (primary data source)
  can_reader.py     #   CAN bus reader (legacy, DATA_SOURCE=can)
  storage.py        #   SQLite schema, migrations, read/write — single source of truth
  web.py            #   FastAPI routes and API endpoints
  auth.py           #   magic-link authentication
  federation.py     #   boat identity (Ed25519), co-op membership
  peer_api.py / peer_auth.py / peer_client.py  # inter-boat peer API + signing
  export.py         #   CSV / GPX / JSON export
  audio.py / transcribe.py   # USB audio recording + Whisper transcription
  polar.py          #   polar performance baseline
  templates/        #   Jinja2 (extends base.html)
  static/           #   CSS + JS
tests/              # pytest, runs anywhere; conftest.py provides in-memory Storage
tests/integration/  # federation/co-op (Layer 1 in-process; Layer 2 Pi harness; Layer 3 Docker)
docs/               # guides, policies, specs (data-licensing.md, testing-guide.md, …)
docs/archive/       # dated point-in-time docs (incidents, audits, calibrations)
scripts/            # deploy.sh, setup.sh, pi_harness.py, …
```

Use `ls`/`tree` for detail — don't ask the docs to enumerate every file.

## Common commands

```bash
uv sync                              # install deps from lockfile (run first)
uv run pytest                        # all tests + coverage
uv run pytest tests/integration/ -v  # federation tests
uv run ruff check . && uv run ruff format --check . && uv run mypy src/

helmlog run                          # start the logger
helmlog status                       # DB row counts
helmlog identity init|show           # Ed25519 boat identity
helmlog co-op create|status|invite   # co-op membership
helmlog --help                       # full subcommand list
```

All four checks (tests, ruff check, ruff format, mypy) must pass before a PR.

## Architecture principles

- **Signal K is the primary data source.** `sk_reader.py` is the default;
  legacy direct-CAN (`can_reader.py`) only via `DATA_SOURCE=can`.
- **Hardware isolation.** Hardware modules (`sk_reader.py`, `can_reader.py`,
  `cameras.py`) are imported only by `main.py`. Everything else receives
  decoded dataclasses, never raw frames or SK deltas — so it can be tested
  without hardware.
- **Decode early, store clean.** Raw bytes → frozen dataclass at the edge:

  ```python
  @dataclass(frozen=True)
  class HeadingRecord:
      pgn: int
      source_addr: int
      timestamp: datetime
      heading_deg: float  # converted from radians at decode time
      deviation_deg: float | None
      variation_deg: float | None
  ```

- **SQLite is the single source of truth.** Every record gets a UTC timestamp
  and is flushed promptly so a crash/reboot loses at most the last record.
  Web/export/peer code reads from SQLite, never live data. New domains land
  in `storage.py` alongside existing ones (per-domain repo extraction tracked
  in #484).
- **Timestamps are always UTC.** Convert to local only at display/export.
- **External fetches are async.** Use `httpx` async for weather/tide.
- **No business logic in `main.py`** — it only wires modules together.

## Coding conventions

Most style is enforced by `ruff` and `mypy --strict` — don't restate
those rules here. The non-enforceable ones:

- **Modules are small and single-purpose.** Past ~200 lines, consider
  splitting.
- **Full type annotations** on all function signatures — mypy must pass.
- **Hardware-dependent code stays in its hardware module** (see Architecture
  principles).
- **Use `loguru` and structured types** (dataclasses, `TypedDict`) — avoid
  `print()` and raw dicts with unknown shapes.

## Testing

- **Follow TDD:** failing test → implement → green → lint.
- **Tests run without hardware** — mock `audio.py`, `can_reader.py`,
  `sk_reader.py`, `cameras.py`.
- **Use the shared in-memory fixture** — no per-test schema setup:

  ```python
  @pytest_asyncio.fixture
  async def storage() -> Storage:
      s = Storage(StorageConfig(db_path=":memory:"))
      await s.connect()
      yield s
      await s.close()
  ```

- **Web route tests** use `httpx.AsyncClient` with `ASGITransport`.
- **Federation/co-op/peer-API changes require integration tests**
  (`tests/integration/`) — don't skip them because unit tests pass.

## Do / Don't

| Do | Don't |
|---|---|
| Enter a worktree before any file edit. | Edit files in the primary checkout `/Users/dweatbrook/src/helmlog`. |
| Land changes via merged PRs on a feature branch. | Push directly to `main` — even for Pi hotfixes. |
| Include `Closes #N` in every PR body that resolves an issue. | Open a PR without linking the issue; the tracker rots. |
| Follow TDD: failing test → implement → green → lint. | Write code first and bolt tests on later. |
| Flush each decoded record to SQLite as it arrives. | Hold data in memory across long runs — a reboot loses it all. |
| Use `canboat` decoders; add new PGNs as dataclasses in `nmea2000.py`. | Hand-parse NMEA 2000 PGNs from raw bytes unless no library covers them. |
| Read device paths from `.env` / config (`.env.example` is the reference). | Hardcode `/dev/can0` or similar paths. |
| Keep `main.py` as wiring only — start the loop, no business logic. | Mix decode/storage/web logic into `main.py`. |
| Add deps via `uv add <pkg>` then `uv sync`; verify the import. | Edit `pyproject.toml` by hand for deps, or use `uv pip install`. |
| After a pull or branch switch, run `uv sync` (or `./scripts/deploy.sh` on the Pi). | Assume the venv is up to date — the Pi service runs `--no-sync`. |
| Commit + push every change before stopping work. | Leave uncommitted edits on the Pi — the next deploy wipes them. |
| Run `uv run pytest tests/integration/ -v` for federation/co-op/peer-API changes. | Skip integration tests for federation work because unit tests pass. |
| Keep `.db` files and the `data/` directory out of git. | Commit `helmlog.db` or other local data. |

## Risk tiers

Changes to these modules are reviewed against a higher bar regardless of how
small the diff looks. Full detail: [docs/risk-tiers.md](docs/risk-tiers.md).

| Tier | Modules | Extra requirements |
|---|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (migrations), `can_reader.py` | TDD + integration tests + data-licensing review (`docs/data-licensing.md`) + a structured spec before implementation |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py` | Integration tests where applicable |

<important if="touching auth.py / peer_auth.py / federation.py / storage.py migrations / can_reader.py">
**Critical-tier change.** Required: TDD + integration tests + a data-licensing
review (`docs/data-licensing.md`) + a structured spec before implementation. See
[docs/risk-tiers.md](docs/risk-tiers.md).
</important>

<important if="working on the Pi (deployed device, not Mac dev)">
- After `uv add <pkg>` or any dependency change, restart the service:
  `sudo systemctl restart helmlog`. The service runs with `--no-sync` and
  trusts the venv.
- A hotfix on the Pi still goes through a feature branch + PR + merge.
  Never commit to `main` from the device.
- The Pi troubleshooting runbook (`/diagnose` in Claude Code) is the systematic
  path — use it before ad-hoc poking.
</important>

## Where to look next

- **User-facing feature index** (crew / viewer / admin breakdown):
  [docs/features.md](docs/features.md)
- **Risk tiers + spec formats:** [docs/risk-tiers.md](docs/risk-tiers.md)
- **Data licensing policy** (PII, co-op embargoes, biometrics, gambling,
  protest firewall): [docs/data-licensing.md](docs/data-licensing.md).
  (Claude Code: `/data-license` reviews code against it.)
- **Testing layers** (in-process, Pi harness, Docker) and Pi-harness SSH
  setup: [docs/testing-guide.md](docs/testing-guide.md).
  (Claude Code: `/integration-test` picks the right layer.)
- **Promote gate (`main → stage`):** the `promote.yml` workflow requires a
  new `##` heading in `RELEASES.md` for each promoted commit set (exempt
  if all commits only touch `docs/ideation-log.md`). Draft the entry before
  promoting (Claude Code: `/release-notes`). `stage → live` has no gate.
- **Signal K paths, PGN byte layouts, and unit conversions:** `sk_reader.py`
  and `nmea2000.py` are the source of truth. Tribal knowledge not visible in
  those files (B&G quirks, J/105 polar targets, miscalibration symptoms) lives
  in the `/domain` skill (Claude Code).
- **Past decisions on this file** (e.g. why `/domain` isn't split, why
  HelmLog now keeps a real `AGENTS.md`):
  [docs/agent-context-decisions.md](docs/agent-context-decisions.md).
