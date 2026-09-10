"""Tests for scripts/analysis/video/common.py — race index, telemetry, geometry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from scripts.analysis.video import common
from tests.video_analysis.conftest import GUN, SESSION_START

if TYPE_CHECKING:
    import sqlite3


def test_bearing_round_trip() -> None:
    width = 7680
    assert common.x_to_rel_bearing(width / 2, width) == 0.0
    assert common.x_to_rel_bearing(0, width) == -180.0
    assert common.rel_bearing_to_x(0.0, width) == width // 2
    assert common.rel_bearing_to_x(90.0, width) == width * 3 // 4
    assert common.abs_bearing(350.0, 20.0) == 10.0
    assert common.angle_diff(10.0, 350.0) == 20.0
    assert common.angle_diff(350.0, 10.0) == -20.0


def test_local_date_and_wednesday() -> None:
    # 2026-09-10 01:17 UTC is Wednesday 2026-09-09 18:17 in Seattle.
    assert common.local_date(SESSION_START) == "2026-09-09"
    assert common.is_wednesday_local(SESSION_START)
    assert not common.is_wednesday_local(SESSION_START + timedelta(days=1))


def test_load_race_joins_video_gun_roundings_result(meta_db: sqlite3.Connection) -> None:
    race = common.load_race(meta_db, 254)
    assert race.name == "20260909-Cyc-1"
    assert race.video is not None and race.video.video_id == "jygj-NbqFJE"
    assert race.video.sync_offset_s == 0.0
    # the race_start inside the session window, not the earlier/later ones
    assert race.vakaros_gun == GUN
    assert len(race.roundings) == 5  # raw; instants.py filters pre-start turns
    assert race.result_place == 5
    assert race.fleet_size == 3
    assert race.tags == ("start-2nd-row",)
    assert race.local_date == "2026-09-09"


def test_load_race_missing(meta_db: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        common.load_race(meta_db, 999)


def test_list_cyc_wednesday_races_filters(meta_db: sqlite3.Connection) -> None:
    # A Ballard Cup race on a Tuesday with video must not be included.
    meta_db.execute(
        "INSERT INTO races VALUES (300,'20260908-Ballard-1','Ballard cup',1,'2026-09-08',"
        "'2026-09-09T01:30:00+00:00','2026-09-09T03:00:00+00:00','race','live',NULL,NULL)"
    )
    meta_db.execute(
        "INSERT INTO race_videos VALUES (79,300,'https://youtu.be/x','x','',"
        "'2026-09-09T01:30:00+00:00',0.0,100.0,'')"
    )
    # A CYC race without a video is skipped too.
    meta_db.execute(
        "INSERT INTO races VALUES (301,'20260909-Cyc-2','Cyc',2,'2026-09-09',"
        "'2026-09-10T01:51:17+00:00','2026-09-10T02:28:12+00:00','race','live',NULL,NULL)"
    )
    ids = [r.id for r in common.list_cyc_wednesday_races(meta_db)]
    assert ids == [254]


def test_video_ref_time_mapping() -> None:
    v = common.VideoRef("id", "u", "t", SESSION_START, 0.0, 1976.0)
    assert v.video_t(GUN) == pytest.approx(474.0)
    assert v.utc_at(474.0) == GUN
    v2 = common.VideoRef("id", "u", "t", SESSION_START, 1754.0, None)
    assert v2.video_t(SESSION_START + timedelta(seconds=10)) == pytest.approx(1764.0)


def test_telemetry_stamp_and_tack(meta_db: sqlite3.Connection) -> None:
    tel = common.load_telemetry(meta_db, SESSION_START, SESSION_START + timedelta(minutes=10))
    st = tel.at(GUN)
    assert st.hdg == pytest.approx(337.0, abs=0.3)
    assert st.twa == 55.0
    assert st.tws == 8.6
    assert st.twd == pytest.approx((337.0 + 55.0) % 360, abs=0.3)  # derived from hdg + TWA
    assert st.sog == 4.7 and st.bsp == 4.5
    assert tel.tack_at(GUN) == "stbd"
    # Backward search covers small gaps; far outside the window there is nothing.
    assert tel.at(SESSION_START + timedelta(minutes=10, seconds=3)).hdg is not None
    assert tel.at(SESSION_START + timedelta(minutes=20)).hdg is None


def test_ledger_schema_and_effective_sync(
    ledger: sqlite3.Connection, meta_db: sqlite3.Connection
) -> None:
    race = common.load_race(meta_db, 254)
    assert common.effective_video(ledger, race) == race.video
    assert common.effective_gun(ledger, race) == GUN
    horn_utc = GUN - timedelta(seconds=6)
    ledger.execute(
        "INSERT INTO sync VALUES (254,'jygj-NbqFJE','horn+vakaros',?,468.0,?,468.0,6.0,3,?)",
        (GUN.isoformat(), GUN.isoformat(), common.utcnow_iso()),
    )
    v = common.effective_video(ledger, race)
    assert v is not None and v.video_t(GUN) == pytest.approx(468.0)
    assert v.video_t(horn_utc) == pytest.approx(462.0)
    assert common.effective_gun(ledger, race) == GUN


def test_dumps_is_deterministic() -> None:
    a = common.dumps({"b": 1, "a": datetime(2026, 1, 1, tzinfo=UTC)})
    assert a == '{"a":"2026-01-01T00:00:00+00:00","b":1}'
