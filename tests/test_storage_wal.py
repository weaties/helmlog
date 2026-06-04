"""Tests for WAL mode and read/write connection split in storage.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from helmlog.storage import Storage, StorageConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest_asyncio.fixture
async def file_storage(tmp_path: Path) -> Storage:  # type: ignore[misc]
    """Storage backed by a real file (not :memory:) so WAL and read conn work."""
    db_path = str(tmp_path / "test.db")
    s = Storage(StorageConfig(db_path=db_path))
    await s.connect()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_path: Path) -> None:
    """WAL journal mode should be set on file-backed databases."""
    db_path = str(tmp_path / "wal.db")
    s = Storage(StorageConfig(db_path=db_path))
    await s.connect()
    try:
        cur = await s._conn().execute("PRAGMA journal_mode")
        row = await cur.fetchone()
        assert row[0] == "wal"
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_busy_timeout_set_on_write_connection(tmp_path: Path) -> None:
    """A non-zero busy_timeout must be set so contending writes wait instead
    of erroring instantly with 'database is locked'."""
    db_path = str(tmp_path / "busy.db")
    s = Storage(StorageConfig(db_path=db_path))
    await s.connect()
    try:
        cur = await s._conn().execute("PRAGMA busy_timeout")
        row = await cur.fetchone()
        assert row[0] >= 5000
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_busy_timeout_set_on_read_connection(tmp_path: Path) -> None:
    """The read connection should also carry a busy_timeout."""
    db_path = str(tmp_path / "busy_ro.db")
    s = Storage(StorageConfig(db_path=db_path))
    await s.connect()
    try:
        assert s._read_db is not None
        cur = await s._read_db.execute("PRAGMA busy_timeout")
        row = await cur.fetchone()
        assert row[0] >= 5000
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_read_connection_exists_for_file_db(tmp_path: Path) -> None:
    """File-backed storage should have a separate read connection."""
    db_path = str(tmp_path / "read.db")
    s = Storage(StorageConfig(db_path=db_path))
    await s.connect()
    try:
        assert s._read_db is not None
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_memory_db_has_no_read_connection() -> None:
    """:memory: databases should fall back to the write connection for reads."""
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    try:
        assert s._read_db is None
        # _read_conn() should fall back to write connection
        assert s._read_conn() is s._conn()
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_read_connection_serves_queries(file_storage: Storage) -> None:
    """Read connection should return data written via the write connection."""
    await file_storage.set_setting("test_key", "test_value")

    # Read via the read connection (get_setting uses _read_conn)
    result = await file_storage.get_setting("test_key")
    assert result == "test_value"


@pytest.mark.asyncio
async def test_close_handles_both_connections(tmp_path: Path) -> None:
    """close() should cleanly close both read and write connections."""
    db_path = str(tmp_path / "close.db")
    s = Storage(StorageConfig(db_path=db_path))
    await s.connect()
    assert s._db is not None
    assert s._read_db is not None
    await s.close()
    assert s._db is None
    assert s._read_db is None
