"""Instrument timer-PGN audit (#789, docs/specs/pgn-audit.md).

A read-only sniffer that confirms whether the boat's B&G/Simrad instruments
emit the race-timer PGNs (130845 Set, 130850 Start/Stop) over NMEA 2000, so
hardware support can be validated from the web UI without an SSH/CLI session.

Hardware boundary: this module imports ``can`` and is imported only by
``main.py`` (like ``sk_reader``/``can_reader``). The web layer never touches it
— it reads observations from SQLite via ``Storage.get_pgn_audit_summary``.

The sniffer NEVER transmits a frame. Control of the timer (writing PGNs back to
the bus) is a separate, later phase.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from helmlog.nmea2000 import (
    FAST_PACKET_PGNS,
    PGN_SIMRAD_SET_TIMER,
    PGN_SIMRAD_START_STOP,
    FastPacketBuffer,
    SimradTimerRecord,
    decode,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage


@dataclass(frozen=True)
class PgnAuditConfig:
    """Audit sniffer config, sourced from the environment."""

    enabled: bool = False
    channel: str = "can0"

    @classmethod
    def from_env(cls) -> PgnAuditConfig:
        return cls(
            enabled=os.environ.get("PGN_AUDIT_ENABLED", "false").lower() == "true",
            channel=os.environ.get("PGN_AUDIT_CHANNEL", "can0"),
        )


def handle_frame(
    buf: FastPacketBuffer,
    pgn: int,
    source_addr: int,
    data: bytes,
    frame_ts: float,
) -> dict[str, Any] | None:
    """Reassemble + decode one CAN frame into a record_pgn_observation kwargs
    dict, or None if the frame isn't a target PGN or the payload is incomplete.

    Pure (no I/O) so the decode/observe path is unit-tested without a bus.
    Undecoded-but-complete target payloads are still returned (action=None) so
    the raw bytes get logged for layout analysis.
    """
    if pgn not in FAST_PACKET_PGNS:
        return None
    payload = buf.feed(pgn, source_addr, data)
    if payload is None:
        return None
    record = decode(pgn, payload, source_addr, frame_ts)
    action: str | None = None
    minutes: int | None = None
    if isinstance(record, SimradTimerRecord):
        action, minutes = record.action, record.minutes
    return {
        "observed_at": datetime.fromtimestamp(frame_ts, tz=UTC),
        "pgn": pgn,
        "source_addr": source_addr,
        "raw_hex": payload.hex(" "),
        "action": action,
        "minutes": minutes,
    }


def verdict_from_summary(summary: dict[str, Any]) -> dict[str, str]:
    """Compute {level, message} from a Storage.get_pgn_audit_summary() dict.

    level ∈ PASS / PARTIAL / FAIL — see the decision table in
    docs/specs/pgn-audit.md.
    """
    pgns = summary.get("pgns", {})
    set_st = pgns.get(PGN_SIMRAD_SET_TIMER) or pgns.get(str(PGN_SIMRAD_SET_TIMER)) or {}
    ss_st = pgns.get(PGN_SIMRAD_START_STOP) or pgns.get(str(PGN_SIMRAD_START_STOP)) or {}
    set_decoded = int(set_st.get("decoded", 0))
    ss_decoded = int(ss_st.get("decoded", 0))
    any_frames = int(set_st.get("frames", 0)) + int(ss_st.get("frames", 0))

    if set_decoded and ss_decoded:
        return {
            "level": "PASS",
            "message": "Both timer PGNs seen and decoded — the feature is viable on this boat.",
        }
    if set_decoded or ss_decoded:
        missing = "130850 Start/Stop" if set_decoded else "130845 Set Timer"
        return {
            "level": "PARTIAL",
            "message": f"Decoded some timer traffic but never decoded {missing}.",
        }
    if any_frames:
        return {
            "level": "PARTIAL",
            "message": (
                "Target PGNs appeared but none decoded — the instruments likely use a "
                "different byte layout. The raw samples have the bytes to adapt the decoder."
            ),
        }
    return {
        "level": "FAIL",
        "message": (
            "No 130845/130850 frames seen. Either the display has no N2K-controllable "
            "timer, or this is the wrong CAN interface."
        ),
    }


async def run_pgn_audit(storage: Storage, config: PgnAuditConfig) -> None:
    """Read-only sniffer loop: record target timer PGNs to the pgn_audit table.

    Opens the CAN bus for reading only. If it can't be opened, logs a warning
    and returns — the audit is simply unavailable and the rest of the logger is
    unaffected. Never transmits.
    """
    import can  # imported here to keep the hardware dep at the edge

    loop = asyncio.get_running_loop()
    try:
        bus: can.BusABC = await loop.run_in_executor(
            None, lambda: can.Bus(channel=config.channel, interface="socketcan")
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash the logger
        logger.warning(
            "PGN audit: cannot open {} for reading — audit unavailable ({})",
            config.channel,
            exc,
        )
        return

    from helmlog.can_reader import extract_pgn

    buf = FastPacketBuffer()
    logger.info(
        "PGN audit: listening read-only on {} for {}",
        config.channel,
        sorted(FAST_PACKET_PGNS),
    )
    try:
        while True:
            msg = await loop.run_in_executor(None, bus.recv, 1.0)
            if msg is None or not msg.is_extended_id:
                continue
            pgn = extract_pgn(msg.arbitration_id)
            if pgn not in FAST_PACKET_PGNS:
                continue
            sa = msg.arbitration_id & 0xFF
            obs = handle_frame(buf, pgn, sa, bytes(msg.data), msg.timestamp or 0.0)
            if obs is not None:
                await storage.record_pgn_observation(**obs)
                logger.info(
                    "PGN audit: {} SA=0x{:02X} action={} minutes={}",
                    obs["pgn"],
                    sa,
                    obs["action"],
                    obs["minutes"],
                )
    except asyncio.CancelledError:
        logger.info("PGN audit: stopping")
        raise
    finally:
        await loop.run_in_executor(None, bus.shutdown)
