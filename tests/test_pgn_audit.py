"""Tests for the PGN audit feature (#789): storage, verdict, frame handling, web."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog import storage as storage_mod
from helmlog.nmea2000 import (
    PGN_SIMRAD_SET_TIMER,
    PGN_SIMRAD_START_STOP,
    FastPacketBuffer,
)
from helmlog.pgn_audit import handle_frame, verdict_from_summary
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_TS = datetime(2026, 6, 9, 18, 30, 0, tzinfo=UTC)

_MFR = bytes([0x41, 0x9F])
_SET_DISC = bytes([0x07, 0x42, 0x00, 0x01])


def _start_stop_payload(cmd: int) -> bytes:
    return _MFR + bytes([0xFF, 0xFF, 0x01, 0x17, cmd, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])


def _set_timer_payload(minutes: int) -> bytes:
    return _MFR + bytes([0xFF, 0xFF, 0xFF, 0xFF]) + _SET_DISC + bytes([minutes, 0xFF, 0xFF, 0xFF])


def _frames(payload: bytes) -> list[bytes]:
    out = [bytes([0x00, len(payload)]) + payload[:6]]
    offset, num = 6, 1
    while offset < len(payload):
        out.append(bytes([num]) + payload[offset : offset + 7])
        offset += 7
        num += 1
    return [f.ljust(8, b"\xff") for f in out]


# ---------------------------------------------------------------------------
# Storage: record + summary + prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_summary_decoded(storage: Storage) -> None:
    await storage.record_pgn_observation(
        observed_at=_TS,
        pgn=PGN_SIMRAD_SET_TIMER,
        source_addr=9,
        raw_hex="41 9f",
        action="set",
        minutes=5,
    )
    await storage.record_pgn_observation(
        observed_at=_TS,
        pgn=PGN_SIMRAD_START_STOP,
        source_addr=9,
        raw_hex="41 9f",
        action="start",
    )
    summary = await storage.get_pgn_audit_summary()
    assert summary["total"] == 2
    assert summary["pgns"][PGN_SIMRAD_SET_TIMER]["decoded"] == 1
    assert summary["pgns"][PGN_SIMRAD_SET_TIMER]["source_addrs"] == [9]
    assert summary["pgns"][PGN_SIMRAD_START_STOP]["frames"] == 1
    # recent is newest-first
    assert summary["recent"][0]["action"] == "start"
    assert summary["recent"][1]["minutes"] == 5


@pytest.mark.asyncio
async def test_undecoded_observation_recorded_raw(storage: Storage) -> None:
    await storage.record_pgn_observation(
        observed_at=_TS,
        pgn=PGN_SIMRAD_SET_TIMER,
        source_addr=12,
        raw_hex="de ad be ef",
        action=None,
    )
    summary = await storage.get_pgn_audit_summary()
    assert summary["pgns"][PGN_SIMRAD_SET_TIMER]["frames"] == 1
    assert summary["pgns"][PGN_SIMRAD_SET_TIMER]["decoded"] == 0
    assert summary["recent"][0]["decoded"] is False
    assert summary["recent"][0]["raw_hex"] == "de ad be ef"


@pytest.mark.asyncio
async def test_log_is_pruned_to_cap(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_mod, "_PGN_AUDIT_MAX_ROWS", 3)
    for i in range(10):
        await storage.record_pgn_observation(
            observed_at=_TS,
            pgn=PGN_SIMRAD_START_STOP,
            source_addr=i,
            raw_hex=f"{i:02x}",
            action="start",
        )
    summary = await storage.get_pgn_audit_summary()
    # Keeps newest 3 (id range pruned each insert): cap + the just-inserted row.
    assert summary["total"] <= 4
    assert summary["recent"][0]["source_addr"] == 9


# ---------------------------------------------------------------------------
# Verdict decision table
# ---------------------------------------------------------------------------


def _summary(set_decoded: int, set_frames: int, ss_decoded: int, ss_frames: int) -> dict:
    return {
        "pgns": {
            PGN_SIMRAD_SET_TIMER: {"decoded": set_decoded, "frames": set_frames},
            PGN_SIMRAD_START_STOP: {"decoded": ss_decoded, "frames": ss_frames},
        }
    }


def test_verdict_pass() -> None:
    assert verdict_from_summary(_summary(2, 2, 1, 1))["level"] == "PASS"


def test_verdict_partial_one_missing() -> None:
    v = verdict_from_summary(_summary(1, 1, 0, 0))
    assert v["level"] == "PARTIAL"
    assert "130850" in v["message"]


def test_verdict_partial_frames_undecoded() -> None:
    v = verdict_from_summary(_summary(0, 3, 0, 2))
    assert v["level"] == "PARTIAL"
    assert "different byte layout" in v["message"]


def test_verdict_fail_no_frames() -> None:
    assert verdict_from_summary(_summary(0, 0, 0, 0))["level"] == "FAIL"


def test_verdict_tolerates_string_keys() -> None:
    # get_pgn_audit_summary uses int keys, but be robust to JSON-roundtripped str keys.
    summary = {
        "pgns": {
            str(PGN_SIMRAD_SET_TIMER): {"decoded": 1, "frames": 1},
            str(PGN_SIMRAD_START_STOP): {"decoded": 1, "frames": 1},
        }
    }
    assert verdict_from_summary(summary)["level"] == "PASS"


# ---------------------------------------------------------------------------
# handle_frame (pure reassemble + decode)
# ---------------------------------------------------------------------------


def test_handle_frame_decodes_set() -> None:
    buf = FastPacketBuffer()
    obs = None
    for frame in _frames(_set_timer_payload(5)):
        obs = handle_frame(buf, PGN_SIMRAD_SET_TIMER, 9, frame, _TS.timestamp())
    assert obs is not None
    assert obs["action"] == "set"
    assert obs["minutes"] == 5
    assert obs["pgn"] == PGN_SIMRAD_SET_TIMER


def test_handle_frame_incomplete_returns_none() -> None:
    buf = FastPacketBuffer()
    # Only the first of multiple frames — not yet reassembled.
    first = _frames(_set_timer_payload(5))[0]
    assert handle_frame(buf, PGN_SIMRAD_SET_TIMER, 9, first, _TS.timestamp()) is None


def test_handle_frame_non_target_pgn_returns_none() -> None:
    buf = FastPacketBuffer()
    assert handle_frame(buf, 127250, 9, b"\x00" * 8, _TS.timestamp()) is None


def test_handle_frame_complete_but_undecoded_keeps_raw() -> None:
    # A target PGN that reassembles but whose layout we don't recognise: a
    # single-frame payload (total<=6) with the right manufacturer but a bogus
    # command. action is None, raw kept.
    buf = FastPacketBuffer()
    bogus = _MFR + bytes([0xFF, 0xFF, 0x99])  # 5 bytes -> single frame
    frame = bytes([0x00, len(bogus)]) + bogus
    obs = handle_frame(buf, PGN_SIMRAD_START_STOP, 7, frame.ljust(8, b"\xff"), _TS.timestamp())
    assert obs is not None
    assert obs["action"] is None
    assert "41 9f" in obs["raw_hex"]


# ---------------------------------------------------------------------------
# Web route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_renders(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/admin/pgn-audit")
    assert r.status_code == 200
    assert "Timer PGN Audit" in r.text


@pytest.mark.asyncio
async def test_state_empty_is_fail(storage: Storage) -> None:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/pgn-audit/state")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"]["level"] == "FAIL"
    assert body["summary"]["total"] == 0
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_state_reflects_observations(storage: Storage) -> None:
    await storage.record_pgn_observation(
        observed_at=_TS,
        pgn=PGN_SIMRAD_SET_TIMER,
        source_addr=9,
        raw_hex="41 9f",
        action="set",
        minutes=5,
    )
    await storage.record_pgn_observation(
        observed_at=_TS,
        pgn=PGN_SIMRAD_START_STOP,
        source_addr=9,
        raw_hex="41 9f",
        action="start",
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/pgn-audit/state")
    body = r.json()
    assert body["verdict"]["level"] == "PASS"
    # JSON serialises the int PGN keys as strings — the page reads them as such.
    assert body["summary"]["pgns"]["130845"]["decoded"] == 1
    assert body["summary"]["recent"][0]["action"] == "start"
