"""Cost-cap state machine and consent-gate enforcement (#697 spec §3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from helmlog.llm_policy import (
    CostCapState,
    PolicyCheck,
    check_can_query,
    get_effective_caps,
)
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


async def _admin(storage: Storage) -> int:
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO users (email, role, created_at) "
        "VALUES ('a@x', 'admin', '2026-01-01T00:00:00+00:00')",
    )
    await db.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


class TestEffectiveCaps:
    @pytest.mark.asyncio
    async def test_defaults_when_no_override(self, storage: Storage) -> None:
        rid = await _race(storage)
        caps = await get_effective_caps(storage, rid)
        # First-pass defaults — see spec open question 2.
        assert caps.soft_warn_usd == 1.00
        assert caps.hard_cap_usd == 5.00

    @pytest.mark.asyncio
    async def test_per_race_override_wins(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.set_race_caps(
            race_id=rid,
            soft_warn_usd=2.50,
            hard_cap_usd=10.00,
            by_user=uid,
        )
        caps = await get_effective_caps(storage, rid)
        assert caps.soft_warn_usd == 2.50
        assert caps.hard_cap_usd == 10.00


class TestConsentGate:
    @pytest.mark.asyncio
    async def test_blocks_without_consent(self, storage: Storage) -> None:
        rid = await _race(storage)
        check = await check_can_query(storage, rid, estimate_usd=0.01)
        assert check.allowed is False
        assert check.reason == "consent_required"

    @pytest.mark.asyncio
    async def test_allows_after_consent(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.acknowledge_llm_consent(user_id=uid)
        check = await check_can_query(storage, rid, estimate_usd=0.01)
        assert check.allowed is True


class TestCostCapStateMachine:
    @pytest.mark.asyncio
    async def test_under_soft_no_confirmation(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.acknowledge_llm_consent(user_id=uid)
        check = await check_can_query(storage, rid, estimate_usd=0.05)
        assert check.allowed is True
        assert check.state is CostCapState.UNDER_SOFT
        assert check.requires_confirmation is False

    @pytest.mark.asyncio
    async def test_soft_warned_requires_confirmation(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.acknowledge_llm_consent(user_id=uid)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=1.20,
        )
        check = await check_can_query(storage, rid, estimate_usd=0.05)
        assert check.allowed is True
        assert check.state is CostCapState.SOFT_WARNED
        assert check.requires_confirmation is True

    @pytest.mark.asyncio
    async def test_at_cap_blocks(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.acknowledge_llm_consent(user_id=uid)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=5.00,
        )
        check = await check_can_query(storage, rid, estimate_usd=0.05)
        assert check.allowed is False
        assert check.state is CostCapState.AT_CAP
        assert check.reason == "hard_cap_reached"

    @pytest.mark.asyncio
    async def test_estimate_pushing_past_cap_rejected_preflight(
        self,
        storage: Storage,
    ) -> None:
        """Spec §3 guard: a query whose estimated cost would push spend
        past hard_cap is rejected before the API call."""
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.acknowledge_llm_consent(user_id=uid)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=4.95,
        )
        check = await check_can_query(storage, rid, estimate_usd=0.10)
        assert check.allowed is False
        assert check.state is CostCapState.AT_CAP
        assert check.reason == "would_exceed_cap"

    @pytest.mark.asyncio
    async def test_admin_raises_cap_unblocks(self, storage: Storage) -> None:
        """State transition: AtCap → SoftWarned when admin raises hard_cap."""
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.acknowledge_llm_consent(user_id=uid)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
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
        capped = await check_can_query(storage, rid, estimate_usd=0.01)
        assert capped.state is CostCapState.AT_CAP

        await storage.set_race_caps(
            race_id=rid,
            soft_warn_usd=1.00,
            hard_cap_usd=20.00,
            by_user=uid,
        )
        unblocked = await check_can_query(storage, rid, estimate_usd=0.01)
        assert unblocked.allowed is True
        assert unblocked.state is CostCapState.SOFT_WARNED


class TestPolicyCheckShape:
    @pytest.mark.asyncio
    async def test_carries_current_spend_and_caps(self, storage: Storage) -> None:
        rid = await _race(storage)
        uid = await _admin(storage)
        await storage.acknowledge_llm_consent(user_id=uid)
        await storage.insert_llm_qa(
            race_id=rid,
            user_id=uid,
            question="q",
            answer="a",
            citations=[],
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
            cost_usd=0.30,
        )
        check = await check_can_query(storage, rid, estimate_usd=0.01)
        assert isinstance(check, PolicyCheck)
        assert check.current_spend_usd == pytest.approx(0.30)
        assert check.soft_warn_usd == 1.00
        assert check.hard_cap_usd == 5.00
