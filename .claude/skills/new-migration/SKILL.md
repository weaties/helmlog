---
name: new-migration
description: Add a new SQLite schema migration to storage.py
disable-model-invocation: true
---

# New Schema Migration

## Steps

### 1. Check current version
Read `_CURRENT_VERSION` in `src/logger/storage.py`. Note the current value.

### 2. Write test first (TDD)
Add a test in `tests/test_storage.py` that exercises the new table or column
via the storage methods you'll create. Use the `storage` fixture — it runs
all migrations on an in-memory SQLite database.

```bash
uv run pytest tests/test_storage.py -xvs -k "test_my_new_feature"
```

Confirm it fails (the table/column doesn't exist yet).

### 3. Bump version
Increment `_CURRENT_VERSION` by 1 in `storage.py`.

### 4. Add migration SQL
Add an entry to the `_MIGRATIONS` dict. Follow existing patterns:

```python
_MIGRATIONS: dict[int, str] = {
    # ... existing migrations ...
    NEW_VERSION: """
        -- Description of what this migration does (#issue-number)
        CREATE TABLE IF NOT EXISTS my_table (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ...
        );
        CREATE INDEX IF NOT EXISTS idx_my_table_col ON my_table(col);
    """,
}
```

Common patterns:
- `CREATE TABLE IF NOT EXISTS` — new tables
- `ALTER TABLE x ADD COLUMN y TEXT` — new columns
- `CREATE INDEX IF NOT EXISTS` — indexes for query performance

### 5. Add storage methods
Add async methods to the `Storage` class:

```python
async def add_my_thing(self, ...) -> int:
    db = self._conn()
    cur = await db.execute("INSERT INTO ...", (...))
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid

async def list_my_things(self, ...) -> list[dict[str, Any]]:
    db = self._conn()
    cur = await db.execute("SELECT ... FROM ... WHERE ...", (...))
    rows = await cur.fetchall()
    return [dict(row) for row in rows]
```

### 6. Run test — confirm it passes
```bash
uv run pytest tests/test_storage.py -xvs -k "test_my_new_feature"
uv run pytest  # full suite
```
