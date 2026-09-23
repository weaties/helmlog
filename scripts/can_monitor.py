"""Live CAN monitor for Simrad/B&G timer frames.

Decodes Fast Packet PGNs 130845 (Set Timer) and 130850 (Start/Stop/Reset)
from can0 in real time.  Frames we transmit (SA=0x7E) are labelled [OUT];
all others are [IN].

Usage:
    uv run python scripts/can_monitor.py [--channel can0]

In another terminal, press ARM in the web UI (or run a test send below),
then watch the decoded output here to verify the minutes byte.

Test send (5 minutes):
    uv run python scripts/can_monitor.py --send-set 5
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime

import can

sys.path.insert(0, "src")
from helmlog.nmea2000 import (
    PGN_SIMRAD_SET_TIMER,
    PGN_SIMRAD_START_STOP,
    FastPacketBuffer,
    decode,
)

_FAST_PGNS = {PGN_SIMRAD_SET_TIMER, PGN_SIMRAD_START_STOP}
_OUR_SA = 0x7E

# Priority / DP constants used to rebuild CAN IDs for filtering
_CAN_ID_130845_BASE = 0x0DFF1D00  # priority 3, DP 1, PF 0xFF, PS 0x1D
_CAN_ID_130850_BASE = 0x19FF2200  # priority 6, DP 1, PF 0xFF, PS 0x22


def pgn_from_can_id(can_id: int) -> tuple[int, int]:
    """Return (pgn, source_address) from a 29-bit extended CAN ID."""
    sa = can_id & 0xFF
    ps = (can_id >> 8) & 0xFF
    pf = (can_id >> 16) & 0xFF
    dp = (can_id >> 24) & 0x01
    pgn = dp << 16 | pf << 8 | ps if pf >= 240 else dp << 16 | pf << 8
    return pgn, sa


def hex_bytes(data: bytes | bytearray) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _fast_packet_frames(can_id: int, payload: bytes, seq: int = 0) -> list[can.Message]:
    seq_bits = (seq & 0x7) << 5
    frames: list[can.Message] = []
    frame0 = bytes([seq_bits | 0x00, len(payload)]) + payload[:6]
    frames.append(
        can.Message(arbitration_id=can_id, data=frame0.ljust(8, b"\xff"), is_extended_id=True)
    )
    offset, frame_num = 6, 1
    while offset < len(payload):
        chunk = payload[offset : offset + 7]
        frames.append(
            can.Message(
                arbitration_id=can_id,
                data=(bytes([seq_bits | frame_num]) + chunk).ljust(8, b"\xff"),
                is_extended_id=True,
            )
        )
        offset += 7
        frame_num += 1
    return frames


def send_set(bus: can.BusABC, minutes: int) -> None:
    payload = bytes(
        [0x41, 0x9F, 0xFF, 0xFF, 0xFF, 0xFF, 0x07, 0x42, 0x00, 0x01, minutes, 0x00, 0x00, 0x00]
    )
    can_id = _CAN_ID_130845_BASE | _OUR_SA
    frames = _fast_packet_frames(can_id, payload)
    print(f"\n>>> Sending SET {minutes} min (PGN 130845, SA=0x{_OUR_SA:02X})")
    for i, f in enumerate(frames):
        print(f"    frame {i}: {hex_bytes(f.data)}")
        bus.send(f)
        if i < len(frames) - 1:
            time.sleep(0.002)
    print()


def monitor(channel: str, send_minutes: int | None) -> None:
    buf = FastPacketBuffer()

    print(f"Opening {channel}  (Ctrl-C to stop)")
    print("Watching PGN 130845 (Set Timer) and 130850 (Start/Stop/Reset)")
    print(f"[OUT] = our frames (SA=0x{_OUR_SA:02X}), [IN] = B&G / network\n")

    with can.Bus(channel=channel, interface="socketcan") as bus:
        if send_minutes is not None:
            time.sleep(0.05)
            send_set(bus, send_minutes)

        for msg in bus:
            if not msg.is_extended_id:
                continue
            pgn, sa = pgn_from_can_id(msg.arbitration_id)
            if pgn not in _FAST_PGNS:
                continue

            direction = "[OUT]" if sa == _OUR_SA else "[IN] "
            ts = datetime.fromtimestamp(msg.timestamp, tz=UTC).strftime("%H:%M:%S.%f")[:-3]
            frame_byte = msg.data[0] & 0x1F if msg.data else 0
            seq_byte = (msg.data[0] >> 5) & 0x7 if msg.data else 0
            raw = hex_bytes(msg.data)

            print(
                f"{ts} {direction} PGN {pgn}  SA=0x{sa:02X}  seq={seq_byte} "
                f"frame={frame_byte}  [{raw}]"
            )

            payload = buf.feed(pgn, sa, bytes(msg.data))
            if payload is not None:
                print(f"         → reassembled ({len(payload)} bytes): {hex_bytes(payload)}")
                rec = decode(pgn, payload, sa, msg.timestamp)
                if rec is not None:
                    if pgn == PGN_SIMRAD_SET_TIMER:
                        mins = getattr(rec, "minutes", None)
                        print(
                            f"         → SET TIMER  minutes={mins}  (byte[10]=0x{payload[10]:02X})"
                        )
                    else:
                        action = getattr(rec, "action", "?")
                        print(f"         → ACTION     {action}")
                else:
                    print("         → (not decoded — running-state broadcast or unknown)")
                print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Live B&G timer CAN monitor")
    ap.add_argument("--channel", default="can0")
    ap.add_argument(
        "--send-set",
        type=int,
        metavar="MINUTES",
        help="After opening the bus, immediately send a SET command with this many minutes",
    )
    args = ap.parse_args()
    try:
        monitor(args.channel, args.send_set)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
