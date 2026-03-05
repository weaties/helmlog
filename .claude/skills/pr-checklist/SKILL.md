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

## 6. Documentation updates

If the change involved any of these, update accordingly:
- **New module** → update project structure tree in `CLAUDE.md`
- **New env vars** → update `.env.example`
- **New CLI command** → update Common Commands in `CLAUDE.md`
- **New stack tool** → update Stack & Tooling table in `CLAUDE.md`
- **Schema migration** → update schema version in `CLAUDE.md` Stack table

## 7. Commit and push

```bash
git add <files>
git commit -m "feat: description (#issue)"
git push -u origin <branch>
```

## 8. Create PR

```bash
gh pr create --title "..." --body "..."
```

PR must target `main`. Include a summary, test plan, and `Closes #<issue>`.
