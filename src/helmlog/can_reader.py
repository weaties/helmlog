"""CAN bus interface — hardware isolation layer.

This is the ONLY module that touches the physical CAN bus.
All other modules receive decoded data structures, not raw frames.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# python-can is imported lazily so the rest of the codebase can be tested
# without a CAN interface present.
try:
    import can

    _CAN_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CAN_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CANReaderConfig:
    """Configuration for the CAN bus reader, loaded from environment."""

    interface: str = field(default_factory=lambda: os.environ.get("CAN_INTERFACE", "can0"))
    bitrate: int = field(default_factory=lambda: int(os.environ.get("CAN_BITRATE", "250000")))


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CANFrame:
    """A single raw CAN frame as received from the bus."""

    arbitration_id: int
    data: bytes
    timestamp: float  # Unix timestamp (seconds, from python-can Message.timestamp)


# ---------------------------------------------------------------------------
# PGN extraction
# ---------------------------------------------------------------------------


def extract_pgn(arbitration_id: int) -> int:
    """Extract the NMEA 2000 PGN from a 29-bit J1939 CAN arbitration ID.

    NMEA 2000 uses the J1939 29-bit extended CAN ID format:
        bits 28-26: priority (3 bits)
        bit  25:    reserved
        bit  24:    data page
        bits 23-16: PDU format (PF)
        bits 15-8:  PDU specific (PS)
        bits  7-0:  source address

    For PDU2 (PF >= 240, broadcast messages):
        PGN = (data_page << 16) | (PF << 8) | PS

    For PDU1 (PF < 240, addressed messages):
        PGN = (data_page << 16) | (PF << 8)
        (PS is the destination address, not part of the PGN)
    """
    data_page = (arbitration_id >> 24) & 0x1
    pdu_format = (arbitration_id >> 16) & 0xFF
    pdu_specific = (arbitration_id >> 8) & 0xFF

    if pdu_format >= 240:  # PDU2 — broadcast
        return (data_page << 16) | (pdu_format << 8) | pdu_specific
    else:  # PDU1 — peer-to-peer
        return (data_page << 16) | (pdu_format << 8)


# ---------------------------------------------------------------------------
# CAN reader
# ---------------------------------------------------------------------------


class CANReader:
    """Async iterator that yields CANFrames from the CAN bus.

    Usage::

        reader = CANReader(config)
        async for frame in reader:
            ...
    """

    def __init__(self, config: CANReaderConfig) -> None:
        self._config = config
        self._bus: can.BusABC | None = None

    def _open_bus(self) -> None:
        if not _CAN_AVAILABLE:  # pragma: no cover
            raise RuntimeError("python-can is not installed")
        self._bus = can.interface.Bus(
            channel=self._config.interface,
            bustype="socketcan",
            bitrate=self._config.bitrate,
        )
        logger.info(
            "CAN bus opened: interface={} bitrate={}",
            self._config.interface,
            self._config.bitrate,
        )

    def close(self) -> None:
        """Shut down the CAN bus connection."""
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None
            logger.info("CAN bus closed")

    async def __aiter__(self) -> AsyncIterator[CANFrame]:
        """Yield CANFrames from the bus until cancelled or interrupted.

        Uses asyncio.to_thread for the blocking recv() so the event loop
        remains responsive and task cancellation (SIGTERM/SIGINT) works cleanly.
        """
        self._open_bus()
        assert self._bus is not None
        bus = self._bus
        try:
            while True:
                msg: can.Message | None = await asyncio.to_thread(bus.recv, 1.0)
                if msg is None:
                    continue  # timeout — loop and try again
                if not msg.is_extended_id:
                    logger.debug("Skipping non-extended CAN frame id={:#x}", msg.arbitration_id)
                    continue
                yield CANFrame(
                    arbitration_id=msg.arbitration_id,
                    data=bytes(msg.data),
                    timestamp=msg.timestamp,
                )
        except asyncio.CancelledError:
            raise  # let cancellation propagate for clean shutdown
        except Exception as exc:
            logger.warning("CAN read error: {}", exc)
            raise
        finally:
            self.close()
