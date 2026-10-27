"""CAN bus writer — transmit NMEA 2000 timer commands to B&G instruments.

Hardware module: imported only by main.py.  Route handlers receive this via
app.state.can_writer and call send(); they never touch python-can directly.

Address claiming (PGN 60928) is performed on start() so the B&G accepts
frames from our source address for the lifetime of the process.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Literal

import can
from loguru import logger

# Source address we claim on the NMEA 2000 network.
_SA = 0x7E

# PGN 130850 = 0x1FF22  start/stop/reset/nearest-minute  priority 6, dp 1
_CAN_ID_130850 = 0x19FF2200 | _SA
# PGN 130845 = 0x1FF1D  set timer duration                priority 3, dp 1
_CAN_ID_130845 = 0x0DFF1D00 | _SA
# PGN 60928  = 0xEE00   ISO Address Claim                 priority 6, dp 0, dest broadcast
_CAN_ID_CLAIM = 0x18EEFF00 | _SA

_CMD_START = 0x3D
_CMD_STOP = 0x3E
_CMD_NEAREST_MINUTE = 0x3F
_CMD_RESET = 0x40

TimerCommand = Literal["start", "stop", "reset", "nearest-minute", "set"]


def _fast_packet_frames(can_id: int, payload: bytes, seq: int = 0) -> list[can.Message]:
    """Pack payload into NMEA 2000 Fast Packet CAN frames.

    seq is the 3-bit sequence counter (0-7) that must increment for each new
    fast packet transfer on the same (source, PGN) pair so receivers can
    distinguish retransmissions from new data.
    """
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


def _address_claim_name() -> bytes:
    """Build an 8-byte NMEA 2000 NAME for our device."""
    word0 = 0x00042 | (0x069 << 21)  # identity + manufacturer
    word1 = (120 << 8) | ((120 >> 1) << 17) | (4 << 28) | (1 << 31)  # func/class/marine/arb
    return struct.pack("<II", word0, word1)


class CANWriter:
    """Transmit NMEA 2000 timer commands on the CAN bus.

    Lifecycle: call start() once on boot to claim the address, then call
    send() for each command.  Call stop() on shutdown to release the bus.
    """

    def __init__(self, channel: str = "can0") -> None:
        self._channel = channel
        self._bus: can.BusABC | None = None
        # Per-PGN sequence counters (0-7, mod 8) — NMEA 2000 Fast Packet
        # requires the 3-bit sequence counter to increment for each new
        # transfer so receivers can distinguish new data from retransmissions.
        self._seq: dict[int, int] = {}

    async def start(self) -> None:
        """Open the CAN bus and claim our source address."""
        loop = asyncio.get_running_loop()
        try:
            bus: can.BusABC = await loop.run_in_executor(
                None, lambda: can.Bus(channel=self._channel, interface="socketcan")
            )
            self._bus = bus
        except Exception as exc:
            logger.warning(
                "CANWriter: failed to open {} — B&G control unavailable ({})", self._channel, exc
            )
            self._bus = None
            return

        claim_msg = can.Message(
            arbitration_id=_CAN_ID_CLAIM,
            data=_address_claim_name(),
            is_extended_id=True,
        )
        await loop.run_in_executor(None, self._bus.send, claim_msg)
        # NMEA 2000 requires 250 ms before using the claimed address.
        await asyncio.sleep(0.260)
        logger.info("CANWriter: address 0x{:02X} claimed on {}", _SA, self._channel)

    async def stop(self) -> None:
        if self._bus is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._bus.shutdown)
            self._bus = None

    def _next_seq(self, can_id: int) -> int:
        """Return and advance the 3-bit sequence counter for this CAN ID."""
        seq = self._seq.get(can_id, 0)
        self._seq[can_id] = (seq + 1) & 0x7
        return seq

    async def send(self, command: TimerCommand, minutes: int | None = None) -> None:
        """Transmit a timer command to the B&G instruments."""
        if self._bus is None:
            raise RuntimeError("CAN bus not available")

        if command == "set":
            if minutes is None:
                raise ValueError("minutes required for 'set'")
            # fmt: off
            payload = bytes([0x41, 0x9F, 0xFF, 0xFF, 0xFF, 0xFF,
                             0x07, 0x42, 0x00, 0x01, minutes, 0x00, 0x00, 0x00])
            # fmt: on
            frames = _fast_packet_frames(_CAN_ID_130845, payload, self._next_seq(_CAN_ID_130845))
        else:
            cmd_byte = {
                "start": _CMD_START,
                "stop": _CMD_STOP,
                "reset": _CMD_RESET,
                "nearest-minute": _CMD_NEAREST_MINUTE,
            }[command]
            payload = bytes(
                [0x41, 0x9F, 0xFF, 0xFF, 0x01, 0x17, cmd_byte, 0x00, 0x00, 0x00, 0x00, 0x00]
            )
            frames = _fast_packet_frames(_CAN_ID_130850, payload, self._next_seq(_CAN_ID_130850))

        loop = asyncio.get_running_loop()
        for i, frame in enumerate(frames):
            await loop.run_in_executor(None, self._bus.send, frame)
            if i < len(frames) - 1:
                await asyncio.sleep(0.002)

        logger.info("CANWriter: sent {} to B&G", command)
