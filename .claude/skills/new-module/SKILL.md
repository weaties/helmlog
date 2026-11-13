---
name: new-module
description: Scaffold a new hardware-isolated module with tests and wiring
disable-model-invocation: true
---

# New Module: `$ARGUMENTS`

Follow this checklist to create a new hardware-isolated module. Use TDD (`/tdd`)
for each step that involves code.

## Checklist

### 0. Enter a worktree
Before any file edits, make sure the session is in a git worktree. Check
`git worktree list` — reuse an existing one via `EnterWorktree(path=...)` if
it matches this task, otherwise create a new one via `EnterWorktree(name=...)`.
Scaffolding touches `tests/`, `src/helmlog/`, `main.py`, and often `routes/` and
`storage.py` — exactly the kind of multi-file change that collides with other
agents sharing the repo. See AGENTS.md for the full rule.

### 1. Write tests first
Create `tests/test_$ARGUMENTS.py` with tests for the module's public API.
Use the `storage` fixture from `conftest.py` and mock any external I/O.

### 2. Create the module
Create `src/helmlog/$ARGUMENTS.py`:
- Single-purpose, under ~200 lines
- All external I/O (HTTP, serial, USB, etc.) isolated in this module
- Use dataclasses for structured data
- Use `loguru` for logging (never `print()`)
- Full type annotations on all functions

### 3. Schema migration (if needed)
If the module needs database tables:
- Bump `_CURRENT_VERSION` in `storage.py`
- Add migration SQL to `_MIGRATIONS` dict
- Add async storage methods (CRUD)
- Test with the in-memory `storage` fixture
- See `/new-migration` for the migration gotchas

### 4. Web routes (if needed)
If the module needs API endpoints or admin UI:
- Add a router module under `src/helmlog/routes/` (one file per domain,
  exposing `router`) and add it to the router list in `create_app()` in
  `web.py`. `web.py` is the app factory — route handlers do not live there
- Admin endpoints: `Depends(require_auth("admin"))`, as the existing routers do
- Add the nav link in `templates/base.html` (the `nav-links` block)
- Add web route tests with the `AsyncClient` + `ASGITransport` pattern (`/tdd`)

### 5. Wire into main.py
- Import the module **only in `main.py`** (hardware isolation principle)
- Parse config from env vars
- Pass to `_web_loop()` / `create_app()` if needed
- Add CLI subcommand if applicable

### 6. Sync dependencies
If the module requires new packages:
- Add with `uv add <package>` (never edit pyproject.toml manually for deps)
- Run `uv sync` and verify the import works
- Never use `uv pip install` — it bypasses the lockfile and won't persist

### 7. Update config
- Add new env vars to `.env.example` with comments
- Add the module to the `AGENTS.md` project structure tree if it is a key module

### 8. Run `/pr-checklist`
