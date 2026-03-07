"""Signal K WebSocket reader — consumes SK delta feed, emits NMEARecord types.

Replaces can_reader.py when Signal K Server owns the CAN bus.
Emits the same record types as nmea2000.py so storage and export are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger
from websockets.asyncio.client import connect as _ws_connect

from helmlog.nmea2000 import (
    PGN_COG_SOG_RAPID,
    PGN_ENVIRONMENTAL,
    PGN_POSITION_RAPID,
    PGN_SPEED_THROUGH_WATER,
    PGN_VESSEL_HEADING,
    PGN_WATER_DEPTH,
    PGN_WIND_DATA,
    COGSOGRecord,
    DepthRecord,
    EnvironmentalRecord,
    HeadingRecord,
    PGNRecord,
    PositionRecord,
    SpeedRecord,
    WindRecord,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

_RAD_TO_DEG: float = 180.0 / math.pi
_MPS_TO_KTS: float = 1.94384449
_KELVIN_OFFSET: float = 273.15
SK_SOURCE_ADDR: int = 0  # no CAN source address for SK-originated records


@dataclass
class SKReaderConfig:
    """Configuration for the Signal K WebSocket reader.

    Values fall back to environment variables SK_HOST / SK_PORT if not set.
    """

    host: str = field(default_factory=lambda: os.environ.get("SK_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.environ.get("SK_PORT", "3000")))
    reconnect_delay_s: float = 5.0


# ---------------------------------------------------------------------------
# Simple single-value record constructors
# ---------------------------------------------------------------------------


def _mk_heading(v: float, ts: datetime) -> HeadingRecord:
    return HeadingRecord(PGN_VESSEL_HEADING, SK_SOURCE_ADDR, ts, v * _RAD_TO_DEG, None, None)


def _mk_speed(v: float, ts: datetime) -> SpeedRecord:
    return SpeedRecord(PGN_SPEED_THROUGH_WATER, SK_SOURCE_ADDR, ts, v * _MPS_TO_KTS)


def _mk_depth(v: float, ts: datetime) -> DepthRecord:
    return DepthRecord(PGN_WATER_DEPTH, SK_SOURCE_ADDR, ts, v, None)


def _mk_env(v: float, ts: datetime) -> EnvironmentalRecord:
    return EnvironmentalRecord(PGN_ENVIRONMENTAL, SK_SOURCE_ADDR, ts, v - _KELVIN_OFFSET)


# ---------------------------------------------------------------------------
# Paired record builders (buffer until both values arrive)
# ---------------------------------------------------------------------------


def _try_cogsog(buf: dict[str, float], ts: datetime) -> COGSOGRecord | None:
    cog = buf.get("navigation.courseOverGroundTrue")
    sog = buf.get("navigation.speedOverGround")
    if cog is None or sog is None:
        return None
    return COGSOGRecord(PGN_COG_SOG_RAPID, SK_SOURCE_ADDR, ts, cog * _RAD_TO_DEG, sog * _MPS_TO_KTS)


def _try_true_wind(buf: dict[str, float], ts: datetime) -> WindRecord | None:
    spd = buf.get("environment.wind.speedTrue")
    if spd is None:
        return None
    # Prefer boat-referenced angle (TWA, reference=0); try all Signal K variants
    for ang_key in (
        "environment.wind.angleTrue",
        "environment.wind.angleTrueWater",
        "environment.wind.angleTrueGround",
    ):
        ang = buf.get(ang_key)
        if ang is not None:
            return WindRecord(
                PGN_WIND_DATA, SK_SOURCE_ADDR, ts, spd * _MPS_TO_KTS, ang * _RAD_TO_DEG, 0
            )
    # Fall back to north-referenced direction (TWD, reference=4) — common on B&G
    direction = buf.get("environment.wind.directionTrue")
    if direction is not None:
        return WindRecord(
            PGN_WIND_DATA, SK_SOURCE_ADDR, ts, spd * _MPS_TO_KTS, direction * _RAD_TO_DEG, 4
        )
    return None


def _try_app_wind(buf: dict[str, float], ts: datetime) -> WindRecord | None:
    spd = buf.get("environment.wind.speedApparent")
    ang = buf.get("environment.wind.angleApparent")
    if spd is None or ang is None:
        return None
    return WindRecord(PGN_WIND_DATA, SK_SOURCE_ADDR, ts, spd * _MPS_TO_KTS, ang * _RAD_TO_DEG, 2)


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

_SIMPLE: dict[str, Callable[[float, datetime], PGNRecord]] = {
    "navigation.headingTrue": _mk_heading,
    "navigation.speedThroughWater": _mk_speed,
    "environment.depth.belowKeel": _mk_depth,
    "environment.water.temperature": _mk_env,
}

_PAIR: dict[str, Callable[[dict[str, float], datetime], PGNRecord | None]] = {
    "navigation.courseOverGroundTrue": _try_cogsog,
    "navigation.speedOverGround": _try_cogsog,
    "environment.wind.speedTrue": _try_true_wind,
    "environment.wind.angleTrue": _try_true_wind,
    "environment.wind.angleTrueWater": _try_true_wind,
    "environment.wind.angleTrueGround": _try_true_wind,
    "environment.wind.directionTrue": _try_true_wind,
    "environment.wind.speedApparent": _try_app_wind,
    "environment.wind.angleApparent": _try_app_wind,
}


# ---------------------------------------------------------------------------
# Delta parser (pure function — testable without a WebSocket)
# ---------------------------------------------------------------------------


def process_delta(raw: str, buf: dict[str, float]) -> list[PGNRecord]:
    """Parse a Signal K delta message; return any records it produces.

    Updates *buf* in-place for multi-field records (COG+SOG, wind speed+angle).
    Unknown paths are silently ignored at DEBUG level.
    Malformed numeric values are logged at WARNING and skipped.
    """
    try:
        delta: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("SK: malformed JSON: {}", exc)
        return []

    records: list[PGNRecord] = []
    for update in delta.get("updates", []):
        ts_str: str = update.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = datetime.now(UTC)

        for entry in update.get("values", []):
            path: str = entry.get("path", "")
            value: Any = entry.get("value")
            if value is None:
                continue

            if path == "navigation.position":
                try:
                    records.append(
                        PositionRecord(
                            PGN_POSITION_RAPID,
                            SK_SOURCE_ADDR,
                            ts,
                            float(value["latitude"]),
                            float(value["longitude"]),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("SK: bad position value {!r}: {}", value, exc)
                continue

            if simple_fn := _SIMPLE.get(path):
                try:
                    records.append(simple_fn(float(value), ts))
                except (TypeError, ValueError) as exc:
                    logger.warning("SK: non-numeric value for {!r}: {}", path, exc)
                continue

            if pair_fn := _PAIR.get(path):
                try:
                    buf[path] = float(value)
                    rec = pair_fn(buf, ts)
                    if rec is not None:
                        records.append(rec)
                except (TypeError, ValueError) as exc:
                    logger.warning("SK: non-numeric value for {!r}: {}", path, exc)
                continue

            logger.debug("SK: unknown path {!r} — ignoring", path)

    return records


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class SKReader:
    """Async iterable that yields NMEARecord instances from a Signal K WebSocket.

    Reconnects automatically on disconnect using exponential backoff capped at
    ``config.reconnect_delay_s``.  Propagates ``asyncio.CancelledError`` cleanly.

    Usage::

        async for record in SKReader(SKReaderConfig()):
            await storage.write(record)
    """

    def __init__(self, config: SKReaderConfig) -> None:
        self._config = config
        self._buf: dict[str, float] = {}

    def __aiter__(self) -> AsyncIterator[PGNRecord]:
        return self._stream()

    async def _stream(self) -> AsyncGenerator[PGNRecord, None]:
        config = self._config
        uri = f"ws://{config.host}:{config.port}/signalk/v1/stream?subscribe=all"
        delay = 1.0
        while True:
            try:
                logger.info("SK: connecting to {}", uri)
                async with _ws_connect(uri) as ws:
                    delay = 1.0
                    logger.info("SK: connected")
                    async for raw in ws:
                        for record in process_delta(str(raw), self._buf):
                            yield record
            except asyncio.CancelledError:
                logger.info("SK: cancelled — stopping")
                raise
            except Exception as exc:
                logger.warning("SK: connection error ({}). Reconnecting in {:.1f}s", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, config.reconnect_delay_s)
