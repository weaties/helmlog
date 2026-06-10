"""Decoder fixtures for the B&G timer PGN validator (scripts/verify_timer_pgns.py).

These lock the reverse-engineered byte layout we expect Triton2 to emit so that,
when the on-water capture comes back, a regression here means the hardware
disagrees with the fork's assumptions (and the decoder must be adapted) — not
that we broke the parser. The frame builders mirror the exact bytes HelmLog's
CANWriter / can_monitor produce in the MarkPre3802 fork.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_timer_pgns.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("verify_timer_pgns", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module by name.
    sys.modules["verify_timer_pgns"] = mod
    spec.loader.exec_module(mod)
    return mod


vtp = _load()

_MFR = bytes([0x41, 0x9F])
_SET_DISC = bytes([0x07, 0x42, 0x00, 0x01])


def _start_stop_payload(cmd: int) -> bytes:
    return _MFR + bytes([0xFF, 0xFF, 0x01, 0x17, cmd, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])


def _set_timer_payload(minutes: int) -> bytes:
    return _MFR + bytes([0xFF, 0xFF, 0xFF, 0xFF]) + _SET_DISC + bytes([minutes, 0xFF, 0xFF, 0xFF])


class TestDecode130850:
    @pytest.mark.parametrize(
        ("cmd", "action"),
        [(0x3D, "start"), (0x3E, "stop"), (0x3F, "nearest_minute"), (0x40, "reset")],
    )
    def test_actions(self, cmd: int, action: str) -> None:
        assert vtp.decode_simrad(vtp.PGN_START_STOP, _start_stop_payload(cmd)) == (action, None)

    def test_unknown_command_is_none(self) -> None:
        assert vtp.decode_simrad(vtp.PGN_START_STOP, _start_stop_payload(0x12)) is None

    def test_wrong_manufacturer_is_none(self) -> None:
        payload = bytes([0x00, 0x00]) + _start_stop_payload(0x3D)[2:]
        assert vtp.decode_simrad(vtp.PGN_START_STOP, payload) is None


class TestDecode130845:
    @pytest.mark.parametrize("minutes", [1, 3, 5, 10, 60])
    def test_set_minutes(self, minutes: int) -> None:
        assert vtp.decode_simrad(vtp.PGN_SET_TIMER, _set_timer_payload(minutes)) == ("set", minutes)

    def test_running_state_broadcast_ignored(self) -> None:
        # Same PGN, different discriminator (02 00 00 01) — must not decode as SET.
        payload = _MFR + bytes([0xFF, 0xFF, 0xFF, 0xFF, 0x02, 0x00, 0x00, 0x01, 5, 0, 0, 0])
        assert vtp.decode_simrad(vtp.PGN_SET_TIMER, payload) is None


class TestFastPacketReassembly:
    def test_set_timer_round_trip(self) -> None:
        payload = _set_timer_payload(5)
        buf = vtp.FastPacketBuffer()
        out = None
        for frame in vtp._fast_packet_frames(payload):
            out = buf.feed(vtp.PGN_SET_TIMER, 0x09, frame)
        assert out == payload
        assert vtp.decode_simrad(vtp.PGN_SET_TIMER, out) == ("set", 5)

    def test_concurrent_sources_do_not_collide(self) -> None:
        # Two ECUs interleaving the same PGN must reassemble independently,
        # regardless of how many Fast Packet frames each payload spans.
        pa, pb = _set_timer_payload(5), _set_timer_payload(9)
        fa, fb = vtp._fast_packet_frames(pa), vtp._fast_packet_frames(pb)
        assert len(fa) == len(fb)
        buf = vtp.FastPacketBuffer()
        out_a = out_b = None
        for frame_a, frame_b in zip(fa, fb, strict=True):
            out_a = buf.feed(vtp.PGN_SET_TIMER, 0x09, frame_a) or out_a
            out_b = buf.feed(vtp.PGN_SET_TIMER, 0x0A, frame_b) or out_b
        assert out_a == pa
        assert out_b == pb

    def test_out_of_order_frame_drops_session(self) -> None:
        payload = _set_timer_payload(5)
        frames = vtp._fast_packet_frames(payload)
        buf = vtp.FastPacketBuffer()
        buf.feed(vtp.PGN_SET_TIMER, 0x09, frames[0])
        # Skip straight to a later frame index — session should reset to None.
        assert buf.feed(vtp.PGN_SET_TIMER, 0x09, bytes([0x05]) + b"\x00" * 7) is None


def test_self_test_passes() -> None:
    assert vtp.self_test() == 0
