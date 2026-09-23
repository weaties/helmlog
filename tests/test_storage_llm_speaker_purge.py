"""Tests for Storage.purge_speaker_llm_artefacts (#697 follow-up from #699).

Gap closed: when a crew member requests voice-data deletion, callbacks
attributed to that speaker are deleted, and qa citation rows are flagged
(scrubbing is a schema follow-up — citations currently carry only {ts},
not a speaker field).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from helmlog.storage import Storage, StorageConfig


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


async def _race(storage: Storage, *, n: int = 1) -> int:
    race = await storage.start_race(
        f"T{n}",
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "2026-01-01",
        n,
        f"race-{n}",
        "race",
    )
    assert race.id is not None
    return race.id


async def _user(storage: Storage, email: str = "ada@example.com", role: str = "crew") -> int:
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO users (email, role, created_at) VALUES (?, ?, ?)",
        (email, role, "2026-01-01T12:00:00+00:00"),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def _insert_callback(
    storage: Storage,
    race_id: int,
    speaker_label: str,
    anchor_ts: str = "00:01:23",
) -> int:
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO llm_callbacks (race_id, speaker_label, anchor_ts, source_excerpt, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (race_id, speaker_label, anchor_ts, "test excerpt", "2026-01-01T12:00:00+00:00"),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def _insert_qa(
    storage: Storage,
    race_id: int,
    *,
    user_id: int | None = None,
    citations: list[dict] | None = None,
) -> int:
    return await storage.insert_llm_qa(
        race_id=race_id,
        user_id=user_id,
        question="What happened at the start?",
        answer="The boat was close-hauled. [00:01:23]",
        citations=citations or [{"ts": "00:01:23"}],
        model="claude-test",
        input_tokens=10,
        output_tokens=10,
        cache_read_tokens=0,
        cache_create_tokens=0,
        cost_usd=0.0,
    )


class TestPurgeSpeakerLLMArtefacts:
    @pytest.mark.asyncio
    async def test_deletes_callbacks_for_speaker(self, storage: Storage) -> None:
        rid = await _race(storage)
        await _insert_callback(storage, rid, "SPEAKER_01", "00:01:00")
        await _insert_callback(storage, rid, "SPEAKER_01", "00:02:00")
        await _insert_callback(storage, rid, "SPEAKER_02", "00:03:00")

        result = await storage.purge_speaker_llm_artefacts("SPEAKER_01")

        assert result["callbacks_deleted"] == 2

    @pytest.mark.asyncio
    async def test_leaves_other_speakers_callbacks_intact(self, storage: Storage) -> None:
        rid = await _race(storage)
        await _insert_callback(storage, rid, "SPEAKER_01", "00:01:00")
        await _insert_callback(storage, rid, "SPEAKER_02", "00:02:00")

        await storage.purge_speaker_llm_artefacts("SPEAKER_01")

        db = storage._conn()
        cur = await db.execute(
            "SELECT COUNT(*) FROM llm_callbacks WHERE speaker_label = 'SPEAKER_02'"
        )
        (count,) = await cur.fetchone()
        assert count == 1

    @pytest.mark.asyncio
    async def test_deletes_callbacks_across_multiple_races(self, storage: Storage) -> None:
        rid1 = await _race(storage, n=1)
        rid2 = await _race(storage, n=2)
        await _insert_callback(storage, rid1, "SPEAKER_01", "00:01:00")
        await _insert_callback(storage, rid2, "SPEAKER_01", "00:02:00")

        result = await storage.purge_speaker_llm_artefacts("SPEAKER_01")

        assert result["callbacks_deleted"] == 2

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_callbacks(self, storage: Storage) -> None:
        result = await storage.purge_speaker_llm_artefacts("SPEAKER_UNKNOWN")

        assert result["callbacks_deleted"] == 0

    @pytest.mark.asyncio
    async def test_qa_scrubbed_is_zero_citations_lack_speaker_field(self, storage: Storage) -> None:
        """citations_json carries only {ts}, not speaker.

        Until the schema is extended (see TODO in purge_speaker_llm_artefacts),
        the scrub count is always 0 — no guessing which rows belong to the
        speaker.
        """
        rid = await _race(storage)
        await _insert_qa(storage, rid, citations=[{"ts": "00:01:23"}])

        result = await storage.purge_speaker_llm_artefacts("SPEAKER_01")

        assert result["qa_scrubbed"] == 0

    @pytest.mark.asyncio
    async def test_returns_dict_with_expected_keys(self, storage: Storage) -> None:
        result = await storage.purge_speaker_llm_artefacts("SPEAKER_01")

        assert set(result.keys()) == {"callbacks_deleted", "qa_scrubbed"}

    @pytest.mark.asyncio
    async def test_operation_is_atomic(self, storage: Storage) -> None:
        """Both deletes happen in one transaction — observable as a single commit."""
        rid = await _race(storage)
        await _insert_callback(storage, rid, "SPEAKER_01")
        await _insert_qa(storage, rid)

        result = await storage.purge_speaker_llm_artefacts("SPEAKER_01")

        assert result["callbacks_deleted"] == 1
        assert result["qa_scrubbed"] == 0

    @pytest.mark.asyncio
    async def test_null_speaker_label_in_callbacks_not_affected(self, storage: Storage) -> None:
        """NULL speaker_label rows must not match a label string."""
        rid = await _race(storage)
        db = storage._conn()
        await db.execute(
            "INSERT INTO llm_callbacks"
            " (race_id, speaker_label, anchor_ts, source_excerpt, created_at)"
            " VALUES (?, NULL, ?, ?, ?)",
            (rid, "00:01:00", "excerpt", "2026-01-01T12:00:00+00:00"),
        )
        await db.commit()

        result = await storage.purge_speaker_llm_artefacts("SPEAKER_01")

        assert result["callbacks_deleted"] == 0
        db2 = storage._conn()
        cur = await db2.execute("SELECT COUNT(*) FROM llm_callbacks WHERE speaker_label IS NULL")
        (count,) = await cur.fetchone()
        assert count == 1
