"""Tests for the post-transcription LLM callback-detection hook (#697)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from helmlog.llm_callback_job import maybe_run_after_transcription, run_for_race
from helmlog.storage import Storage, StorageConfig


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


class FakeClient:
    model: str = "claude-haiku"

    def __init__(
        self,
        callbacks: list[dict[str, Any]] | None = None,
        cost: float = 0.005,
        estimate: float = 0.001,
    ) -> None:
        self.callbacks = callbacks or []
        self.cost = cost
        self._estimate = estimate
        self.calls = 0

    def estimate_input_cost(self, text: str) -> float:
        return self._estimate

    async def detect_callbacks(
        self,
        *,
        transcript_text: str,
        **kwargs: object,
    ) -> tuple[list[dict[str, Any]], float]:
        self.calls += 1
        return self.callbacks, self.cost


async def _race_with_transcript(storage: Storage) -> tuple[int, int]:
    r = await storage.start_race(
        "T",
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "2026-01-01",
        1,
        "race-1",
        "race",
    )
    rid = r.id
    assert rid is not None
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO audio_sessions (start_utc, end_utc, file_path, device_name,"
        " sample_rate, channels, race_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-01-01T12:00:00+00:00",
            "2026-01-01T12:30:00+00:00",
            "/tmp/x.wav",
            "mic",
            16000,
            1,
            rid,
        ),
    )
    aid = cur.lastrowid
    await db.execute(
        "INSERT INTO transcripts (audio_session_id, status, text, model,"
        " created_utc, updated_utc, segments_json) VALUES (?, 'done', ?, 'whisper', ?, ?, ?)",
        (
            aid,
            "tack now ok",
            "2026-01-01T12:30:00+00:00",
            "2026-01-01T12:30:00+00:00",
            json.dumps(
                [
                    {"start": 330.0, "text": "come back to this", "speaker": "helm"},
                ]
            ),
        ),
    )
    await db.commit()
    return rid, aid  # type: ignore[return-value]


class TestRunForRace:
    @pytest.mark.asyncio
    async def test_skips_without_consent(self, storage: Storage) -> None:
        rid, _ = await _race_with_transcript(storage)
        client = FakeClient(
            callbacks=[
                {"anchor_ts": "12:05", "speaker": "helm", "excerpt": "x", "rationale": "r"},
            ]
        )
        result = await run_for_race(storage, rid, client)
        assert result["skipped"] == "consent_required"
        assert client.calls == 0
        assert await storage.list_llm_callbacks(rid) == []

    @pytest.mark.asyncio
    async def test_skips_when_at_cap(self, storage: Storage) -> None:
        rid, _ = await _race_with_transcript(storage)
        await storage.acknowledge_llm_consent(user_id=None)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=None,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=5.50,
        )
        client = FakeClient()
        result = await run_for_race(storage, rid, client)
        assert result["skipped"] in {"hard_cap_reached", "would_exceed_cap"}
        assert client.calls == 0

    @pytest.mark.asyncio
    async def test_persists_on_success(self, storage: Storage) -> None:
        rid, _ = await _race_with_transcript(storage)
        await storage.acknowledge_llm_consent(user_id=None)
        client = FakeClient(
            callbacks=[
                {
                    "anchor_ts": "12:05",
                    "speaker": "helm",
                    "excerpt": "come back to this",
                    "rationale": "explicit revisit",
                },
            ]
        )
        result = await run_for_race(storage, rid, client)
        assert result["count"] == 1
        rows = await storage.list_llm_callbacks(rid)
        assert len(rows) == 1
        assert rows[0]["speaker_label"] == "helm"
        job = await storage.get_callback_job(rid)
        assert job["status"] == "Complete"


class TestMaybeRunAfterTranscription:
    @pytest.mark.asyncio
    async def test_no_op_for_unraced_audio(self, storage: Storage) -> None:
        """An audio session not linked to a race must not trigger the job."""
        db = storage._conn()
        cur = await db.execute(
            "INSERT INTO audio_sessions (start_utc, file_path, device_name,"
            " sample_rate, channels) VALUES (?, ?, ?, ?, ?)",
            ("2026-01-01T12:00:00+00:00", "/tmp/x.wav", "mic", 16000, 1),
        )
        await db.commit()
        client = FakeClient()
        result = await maybe_run_after_transcription(
            storage,
            audio_session_id=int(cur.lastrowid),
            client=client,  # type: ignore[arg-type]
        )
        assert result is None
        assert client.calls == 0

    @pytest.mark.asyncio
    async def test_runs_when_linked_and_consented(self, storage: Storage) -> None:
        rid, aid = await _race_with_transcript(storage)
        await storage.acknowledge_llm_consent(user_id=None)
        client = FakeClient(
            callbacks=[
                {"anchor_ts": "12:05", "speaker": "helm", "excerpt": "x", "rationale": "r"},
            ]
        )
        result = await maybe_run_after_transcription(
            storage,
            audio_session_id=aid,
            client=client,
        )
        assert result is not None
        assert result.get("count") == 1
