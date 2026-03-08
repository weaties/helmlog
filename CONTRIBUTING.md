# Contributing to Helm Log

## Getting Started

```bash
brew install portaudio libsndfile   # macOS deps
uv sync                             # install Python deps
cp .env.example .env                # configure local settings
```

## Development Workflow

1. Branch off `main`: `git checkout -b feature/my-feature main`
2. Follow TDD: write a failing test, implement, pass, lint
3. Run all checks before pushing (see below)
4. Push and create a PR targeting `main`
5. All changes to `main` must come through merged PRs

### Required Checks

```bash
uv run pytest                    # all tests pass
uv run ruff check .              # lint clean
uv run ruff format --check .     # format clean
uv run mypy src/                 # types clean
```

## Data Licensing Policy

Helm Log includes a **data licensing policy** (`docs/data-licensing.md`) that
governs how sailing data is owned, shared, and protected. All contributors
must understand and respect this policy when writing code that handles data.

### Key Principles

- **Boat owners own their data.** Code must never prevent a boat from
  exporting its own data in open formats (CSV, GPX, JSON)
- **PII is protected.** Audio, photos, email addresses, and biometric data
  are personally identifiable information with deletion and anonymization
  rights. Code handling PII must support these rights
- **Co-op data is view-only.** API endpoints serving co-op data from other
  boats must not support bulk export. Rate limiting and audit logging are
  required
- **Gambling prohibition.** No API or feature may facilitate the use of co-op
  data for betting or wagering purposes
- **Protest firewall.** Co-op data from other boats must not be exportable in
  formats designed for submission to protest committees
- **Biometric data is separate.** If adding wearable sensor support, biometric
  data requires per-person consent independent of instrument data sharing
- **Temporal sharing.** Session data may be embargoed per co-op policy. Code
  must respect embargo timestamps before serving track data

### When the Policy Applies

Any code change that touches these areas must be checked against
`docs/data-licensing.md`:

- Data storage, export, or deletion
- API endpoints that serve session or instrument data
- Authentication, access control, or user management
- Audio recording, transcription, or diarization
- Co-op membership, sharing, or governance features
- Any new data type collection (photos, biometrics, etc.)

Use the `/data-license` skill to review changes against the policy.

## Coding Standards

- **Python 3.12+** with full type annotations
- **Ruff** for linting and formatting (line length 100)
- **Loguru** for logging (never `print()`)
- **Dataclasses or TypedDict** for structured data
- Modules stay under ~200 lines; split when they grow
- Hardware-dependent code is isolated (see `CLAUDE.md`)

## Testing

- Tests live in `tests/` and run without hardware
- Use the `storage` fixture from `conftest.py` for in-memory SQLite
- Use `httpx.AsyncClient` with `ASGITransport` for web route tests
- Mock hardware modules (`audio.py`, `can_reader.py`, `sk_reader.py`, `cameras.py`)

## Skills

Claude Code skills are available for common workflows:

| Skill | Purpose |
|---|---|
| `/tdd` | Test-driven development cycle |
| `/new-module` | Scaffold a new hardware-isolated module |
| `/new-migration` | Add a SQLite schema migration |
| `/deploy-pi` | Pi deployment reference |
| `/pr-checklist` | Pre-PR verification checks |
| `/data-license` | Review changes against the data licensing policy |

## License

- **Code**: AGPLv3 (see `LICENSE`)
- **Data**: Governed by the data licensing policy (`docs/data-licensing.md`).
  The code license and data policy are independent — contributing code does
  not grant rights to user data, and using the data co-op does not grant
  rights beyond what AGPLv3 allows
