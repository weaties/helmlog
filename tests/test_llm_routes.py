"""HTTP route tests for /api/llm and /api/sessions/{id}/llm/* (#697).

Tests cover the spec decision-table rows that are reachable through the
HTTP layer: consent gate, role enforcement, cost-cap blocking, save-as-
moment flow, and per-race scope. The LLM client is replaced with a fake
that returns canned responses — no real API calls.

AUTH_DISABLED=true is set in pyproject.toml so the default user is admin.
Role-restricted rows flip AUTH_DISABLED off and authenticate via session
cookie helpers.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

from helmlog.llm_client import LLMResponse
from helmlog.storage import Storage, StorageConfig
from helmlog.web import create_app


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


class FakeLLMClient:
    model: str = "claude-sonnet-4-6"

    def __init__(
        self,
        ask_response: LLMResponse | None = None,
        callbacks: list[dict[str, Any]] | None = None,
        callback_cost: float = 0.001,
        estimate: float = 0.001,
    ) -> None:
        self.ask_response = ask_response or LLMResponse(
            text="We tacked at [12:05].",
            citations=[{"ts": "12:05"}],
            input_tokens=200,
            output_tokens=20,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.001,
        )
        self.callbacks = callbacks or []
        self.callback_cost = callback_cost
        self._estimate = estimate
        self.ask_calls: list[dict[str, Any]] = []

    def estimate_input_cost(self, text: str) -> float:
        return self._estimate

    async def ask(self, *, transcript_text: str, question: str, **kwargs: object) -> LLMResponse:
        self.ask_calls.append({"transcript_text": transcript_text, "question": question})
        return self.ask_response

    async def detect_callbacks(
        self,
        *,
        transcript_text: str,
        **kwargs: object,
    ) -> tuple[list[dict[str, Any]], float]:
        return self.callbacks, self.callback_cost


async def _race(storage: Storage, *, n: int = 1) -> int:
    r = await storage.start_race(
        f"T{n}",
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "2026-01-01",
        n,
        f"race-{n}",
        "race",
    )
    assert r.id is not None
    return r.id


async def _seed_transcript(storage: Storage, race_id: int) -> None:
    """Attach a finished transcript to the race so the prompt builder finds text."""
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO audio_sessions (start_utc, end_utc, file_path,"
        " device_name, sample_rate, channels, race_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-01-01T12:00:00+00:00",
            "2026-01-01T12:30:00+00:00",
            "/tmp/x.wav",
            "test-mic",
            16000,
            1,
            race_id,
        ),
    )
    audio_id = cur.lastrowid
    import json as _json

    segs = [
        {"start": 330.0, "text": "tack now", "speaker": "helm"},
        {"start": 360.0, "text": "ok", "speaker": "trim"},
    ]
    await db.execute(
        "INSERT INTO transcripts (audio_session_id, status, text, model,"
        " created_utc, updated_utc, segments_json) VALUES (?, 'done', ?, 'whisper', ?, ?, ?)",
        (
            audio_id,
            "tack now ok",
            "2026-01-01T12:30:00+00:00",
            "2026-01-01T12:30:00+00:00",
            _json.dumps(segs),
        ),
    )
    await db.commit()


def _client(storage: Storage, llm: FakeLLMClient | None = None) -> httpx.AsyncClient:
    app = create_app(storage)
    if llm is not None:
        app.state.llm_client = llm
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestConsent:
    @pytest.mark.asyncio
    async def test_get_unacknowledged(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.get("/api/llm/consent")
            assert resp.status_code == 200
            assert resp.json()["acknowledged"] is False

    @pytest.mark.asyncio
    async def test_admin_can_acknowledge(self, storage: Storage) -> None:
        async with _client(storage) as c:
            resp = await c.post("/api/llm/consent")
            assert resp.status_code == 200
            assert resp.json()["acknowledged"] is True
            # by_user can be None when AUTH_DISABLED returns the mock admin (id=None)


class TestQABlockedWithoutConsent:
    @pytest.mark.asyncio
    async def test_post_question_409_without_consent(self, storage: Storage) -> None:
        rid = await _race(storage)
        await _seed_transcript(storage, rid)
        async with _client(storage, FakeLLMClient()) as c:
            resp = await c.post(
                f"/api/sessions/{rid}/llm/qa",
                json={"question": "what happened?"},
            )
            assert resp.status_code == 409
            assert resp.json()["reason"] == "consent_required"

    @pytest.mark.asyncio
    async def test_history_visible_without_consent(self, storage: Storage) -> None:
        """History GET is allowed even pre-consent — viewers can read prior
        Q&A from the time consent was active. Confirms decision-table row
        'viewer | any | Read prior Q&A history | Yes'."""
        rid = await _race(storage)
        async with _client(storage) as c:
            resp = await c.get(f"/api/sessions/{rid}/llm/qa")
            assert resp.status_code == 200
            assert resp.json()["qa"] == []


class TestQAAskFlow:
    @pytest.mark.asyncio
    async def test_ask_persists_and_returns_citations(self, storage: Storage) -> None:
        rid = await _race(storage)
        await _seed_transcript(storage, rid)
        fake = FakeLLMClient()
        async with _client(storage, fake) as c:
            await c.post("/api/llm/consent")
            resp = await c.post(
                f"/api/sessions/{rid}/llm/qa",
                json={"question": "When did we tack?"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["answer"] == "We tacked at [12:05]."
            assert data["citations"] == [{"ts": "12:05"}]
            assert data["cost_usd"] == pytest.approx(0.001)

            # Persisted
            history = (await c.get(f"/api/sessions/{rid}/llm/qa")).json()
            assert len(history["qa"]) == 1
            assert history["qa"][0]["question"] == "When did we tack?"

        # Transcript was actually included
        assert "tack now" in fake.ask_calls[0]["transcript_text"]

    @pytest.mark.asyncio
    async def test_ask_with_no_transcript_404(self, storage: Storage) -> None:
        rid = await _race(storage)  # no transcript seeded
        async with _client(storage, FakeLLMClient()) as c:
            await c.post("/api/llm/consent")
            resp = await c.post(
                f"/api/sessions/{rid}/llm/qa",
                json={"question": "anything?"},
            )
            assert resp.status_code == 404


class TestQACostCap:
    @pytest.mark.asyncio
    async def test_at_cap_blocks_with_429(self, storage: Storage) -> None:
        rid = await _race(storage)
        await _seed_transcript(storage, rid)
        # Pre-fill spend at the hard cap.
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
        async with _client(storage, FakeLLMClient()) as c:
            resp = await c.post(
                f"/api/sessions/{rid}/llm/qa",
                json={"question": "another?"},
            )
            assert resp.status_code == 429
            assert resp.json()["reason"] in {"hard_cap_reached", "would_exceed_cap"}


class TestSaveAsMoment:
    @pytest.mark.asyncio
    async def test_creates_moment_at_first_citation(self, storage: Storage) -> None:
        rid = await _race(storage)
        await _seed_transcript(storage, rid)
        async with _client(storage, FakeLLMClient()) as c:
            await c.post("/api/llm/consent")
            qa_id = (
                await c.post(
                    f"/api/sessions/{rid}/llm/qa",
                    json={"question": "When did we tack?"},
                )
            ).json()["id"]
            resp = await c.post(f"/api/llm/qa/{qa_id}/save-as-moment")
            assert resp.status_code == 201
            mid = resp.json()["moment_id"]

        m = await storage.get_moment(mid)
        assert m is not None
        assert m["session_id"] == rid
        assert m["anchor_kind"] == "timestamp"

    @pytest.mark.asyncio
    async def test_save_with_no_citation_uses_session_anchor(
        self,
        storage: Storage,
    ) -> None:
        rid = await _race(storage)
        await _seed_transcript(storage, rid)
        # LLM returns a citation-less answer.
        fake = FakeLLMClient(
            ask_response=LLMResponse(
                text="Nothing notable.",
                citations=[],
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_create_tokens=0,
                cost_usd=0.0001,
            ),
        )
        async with _client(storage, fake) as c:
            await c.post("/api/llm/consent")
            qa_id = (
                await c.post(
                    f"/api/sessions/{rid}/llm/qa",
                    json={"question": "Anything?"},
                )
            ).json()["id"]
            resp = await c.post(f"/api/llm/qa/{qa_id}/save-as-moment")
            assert resp.status_code == 201
        m = await storage.get_moment(resp.json()["moment_id"])
        assert m["anchor_kind"] == "session"


class TestCostEndpoint:
    @pytest.mark.asyncio
    async def test_returns_state_and_caps(self, storage: Storage) -> None:
        rid = await _race(storage)
        async with _client(storage) as c:
            resp = await c.get(f"/api/sessions/{rid}/llm/cost")
            assert resp.status_code == 200
            data = resp.json()
            assert data["current_spend_usd"] == pytest.approx(0.0)
            assert data["soft_warn_usd"] == 1.00
            assert data["hard_cap_usd"] == 5.00
            assert data["state"] == "UnderSoft"


class TestRaceCapsAdmin:
    @pytest.mark.asyncio
    async def test_admin_sets_caps(self, storage: Storage) -> None:
        rid = await _race(storage)
        async with _client(storage) as c:
            resp = await c.put(
                f"/api/sessions/{rid}/llm/caps",
                json={"soft_warn_usd": 2.0, "hard_cap_usd": 10.0},
            )
            assert resp.status_code == 200
            cost = (await c.get(f"/api/sessions/{rid}/llm/cost")).json()
            assert cost["soft_warn_usd"] == 2.0
            assert cost["hard_cap_usd"] == 10.0


class TestPerRaceScope:
    @pytest.mark.asyncio
    async def test_qa_history_scoped_to_race(self, storage: Storage) -> None:
        r1 = await _race(storage, n=1)
        r2 = await _race(storage, n=2)
        await _seed_transcript(storage, r1)
        await _seed_transcript(storage, r2)
        async with _client(storage, FakeLLMClient()) as c:
            await c.post("/api/llm/consent")
            await c.post(f"/api/sessions/{r1}/llm/qa", json={"question": "q1"})
            r2_history = (await c.get(f"/api/sessions/{r2}/llm/qa")).json()
            assert r2_history["qa"] == []


class TestRolesViaAuthDisabled:
    """Spec decision-table row: 'crew | consented | Configure thresholds | No'.

    AUTH_DISABLED=true makes every request admin, so we flip it off and
    assert the endpoint 401s without a session — the role-rank check is
    covered by require_auth's existing tests.
    """

    @pytest.mark.asyncio
    async def test_caps_requires_admin(
        self,
        storage: Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTH_DISABLED", "false")
        rid = await _race(storage)
        async with _client(storage) as c:
            resp = await c.put(
                f"/api/sessions/{rid}/llm/caps",
                json={"soft_warn_usd": 2.0, "hard_cap_usd": 10.0},
            )
            assert resp.status_code == 401
        # cleanup happens via monkeypatch, but defensively reset.
        os.environ["AUTH_DISABLED"] = "true"
