"""Tests for analysis/compare.py — best/median selection helpers (#741)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from helmlog.analysis.compare import (
    assemble_compare_set,
    filter_pool,
    list_maneuver_pairs,
    pick_best,
    pick_median,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage


def _m(
    *,
    mid: int,
    metric_value: float | None,
    session_start: str = "2026-01-01T00:00:00+00:00",
    mtype: str = "tack",
    entry_tws: float | None = 10.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal enriched-maneuver dict for tests."""
    out: dict[str, Any] = {
        "id": mid,
        "type": mtype,
        "distance_loss_m": metric_value,
        "entry_tws": entry_tws,
        "session_start_utc": session_start,
    }
    if extra:
        out.update(extra)
    return out


class TestPickBest:
    def test_returns_n_lowest_values(self) -> None:
        items = [
            _m(mid=1, metric_value=10.0),
            _m(mid=2, metric_value=2.0),
            _m(mid=3, metric_value=8.0),
            _m(mid=4, metric_value=4.0),
            _m(mid=5, metric_value=6.0),
        ]
        result = pick_best(items, metric="distance_loss_m", n=3)
        assert [m["id"] for m in result] == [2, 4, 5]

    def test_skips_items_with_none_metric(self) -> None:
        items = [
            _m(mid=1, metric_value=None),
            _m(mid=2, metric_value=2.0),
            _m(mid=3, metric_value=4.0),
        ]
        result = pick_best(items, metric="distance_loss_m", n=3)
        assert [m["id"] for m in result] == [2, 3]

    def test_n_larger_than_pool_returns_all(self) -> None:
        items = [_m(mid=1, metric_value=5.0), _m(mid=2, metric_value=3.0)]
        result = pick_best(items, metric="distance_loss_m", n=5)
        assert [m["id"] for m in result] == [2, 1]

    def test_empty_pool_returns_empty(self) -> None:
        assert pick_best([], metric="distance_loss_m", n=3) == []

    def test_all_none_returns_empty(self) -> None:
        items = [_m(mid=1, metric_value=None), _m(mid=2, metric_value=None)]
        assert pick_best(items, metric="distance_loss_m", n=3) == []

    def test_ties_broken_by_recency_newer_first(self) -> None:
        items = [
            _m(mid=1, metric_value=2.0, session_start="2026-01-01T00:00:00+00:00"),
            _m(mid=2, metric_value=2.0, session_start="2026-03-01T00:00:00+00:00"),
            _m(mid=3, metric_value=5.0),
        ]
        result = pick_best(items, metric="distance_loss_m", n=2)
        assert [m["id"] for m in result] == [2, 1]

    def test_alternative_metric_field(self) -> None:
        items = [
            _m(mid=1, metric_value=2.0, extra={"time_to_recover_s": 30.0}),
            _m(mid=2, metric_value=10.0, extra={"time_to_recover_s": 5.0}),
        ]
        result = pick_best(items, metric="time_to_recover_s", n=1)
        assert [m["id"] for m in result] == [2]


class TestPickMedian:
    def test_picks_n_closest_to_median(self) -> None:
        # values: 1, 2, 3, 4, 5, 6, 7  → median = 4
        # closest 3: 4, 3 (or 5), 5 (or 3)
        items = [_m(mid=i, metric_value=float(i)) for i in range(1, 8)]
        result = pick_median(items, metric="distance_loss_m", n=3)
        ids = sorted(m["id"] for m in result)
        assert ids == [3, 4, 5]

    def test_even_count_uses_average_of_middle_two(self) -> None:
        # values: 2, 4, 6, 8 → median = 5; closest are 4 and 6 (distance 1)
        items = [
            _m(mid=1, metric_value=2.0),
            _m(mid=2, metric_value=4.0),
            _m(mid=3, metric_value=6.0),
            _m(mid=4, metric_value=8.0),
        ]
        result = pick_median(items, metric="distance_loss_m", n=2)
        ids = sorted(m["id"] for m in result)
        assert ids == [2, 3]

    def test_skips_none_metric(self) -> None:
        items = [
            _m(mid=1, metric_value=None),
            _m(mid=2, metric_value=2.0),
            _m(mid=3, metric_value=4.0),
            _m(mid=4, metric_value=6.0),
        ]
        result = pick_median(items, metric="distance_loss_m", n=3)
        ids = {m["id"] for m in result}
        assert ids == {2, 3, 4}

    def test_empty_returns_empty(self) -> None:
        assert pick_median([], metric="distance_loss_m", n=3) == []

    def test_single_item_returns_that_item(self) -> None:
        items = [_m(mid=1, metric_value=4.0)]
        result = pick_median(items, metric="distance_loss_m", n=3)
        assert [m["id"] for m in result] == [1]

    def test_tie_in_distance_to_median_broken_by_recency(self) -> None:
        # values: 2, 4, 6 → median = 4; both 2 and 6 are distance-2 from median
        items = [
            _m(mid=1, metric_value=2.0, session_start="2026-01-01T00:00:00+00:00"),
            _m(mid=2, metric_value=4.0, session_start="2026-02-01T00:00:00+00:00"),
            _m(mid=3, metric_value=6.0, session_start="2026-03-01T00:00:00+00:00"),
        ]
        result = pick_median(items, metric="distance_loss_m", n=2)
        # 4 is closest (dist 0); tie between 2 and 6 broken by recency → 6 wins
        assert [m["id"] for m in result] == [2, 3]

    def test_excludes_items_already_in_best_set(self) -> None:
        # When passed an exclusion id-set, the median picker must skip them
        # so the best/median sets don't overlap.
        items = [_m(mid=i, metric_value=float(i)) for i in range(1, 8)]
        # Pretend ids {1, 2} are the "best" — median should not include them.
        result = pick_median(items, metric="distance_loss_m", n=3, exclude_ids={1, 2})
        assert all(m["id"] not in {1, 2} for m in result)
        # Remaining values 3..7, median = 5; closest 3 are 4, 5, 6.
        ids = sorted(m["id"] for m in result)
        assert ids == [4, 5, 6]


class TestFilterPool:
    def test_filters_by_type(self) -> None:
        items = [
            _m(mid=1, metric_value=2.0, mtype="tack"),
            _m(mid=2, metric_value=2.0, mtype="gybe"),
            _m(mid=3, metric_value=2.0, mtype="tack"),
        ]
        result = filter_pool(items, maneuver_type="tack")
        assert {m["id"] for m in result} == {1, 3}

    def test_filters_by_tws_range_inclusive(self) -> None:
        items = [
            _m(mid=1, metric_value=2.0, entry_tws=7.9),
            _m(mid=2, metric_value=2.0, entry_tws=8.0),
            _m(mid=3, metric_value=2.0, entry_tws=10.0),
            _m(mid=4, metric_value=2.0, entry_tws=12.0),
            _m(mid=5, metric_value=2.0, entry_tws=12.1),
        ]
        result = filter_pool(items, tws_min=8.0, tws_max=12.0)
        assert {m["id"] for m in result} == {2, 3, 4}

    def test_drops_items_missing_tws_when_range_set(self) -> None:
        items = [
            _m(mid=1, metric_value=2.0, entry_tws=None),
            _m(mid=2, metric_value=2.0, entry_tws=10.0),
        ]
        result = filter_pool(items, tws_min=8.0, tws_max=12.0)
        assert {m["id"] for m in result} == {2}

    def test_no_filters_returns_pool_unchanged(self) -> None:
        items = [_m(mid=1, metric_value=2.0), _m(mid=2, metric_value=2.0)]
        result = filter_pool(items)
        assert len(result) == 2

    def test_combined_type_and_tws_filter(self) -> None:
        items = [
            _m(mid=1, metric_value=2.0, mtype="tack", entry_tws=10.0),
            _m(mid=2, metric_value=2.0, mtype="tack", entry_tws=20.0),
            _m(mid=3, metric_value=2.0, mtype="gybe", entry_tws=10.0),
        ]
        result = filter_pool(items, maneuver_type="tack", tws_min=8.0, tws_max=12.0)
        assert {m["id"] for m in result} == {1}


class TestRoundingTypes:
    """Roundings are stored as type='rounding' with a discriminator in details."""

    def test_filter_weather_rounding(self) -> None:
        items = [
            _m(
                mid=1,
                metric_value=2.0,
                mtype="rounding",
                extra={"details": {"rounding_kind": "weather"}},
            ),
            _m(
                mid=2,
                metric_value=2.0,
                mtype="rounding",
                extra={"details": {"rounding_kind": "leeward"}},
            ),
            _m(mid=3, metric_value=2.0, mtype="tack"),
        ]
        result = filter_pool(items, maneuver_type="weather_rounding")
        assert {m["id"] for m in result} == {1}

    def test_filter_leeward_rounding(self) -> None:
        items = [
            _m(
                mid=1,
                metric_value=2.0,
                mtype="rounding",
                extra={"details": {"rounding_kind": "weather"}},
            ),
            _m(
                mid=2,
                metric_value=2.0,
                mtype="rounding",
                extra={"details": {"rounding_kind": "leeward"}},
            ),
        ]
        result = filter_pool(items, maneuver_type="leeward_rounding")
        assert {m["id"] for m in result} == {2}


class TestAssembleCompareSet:
    def test_returns_best_and_median_disjoint(self) -> None:
        # 10 maneuvers with metric values 1..10. Best 3 = {1,2,3}.
        # Median of full set = 5.5; closest 3 from remaining = {5, 6, 4}.
        items = [_m(mid=i, metric_value=float(i)) for i in range(1, 11)]
        result = assemble_compare_set(items, metric="distance_loss_m", n=3)
        best_ids = {m["id"] for m in result["best"]}
        median_ids = {m["id"] for m in result["median"]}
        assert best_ids == {1, 2, 3}
        assert best_ids.isdisjoint(median_ids)
        assert len(result["median"]) == 3
        assert result["pool_size"] == 10
        assert result["median_value"] == pytest.approx(5.5)

    def test_pool_too_small_returns_what_it_has(self) -> None:
        items = [_m(mid=1, metric_value=1.0), _m(mid=2, metric_value=2.0)]
        result = assemble_compare_set(items, metric="distance_loss_m", n=3)
        # Both go to "best"; "median" is empty since exclude eats remainder.
        assert len(result["best"]) == 2
        assert len(result["median"]) == 0
        assert result["pool_size"] == 2

    def test_empty_pool(self) -> None:
        result = assemble_compare_set([], metric="distance_loss_m", n=3)
        assert result["best"] == []
        assert result["median"] == []
        assert result["pool_size"] == 0
        assert result["median_value"] is None


_TS = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_session_with_maneuvers(
    storage: Storage,
    *,
    session_id: int,
    name: str,
    maneuvers: list[tuple[str, str | None]],
) -> None:
    """Insert a session and a list of (type, rounding_kind) maneuvers."""
    db = storage._conn()
    start = _TS + timedelta(hours=session_id)
    end = start + timedelta(minutes=30)
    await db.execute(
        "INSERT INTO races"
        " (id, name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            name,
            "test-event",
            session_id,
            start.date().isoformat(),
            "race",
            start.isoformat(),
            end.isoformat(),
        ),
    )
    for i, (mtype, rounding_kind) in enumerate(maneuvers):
        ts = (start + timedelta(seconds=60 + i * 10)).isoformat()
        details = None
        if rounding_kind is not None:
            import json

            details = json.dumps({"rounding_kind": rounding_kind})
        await db.execute(
            "INSERT INTO maneuvers"
            " (session_id, type, ts, end_ts, duration_sec, loss_kts,"
            "  vmg_loss_kts, tws_bin, twa_bin, details)"
            " VALUES (?, ?, ?, ?, 10.0, 1.0, NULL, 10, 40, ?)",
            (session_id, mtype, ts, ts, details),
        )
    await db.commit()


class TestListManeuverPairs:
    @pytest.mark.asyncio
    async def test_returns_all_pairs_when_no_filter(self, storage: Storage) -> None:
        await _seed_session_with_maneuvers(
            storage,
            session_id=1,
            name="s1",
            maneuvers=[("tack", None), ("gybe", None)],
        )
        await _seed_session_with_maneuvers(
            storage,
            session_id=2,
            name="s2",
            maneuvers=[("tack", None)],
        )
        pairs = await list_maneuver_pairs(storage)
        # Pairs are (session_id, maneuver_id). 3 maneuvers total.
        assert len(pairs) == 3
        assert {p[0] for p in pairs} == {1, 2}

    @pytest.mark.asyncio
    async def test_filter_by_type_tack(self, storage: Storage) -> None:
        await _seed_session_with_maneuvers(
            storage,
            session_id=1,
            name="s1",
            maneuvers=[("tack", None), ("gybe", None), ("tack", None)],
        )
        pairs = await list_maneuver_pairs(storage, maneuver_type="tack")
        assert len(pairs) == 2

    @pytest.mark.asyncio
    async def test_filter_weather_rounding_uses_rounding_kind(self, storage: Storage) -> None:
        await _seed_session_with_maneuvers(
            storage,
            session_id=1,
            name="s1",
            maneuvers=[
                ("rounding", "weather"),
                ("rounding", "leeward"),
                ("tack", None),
            ],
        )
        weather = await list_maneuver_pairs(storage, maneuver_type="weather_rounding")
        leeward = await list_maneuver_pairs(storage, maneuver_type="leeward_rounding")
        assert len(weather) == 1
        assert len(leeward) == 1

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty(self, storage: Storage) -> None:
        pairs = await list_maneuver_pairs(storage)
        assert pairs == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
