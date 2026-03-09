# HelmLog — Testing Guide

> How the test suite is organized, how to write new tests, and conventions
> to follow.

_Last reviewed: 2026-03-08 · 619 tests across 24 test files._

---

## Running tests

```bash
# Full suite with coverage
uv run pytest

# Single file
uv run pytest tests/test_federation.py

# Single test class or method
uv run pytest tests/test_federation.py::TestSigning
uv run pytest tests/test_federation.py::TestSigning::test_sign_and_verify

# Verbose output
uv run pytest -v

# Stop on first failure
uv run pytest -x

# Run only tests matching a keyword
uv run pytest -k "co_op"
```

Always run the full suite before pushing:

```bash
uv run pytest && uv run ruff check . && uv run mypy src/
```

---

## Project configuration

Test configuration lives in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"                               # no need for @pytest.mark.asyncio on every test
addopts = "--cov=src/helmlog --cov-report=term-missing"
env = ["AUTH_DISABLED=true"]                         # auth middleware is bypassed in tests
```

Key settings:
- **`asyncio_mode = "auto"`** — async test functions are detected automatically.
  You still need `@pytest.mark.asyncio` on async tests inside classes (pytest
  limitation), but standalone async test functions work without it.
- **Coverage** is always printed. Check `term-missing` output for uncovered lines.
- **`AUTH_DISABLED=true`** — the web API auth middleware is off, so API tests
  don't need to mock authentication.

---

## Test file layout

Each source module has a corresponding test file:

| Module | Test file |
|--------|-----------|
| `storage.py` | `test_storage.py` |
| `federation.py` | `test_federation.py`, `test_federation_storage.py` |
| `web.py` | `test_web.py` |
| `export.py` | `test_export.py` |
| `sk_reader.py` | `test_sk_reader.py` |
| `nmea2000.py` | `test_nmea2000.py` |
| ... | ... |

Federation has two test files because the module has two distinct concerns:
pure crypto/identity logic (`test_federation.py`) and SQLite storage
integration (`test_federation_storage.py`).

---

## Shared fixtures — `conftest.py`

The project-wide `conftest.py` provides:

- **`storage`** — an in-memory SQLite `Storage` instance with all migrations
  applied. This is the workhorse fixture for any test that touches the database.
- **`sample_can_frames`** — one `CANFrame` per supported PGN type (heading,
  speed, depth, position, COG/SOG, wind, environmental).
- **`sample_records`** — decoded `PGNRecord` dataclasses with known values.

### Writing your own fixtures

Module-specific fixtures go in the test file, not in `conftest.py`. Only
truly shared fixtures belong in conftest.

```python
# In test_federation.py — a fixture only used by federation tests
@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    return generate_keypair()
```

For async fixtures that need cleanup, use `yield`:

```python
@pytest.fixture
async def storage(tmp_path: object) -> Storage:
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()
```

---

## Conventions

### Test organization

- Group related tests into classes: `TestBoatIdentity`, `TestSigning`, etc.
- Class names start with `Test`. Method names start with `test_`.
- Order tests from simple to complex: happy path first, then edge cases,
  then error cases.

### Async tests

Most tests touching storage or the web API are async:

```python
class TestBoatIdentity:
    @pytest.mark.asyncio
    async def test_save_and_get(self, storage: Storage) -> None:
        await storage.save_boat_identity(
            pub_key="abc123", fingerprint="fp123",
            sail_number="69", boat_name="Javelina",
        )
        identity = await storage.get_boat_identity()
        assert identity is not None
        assert identity["pub_key"] == "abc123"
```

The `@pytest.mark.asyncio` decorator is required for async methods inside
classes. Standalone async functions don't need it (thanks to `asyncio_mode = "auto"`).

### Pure-logic tests

Tests that don't touch I/O can be synchronous:

```python
class TestDataclasses:
    def test_charter_to_dict(self) -> None:
        charter = Charter(
            co_op_id="abc123", name="Test Co-op", areas=["Bay"],
            admin_boat_pub="pubkey", admin_boat_fingerprint="fp",
            created_at="2026-01-01T00:00:00Z",
        )
        d = charter.to_dict()
        assert d["type"] == "charter"
```

### Database fixtures

When a test needs rows in tables beyond what the storage fixture provides
(e.g. a race session), create them directly:

```python
@pytest.fixture
async def session_id(self, storage: Storage) -> int:
    """Create a race session and return its ID."""
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("Test Race", "CYC Wednesday", 1, "2026-03-08",
         "2026-03-08T12:00:00Z", "race"),
    )
    await db.commit()
    return cur.lastrowid or 0
```

**Important:** Match the table's NOT NULL constraints. Check the schema
migrations in `storage.py` if you're unsure which columns are required.

### Web API tests

The web test suite uses `httpx.AsyncClient` with `ASGITransport`:

```python
from httpx import ASGITransport, AsyncClient
from helmlog.web import create_app

async def test_api_endpoint(storage: Storage) -> None:
    app = create_app(storage)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
```

### Assertions

- Use plain `assert` statements. No need for `assertEqual` or similar.
- Assert specific values, not just truthiness: `assert x == 42` not `assert x`.
- For dict-like results from storage, access by key: `assert row["name"] == "Fleet"`.
- For checking that something raises, use `pytest.raises`:

```python
with pytest.raises(FileExistsError):
    init_identity(identity_dir, sail_number="69", boat_name="Test")
```

### Type annotations

All test functions should have return type annotations (`-> None`).
Fixture return types should be annotated. Use `TYPE_CHECKING` blocks for
imports only needed in annotations:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
```

---

## What to test

### For new storage methods

1. **Happy path** — save, then retrieve and verify all fields.
2. **Upsert behavior** — save twice with different data, verify the update.
3. **Empty/missing** — query when nothing exists, verify `None` or `[]`.
4. **Constraints** — if there's a foreign key or unique constraint, test
   that it's enforced.

### For new federation logic

1. **Round-trip** — sign something, verify it.
2. **Tamper detection** — sign, modify data, verify fails.
3. **Wrong key** — sign with one key, verify with another, expect failure.
4. **Serialization** — convert to JSON and back, verify all fields survive.

### For new API endpoints

1. **200 response** — happy path returns expected JSON shape.
2. **404/400** — missing or invalid parameters return proper error codes.
3. **Side effects** — POST endpoints actually persist data (query after to verify).

### For new CLI commands

CLI handlers are tested indirectly through their underlying functions. The
handler functions (`_identity_init`, `_co_op_create`, etc.) are thin wrappers
that call federation and storage modules — test those modules directly.

---

## Linting and type checking

Tests must pass the same lint and type checks as source code:

```bash
uv run ruff check tests/
uv run mypy src/          # mypy only checks src/, not tests/
```

Ruff rules applied to tests:
- Line length: 100 characters
- Imports sorted with isort
- Type-checking-only imports in `TYPE_CHECKING` blocks (TCH003)
- No unused imports (F401)

---

## Coverage

Coverage is printed after every test run. The project doesn't enforce a
minimum coverage threshold, but new code should have tests for all
non-trivial paths.

Current coverage is ~69% overall. Hardware-dependent modules (audio,
cameras, monitor) have low coverage because they require physical devices.
Pure-logic modules (federation, storage, export) should aim for 90%+.

Federation module coverage: **97%** (6 uncovered lines out of 220).

---

## Common pitfalls

**Missing `@pytest.mark.asyncio` on class methods** — async methods inside
test classes need the decorator. Without it, the test silently passes
(the coroutine is created but never awaited).

**Wrong column names in fixture inserts** — the `races` table schema has
evolved through many migrations. Always check the actual column names in
`storage.py` migrations rather than guessing.

**Forgetting `await` in async tests** — storage methods are all async.
If you forget `await`, you'll get a coroutine object instead of the result,
and assertions will pass on truthiness (`assert <coroutine>` is truthy).

**Fixture scope** — all fixtures default to function scope. If you need
a shared expensive fixture (rare), use `scope="session"` or `scope="module"`,
but be careful about test isolation.
