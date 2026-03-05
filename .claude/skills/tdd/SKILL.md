---
name: tdd
description: Use test-driven development when implementing new features or fixing bugs
---

# Test-Driven Development (Red-Green-Refactor)

Always follow this cycle when implementing new functionality or fixing bugs.

## 1. Red — Write a Failing Test

Write the test **before** writing the implementation.

- Test file: `tests/test_<module>.py`
- Use the `storage` fixture from `conftest.py` for in-memory SQLite
- Use `httpx.AsyncClient` with `ASGITransport` for web route tests
- Mock hardware modules (`audio.py`, `can_reader.py`, `sk_reader.py`, `cameras.py`)
- All tests are `@pytest.mark.asyncio` for async code

```bash
uv run pytest tests/test_<module>.py -xvs
```

Confirm the test **fails** with the expected error (not an import error or fixture issue).

## 2. Green — Write Minimal Code to Pass

Implement just enough to make the failing test pass. No more.

```bash
uv run pytest tests/test_<module>.py -xvs
```

Confirm the test **passes**.

## 3. Refactor — Improve Without Breaking

Clean up the implementation. Then run the **full** test suite:

```bash
uv run pytest
```

All tests must pass.

## 4. Lint and Type-Check

```bash
uv run ruff check . && uv run ruff format . && uv run mypy src/
```

**Pre-existing errors to ignore** (do not fix unless explicitly asked):
- `web.py`: `Item "None" of "datetime | None" has no attribute "isoformat"`
- `web.py`: `Item "None" of "AudioRecorder | None" has no attribute "stop"` (x2)
- `main.py`: `Unused "type: ignore" comment`
- `storage.py`: 2 pre-existing E501 line-length violations

## 5. Commit

Commit the test and implementation together. Use conventional commit format.

## Testing Patterns

**Storage tests** — use the `storage` fixture (in-memory SQLite, fully migrated):
```python
@pytest.mark.asyncio
async def test_my_feature(storage: object) -> None:
    from logger.storage import Storage
    assert isinstance(storage, Storage)
    # storage is ready with all migrations applied
```

**Web route tests** — use `httpx.AsyncClient`:
```python
@pytest.mark.asyncio
async def test_my_endpoint(storage: object) -> None:
    from logger.storage import Storage
    assert isinstance(storage, Storage)
    from logger.web import create_app
    app = create_app(storage)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/my-endpoint")
        assert resp.status_code == 200
```

**Hardware mocking** — patch at the module level:
```python
with patch("logger.cameras.httpx.AsyncClient") as mock_client:
    # test camera operations without real hardware
```
