---
name: pr-checklist
description: Run pre-PR verification checks before creating a pull request
---

# Pre-PR Checklist

Run these checks before creating or pushing to a pull request.

## 0. Mark the issue in-progress

If working on a GitHub issue, comment on it with the branch name and agent identity:

```bash
gh issue comment <number> --body "In progress on \`<branch-name>\` (Claude Code on $(hostname))"
```

## 1. Confirm feature branch

You must be on a feature branch, **not `main`**. All changes to `main` come
through merged PRs.

```bash
git branch --show-current
# Must NOT be "main"
```

## 2. Run tests

```bash
uv run pytest
```

All tests must pass. If new functionality was added, there must be corresponding tests.

## 3. Lint check

```bash
uv run ruff check .
```

Must be clean. Pre-existing exceptions:
- 2 E501 line-length violations in `storage.py` (pre-existing, do not fix)

## 4. Format check

```bash
uv run ruff format --check .
```

If it fails, run `uv run ruff format .` to auto-fix.

## 5. Type check

```bash
uv run mypy src/
```

Must be clean. Pre-existing exceptions (do not fix unless asked):
- `web.py`: `Item "None" of "datetime | None" has no attribute "isoformat"`
- `web.py`: `Item "None" of "AudioRecorder | None" has no attribute "stop"` (x2)
- `main.py`: `Unused "type: ignore" comment`

## 6. Integration tests (federation PRs)

If the change touches federation, co-op, peer API, or data licensing code,
run the integration tests:

```bash
uv run pytest tests/integration/ -v
```

All 32 integration tests must pass. These validate inter-boat auth, embargo,
data licensing, and audit logging with real Ed25519 crypto.

For major federation changes, also run Pi smoke tests before merge:
```bash
ssh weaties@corvopi-tst1 "cd ~/helmlog && uv run python scripts/integration_smoke.py --peer corvopi-live"
```

## 7. Data licensing compliance

If the change touches data storage, export, deletion, API endpoints, PII
handling, co-op features, or any new data type collection, run `/data-license`
to verify compliance with `docs/data-licensing.md`.

Key items to check:
- Own-boat data remains exportable in open formats (CSV, GPX, JSON, WAV)
- PII (audio, photos, emails, biometrics) supports deletion/anonymization
- Co-op endpoints do not allow bulk export of other boats' data
- Temporal sharing embargo timestamps are respected
- No gambling/betting facilitation
- Audit logging on co-op data access

## 8. Dependency check

If any dependencies were added (via `uv add`), verify:
- The dependency is in `pyproject.toml` and `uv.lock`
- `uv sync` installs it cleanly (the import works)
- **Never** use `uv pip install` for project dependencies — it bypasses the lockfile and won't persist. The helmlog service runs with `--no-sync`, so the venv must be correct at deploy time.

## 9. Documentation updates

If the change involved any of these, update accordingly:
- **New module** → update project structure tree in `CLAUDE.md`
- **New env vars** → update `.env.example`
- **New CLI command** → update Common Commands in `CLAUDE.md`
- **New stack tool** → update Stack & Tooling table in `CLAUDE.md`
- **Schema migration** → update schema version in `CLAUDE.md` Stack table
- **Data handling changes** → verify against `docs/data-licensing.md`
- **New dependency** → verify it's in `pyproject.toml` and installs via `uv sync`

## 10. Commit and push

```bash
git add <files>
git commit -m "feat: description (#issue)"
git push -u origin <branch>
```

## 11. Create PR

```bash
gh pr create --title "..." --body "..."
```

PR must target `main`. Include a summary, test plan, and `Closes #<issue>`.
