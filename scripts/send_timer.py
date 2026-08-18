#!/usr/bin/env python3
"""
Send B&G race timer START/STOP/RESET via PGN 130850 on can0.
Performs ISO address claiming (PGN 60928) first so B&G accepts the messages.

Usage: python3 send_timer.py [start|stop|reset|nearest]
"""

import socket
import struct
import sys
import time

# --- Config ---
CHANNEL = "can0"
SOURCE_ADDR = 0x3B  # our claimed address

# Commands (byte 6 of PGN 130850 payload)
COMMANDS = {
    "start": 0x3D,
    "stop": 0x3E,
    "nearest": 0x3F,
    "reset": 0x40,
}


def make_can_frame(can_id: int, data: bytes) -> bytes:
    """Pack a raw CAN frame for Linux socketcan (struct can_frame)."""
    assert len(data) <= 8
    data = data.ljust(8, b"\x00")
    return struct.pack("=IB3x8s", can_id | socket.CAN_EFF_FLAG, len(data), data)


def send_frame(sock: socket.socket, can_id: int, data: bytes) -> None:
    sock.send(make_can_frame(can_id, data))


def build_pgn_can_id(priority: int, pgn: int, src: int) -> int:
    """Build a 29-bit CAN ID for a PDU2 (broadcast) PGN."""
    # For PDU2 (PF >= 240): ID = priority<<26 | pgn<<8 | src
    # pgn already encodes PF and PS
    return (priority << 26) | (pgn << 8) | src


def send_address_claim(sock: socket.socket) -> None:
    """
    Broadcast ISO Address Claim (PGN 60928 = 0xEE00).
    CAN ID: priority=6, PGN=0xEE00, dest=0xFF (global), src=SOURCE_ADDR
    -> (6<<26)|(0xEE<<16)|(0xFF<<8)|SOURCE_ADDR = 0x18EEFF3B
    """
    # NAME: Arbitrary Address Capable, Marine, Display function
    name = (
        (1 << 63)  # Arbitrary Address Capable
        | (4 << 60)  # Industry Group = Marine
        | (0 << 56)  # System Instance
        | (25 << 49)  # Device Class = Inter/Intranetwork
        | (0 << 48)  # Reserved
        | (130 << 40)  # Device Function = Display
        | (0 << 35)  # Device Instance Upper
        | (0 << 32)  # Device Instance Lower
        | (999 << 21)  # Manufacturer Code (arbitrary)
        | 1  # Identity Number
    )
    name_bytes = name.to_bytes(8, "little")
    can_id = 0x18EEFF00 | SOURCE_ADDR
    send_frame(sock, can_id, name_bytes)
    print(f"  Address claim sent (0x{can_id:08X}): {name_bytes.hex()}")


def send_pgn130850(sock: socket.socket, command_byte: int) -> None:
    """
    Send PGN 130850 (0x1FF22) as Fast Packet — two 8-byte CAN frames.
    Payload (12 bytes):
      [0-1]  41 9F  Simrad manufacturer ID
      [2-3]  FF FF  reserved
      [4-5]  01 17  reserved
      [6]    cmd    START/STOP/RESET/NEAREST
      [7]    00
      [8-11] FF FF FF FF  padding
    """
    payload = bytes(
        [0x41, 0x9F, 0xFF, 0xFF, 0x01, 0x17, command_byte, 0x00, 0xFF, 0xFF, 0xFF, 0xFF]
    )

    # Fast Packet: priority=2, PGN=0x1FF22, src=SOURCE_ADDR
    # PDU2: PF=0xFF, PS=0x22 → CAN ID = (2<<26)|(0xFF<<16)|(0x22<<8)|SOURCE_ADDR
    can_id = (2 << 26) | (0x1FF22 << 8) | SOURCE_ADDR

    # Frame 0: [seq|frame0, total_len, payload[0:6]]
    frame0 = bytes([0x00, len(payload)]) + payload[0:6]
    # Frame 1: [seq|frame1, payload[6:13]]
    frame1 = bytes([0x01]) + payload[6:] + b"\xff" * (7 - len(payload[6:]))

    send_frame(sock, can_id, frame0)
    time.sleep(0.005)
    send_frame(sock, can_id, frame1)
    print(f"  PGN 130850 sent (0x{can_id:08X})")
    print(f"    frame0: {frame0.hex()}")
    print(f"    frame1: {frame1.hex()}")


def main() -> None:
    cmd_name = sys.argv[1].lower() if len(sys.argv) > 1 else "start"
    if cmd_name not in COMMANDS:
        print(f"Unknown command. Choose from: {', '.join(COMMANDS)}")
        sys.exit(1)
    command_byte = COMMANDS[cmd_name]
    print(f"Sending B&G timer command: {cmd_name.upper()} (0x{command_byte:02X})")

    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((CHANNEL,))

    # Step 1: claim our address
    print("1. Sending ISO Address Claim...")
    send_address_claim(sock)

    # Step 2: wait for challenges (250ms per NMEA 2000 spec)
    print("2. Waiting 250ms for address conflicts...")
    time.sleep(0.25)

    # Step 3: send the command
    print(f"3. Sending timer {cmd_name.upper()} command...")
    send_pgn130850(sock, command_byte)

    sock.close()
    print("Done.")


if __name__ == "__main__":
    main()
