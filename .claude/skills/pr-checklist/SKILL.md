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

## 2. Determine risk tier

Check which files were changed and resolve the PR's risk tier (highest tier
of any changed file). See the Risk Tiers table in CLAUDE.md.

```bash
git diff --name-only main...HEAD
```

**Tier resolution rules:**

| Tier | Files matching |
|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (if migrations changed), `can_reader.py` |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py`, `boat_settings.py` |
| **Standard** | `web.py`, `polar.py`, `external.py`, `races.py`, `triggers.py`, `maneuver_detector.py`, `race_classifier.py`, `courses.py`, other `.py` files |
| **Low** | Templates (`*.html`), CSS, JS, docs (`*.md`), config, scripts |

The PR's tier is the **highest** tier touched. Report the resolved tier
before proceeding — e.g., "This PR touches `auth.py` → **Critical** tier."

## 3. Run tests

```bash
uv run pytest
```

All tests must pass. If new functionality was added, there must be corresponding tests.

**Skip for Low tier:** Tests are optional if the PR only touches templates,
CSS, JS, docs, or config — but run them if available.

## 4. Lint check

```bash
uv run ruff check .
```

Must be clean. Pre-existing exceptions:
- 2 E501 line-length violations in `storage.py` (pre-existing, do not fix)

## 5. Format check

```bash
uv run ruff format --check .
```

If it fails, run `uv run ruff format .` to auto-fix.

## 6. Type check

```bash
uv run mypy src/
```

Must be clean. Pre-existing exceptions (do not fix unless asked):
- `web.py`: `Item "None" of "datetime | None" has no attribute "isoformat"`
- `web.py`: `Item "None" of "AudioRecorder | None" has no attribute "stop"` (x2)
- `main.py`: `Unused "type: ignore" comment`

**Skip for Low tier:** mypy is optional if the PR only touches templates,
CSS, JS, docs, or config.

## 7. Integration tests (High and Critical tiers)

**Required for Critical tier.** Required for High tier if touching federation,
co-op, peer API, or data licensing code.

```bash
uv run pytest tests/integration/ -v
```

All integration tests must pass. These validate inter-boat auth, embargo,
data licensing, and audit logging with real Ed25519 crypto.

For major federation changes (Critical tier), also run Pi smoke tests before merge:
```bash
ssh weaties@corvopi-tst1 "cd ~/helmlog && uv run python scripts/integration_smoke.py --peer corvopi-live"
```

## 8. Data licensing compliance (Critical and High tiers)

**Required for Critical tier.** Required for High tier if touching data
storage, export, deletion, API endpoints, PII handling, or co-op features.

Run `/data-license` to verify compliance with `docs/data-licensing.md`.

Key items to check:
- Own-boat data remains exportable in open formats (CSV, GPX, JSON, WAV)
- PII (audio, photos, emails, biometrics) supports deletion/anonymization
- Co-op endpoints do not allow bulk export of other boats' data
- Temporal sharing embargo timestamps are respected
- No gambling/betting facilitation
- Audit logging on co-op data access

## 9. Spec review (Critical tier)

**Required for Critical tier.** Verify that a structured spec (decision table,
state diagram, or EARS requirements) was reviewed and approved before
implementation began. The spec should be posted as a comment on the GitHub
issue.

If no spec exists for a Critical-tier change, flag this to the user before
proceeding.

## 10. Dependency check

If any dependencies were added (via `uv add`), verify:
- The dependency is in `pyproject.toml` and `uv.lock`
- `uv sync` installs it cleanly (the import works)
- **Never** use `uv pip install` for project dependencies — it bypasses the lockfile and won't persist. The helmlog service runs with `--no-sync`, so the venv must be correct at deploy time.

## 11. Documentation updates

If the change involved any of these, update accordingly:
- **New module** → update project structure tree in `CLAUDE.md`
- **New env vars** → update `.env.example`
- **New CLI command** → update Common Commands in `CLAUDE.md`
- **New stack tool** → update Stack & Tooling table in `CLAUDE.md`
- **Schema migration** → update schema version in `CLAUDE.md` Stack table
- **Data handling changes** → verify against `docs/data-licensing.md`
- **New dependency** → verify it's in `pyproject.toml` and installs via `uv sync`
- **New module with risk implications** → add to Risk Tiers table in `CLAUDE.md`

## 12. Complexity check

Flag if any touched `.py` file exceeds 200 lines (the module size convention).
If so, consider whether it should be split before merging.

```bash
wc -l $(git diff --name-only main...HEAD | grep '\.py$')
```

## 13. Commit and push

```bash
git add <files>
git commit -m "feat: description (#issue)"
git push -u origin <branch>
```

## 14. Create PR

```bash
gh pr create --title "..." --body "..."
```

PR must target `main`. Include a summary, test plan, and `Closes #<issue>`.

## Quick Reference — Tier Checklist Summary

| Check | Critical | High | Standard | Low |
|---|---|---|---|---|
| Tests | Required | Required | Required | Optional |
| Lint + format | Required | Required | Required | Required |
| mypy | Required | Required | Required | Optional |
| Integration tests | Required | If federation | No | No |
| `/data-license` | Required | If data/PII | No | No |
| Spec review | Required | No | No | No |
| Complexity check | Required | Required | Required | No |
