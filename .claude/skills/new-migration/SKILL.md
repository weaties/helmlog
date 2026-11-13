---
name: new-migration
description: Add a new SQLite schema migration to storage.py
disable-model-invocation: true
---

# New Schema Migration

The general flow (worktree, TDD, lint) is in AGENTS.md and `/tdd`. The
schema-migration mechanics are visible in `storage.py` — the
`_CURRENT_VERSION` constant and `_MIGRATIONS` dict show every prior
migration as a worked example. `storage.py` is over 12,000 lines: search
for those two names and read the last few migrations rather than the whole
file. This skill captures only the migration-specific gotchas:

- **Bump `_CURRENT_VERSION` in `storage.py` by exactly 1** — never skip
  numbers, never reuse one. Version collisions across concurrent
  agent worktrees are the main reason AGENTS.md mandates worktree
  isolation; check the diff at PR time to confirm no other agent
  shipped the same number while you were branched.

- **Migrations must be idempotent.** Use `CREATE TABLE IF NOT EXISTS`
  and `CREATE INDEX IF NOT EXISTS`. SQLite's `ALTER TABLE` is
  add-column-only — no drops, no renames. Plan accordingly.

- **One migration per logical schema change.** Don't bundle unrelated
  table creations into one version — it makes rollback reasoning and
  the `RELEASES.md` entry harder.

- **Never put a `;` inside a migration `--` comment.**
  `_split_migration_sql()` splits on `;` before it strips comments, so the
  rest of the comment is glued onto the next statement and executed.
  Symptom: `sqlite3.OperationalError: near "<word>": syntax error` right
  after `Applying schema migration vNN`, with whole test files erroring at
  fixture setup. Reword the comment ("host clock, and an unsynced boot",
  not "host clock; an unsynced boot").

- **Use the `storage` fixture** for tests — it runs all migrations
  on an in-memory database, so a TDD red-step will fail with
  "no such table" until the migration lands. That's the intended
  signal.
