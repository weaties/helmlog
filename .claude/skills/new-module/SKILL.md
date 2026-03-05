---
name: new-module
description: Scaffold a new hardware-isolated module with tests and wiring
disable-model-invocation: true
---

# New Module: `$ARGUMENTS`

Follow this checklist to create a new hardware-isolated module. Use TDD (`/tdd`)
for each step that involves code.

## Checklist

### 1. Write tests first
Create `tests/test_$ARGUMENTS.py` with tests for the module's public API.
Use the `storage` fixture from `conftest.py` and mock any external I/O.

### 2. Create the module
Create `src/logger/$ARGUMENTS.py`:
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

### 4. Web routes (if needed)
If the module needs API endpoints or admin UI:
- Add to `web.py` following existing patterns
- Admin pages: require `require_auth("admin")`
- Add nav link in `_nav_html()`
- Add web route tests in `test_web.py`

### 5. Wire into main.py
- Import the module **only in `main.py`** (hardware isolation principle)
- Parse config from env vars
- Pass to `_web_loop()` / `create_app()` if needed
- Add CLI subcommand if applicable

### 6. Update config
- Add new env vars to `.env.example` with comments
- Update `CLAUDE.md` project structure tree

### 7. Run `/pr-checklist`
