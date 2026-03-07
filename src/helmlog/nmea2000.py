"""NMEA 2000 PGN decoding — pure Python, no hardware required.

Supported PGNs:
    127250 — Vessel Heading
    128259 — Speed Through Water
    128267 — Water Depth
    129025 — Position Rapid Update
    129026 — COG & SOG Rapid Update
    130306 — Wind Data
    130310 — Environmental Parameters

All decoders use struct.unpack with little-endian byte order per the NMEA 2000 spec.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from loguru import logger

# ---------------------------------------------------------------------------
# PGN constants
# ---------------------------------------------------------------------------

PGN_VESSEL_HEADING: Final[int] = 127250
PGN_SPEED_THROUGH_WATER: Final[int] = 128259
PGN_WATER_DEPTH: Final[int] = 128267
PGN_POSITION_RAPID: Final[int] = 129025
PGN_COG_SOG_RAPID: Final[int] = 129026
PGN_WIND_DATA: Final[int] = 130306
PGN_ENVIRONMENTAL: Final[int] = 130310

SUPPORTED_PGNS: Final[frozenset[int]] = frozenset(
    {
        PGN_VESSEL_HEADING,
        PGN_SPEED_THROUGH_WATER,
        PGN_WATER_DEPTH,
        PGN_POSITION_RAPID,
        PGN_COG_SOG_RAPID,
        PGN_WIND_DATA,
        PGN_ENVIRONMENTAL,
    }
)

# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadingRecord:
    """PGN 127250 — Vessel Heading."""

    pgn: int
    source_addr: int
    timestamp: datetime
    heading_deg: float  # degrees true (converted from radians)
    deviation_deg: float | None  # magnetic deviation, degrees
    variation_deg: float | None  # magnetic variation, degrees


@dataclass(frozen=True)
class SpeedRecord:
    """PGN 128259 — Speed Through Water."""

    pgn: int
    source_addr: int
    timestamp: datetime
    speed_kts: float  # knots (converted from m/s)


@dataclass(frozen=True)
class DepthRecord:
    """PGN 128267 — Water Depth."""

    pgn: int
    source_addr: int
    timestamp: datetime
    depth_m: float  # metres below transducer
    offset_m: float | None  # transducer offset (positive = above keel)


@dataclass(frozen=True)
class PositionRecord:
    """PGN 129025 — Position Rapid Update."""

    pgn: int
    source_addr: int
    timestamp: datetime
    latitude_deg: float  # degrees, positive North
    longitude_deg: float  # degrees, positive East


@dataclass(frozen=True)
class COGSOGRecord:
    """PGN 129026 — COG & SOG Rapid Update."""

    pgn: int
    source_addr: int
    timestamp: datetime
    cog_deg: float  # course over ground, degrees true
    sog_kts: float  # speed over ground, knots


@dataclass(frozen=True)
class WindRecord:
    """PGN 130306 — Wind Data."""

    pgn: int
    source_addr: int
    timestamp: datetime
    wind_speed_kts: float  # knots
    wind_angle_deg: float  # degrees (apparent or true per reference field)
    reference: int  # 0=true, 1=magnetic, 2=apparent, 3=boat (see spec)


@dataclass(frozen=True)
class EnvironmentalRecord:
    """PGN 130310 — Environmental Parameters."""

    pgn: int
    source_addr: int
    timestamp: datetime
    water_temp_c: float  # Celsius (converted from Kelvin)


# Union type for all PGN record types
PGNRecord = (
    HeadingRecord
    | SpeedRecord
    | DepthRecord
    | PositionRecord
    | COGSOGRecord
    | WindRecord
    | EnvironmentalRecord
)

# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------

_RAD_TO_DEG: Final[float] = 180.0 / math.pi
_MPS_TO_KTS: Final[float] = 1.94384449  # 1 m/s = 1.94384... knots
_KELVIN_OFFSET: Final[float] = 273.15


def _rad_to_deg(radians: float) -> float:
    return radians * _RAD_TO_DEG


def _mps_to_kts(mps: float) -> float:
    return mps * _MPS_TO_KTS


# ---------------------------------------------------------------------------
# Individual decoders (private)
# ---------------------------------------------------------------------------

# NMEA 2000 "not available" sentinel values
_UINT16_NA: Final[int] = 0xFFFF
_UINT32_NA: Final[int] = 0xFFFFFFFF
_INT16_NA: Final[int] = -32768  # 0x8000 as signed int16
_INT32_NA: Final[int] = -2147483648  # 0x80000000 as signed int32


def _decode_127250(data: bytes, source: int, ts: datetime) -> HeadingRecord | None:
    """PGN 127250 — Vessel Heading (8 bytes).

    Byte layout (little-endian):
        0:     SID (1 byte)
        1-2:   Heading (uint16, 0.0001 rad/bit)
        3-4:   Deviation (int16, 0.0001 rad/bit; 0x7FFF = not available)
        5-6:   Variation (int16, 0.0001 rad/bit; 0x7FFF = not available)
        7:     Reference (bits 0-1: 0=true, 1=magnetic)
    """
    if len(data) < 8:
        logger.warning("PGN 127250: short data ({} bytes)", len(data))
        return None

    _sid, raw_hdg, raw_dev, raw_var, _ref = struct.unpack_from("<BHhhB", data, 0)

    if raw_hdg == _UINT16_NA:
        logger.debug("PGN 127250: heading not available")
        return None

    heading_deg = _rad_to_deg(raw_hdg * 0.0001)
    deviation_deg = _rad_to_deg(raw_dev * 0.0001) if raw_dev != 0x7FFF else None
    variation_deg = _rad_to_deg(raw_var * 0.0001) if raw_var != 0x7FFF else None

    return HeadingRecord(
        pgn=PGN_VESSEL_HEADING,
        source_addr=source,
        timestamp=ts,
        heading_deg=heading_deg,
        deviation_deg=deviation_deg,
        variation_deg=variation_deg,
    )


def _decode_128259(data: bytes, source: int, ts: datetime) -> SpeedRecord | None:
    """PGN 128259 — Speed Through Water (6 bytes).

    Byte layout:
        0:     SID
        1-2:   Speed (uint16, 0.01 m/s per bit)
        3-4:   Speed Through Water (uint16, 0.01 m/s, transducer)
        5:     Speed Type (bits)
    """
    if len(data) < 6:
        logger.warning("PGN 128259: short data ({} bytes)", len(data))
        return None

    _sid, raw_speed = struct.unpack_from("<BH", data, 0)

    if raw_speed == _UINT16_NA:
        logger.debug("PGN 128259: speed not available")
        return None

    speed_mps = raw_speed * 0.01
    return SpeedRecord(
        pgn=PGN_SPEED_THROUGH_WATER,
        source_addr=source,
        timestamp=ts,
        speed_kts=_mps_to_kts(speed_mps),
    )


def _decode_128267(data: bytes, source: int, ts: datetime) -> DepthRecord | None:
    """PGN 128267 — Water Depth (7 bytes).

    Byte layout:
        0:     SID
        1-4:   Depth (uint32, 0.01 m per bit)
        5-6:   Offset (int16, 0.001 m per bit; positive = keel above transducer)
    """
    if len(data) < 7:
        logger.warning("PGN 128267: short data ({} bytes)", len(data))
        return None

    _sid, raw_depth, raw_offset = struct.unpack_from("<BIh", data, 0)

    if raw_depth == _UINT32_NA:
        logger.debug("PGN 128267: depth not available")
        return None

    depth_m = raw_depth * 0.01
    offset_m = raw_offset * 0.001 if raw_offset != _INT16_NA else None

    return DepthRecord(
        pgn=PGN_WATER_DEPTH,
        source_addr=source,
        timestamp=ts,
        depth_m=depth_m,
        offset_m=offset_m,
    )


def _decode_129025(data: bytes, source: int, ts: datetime) -> PositionRecord | None:
    """PGN 129025 — Position Rapid Update (8 bytes).

    Byte layout:
        0-3:   Latitude (int32, 1e-7 degrees per bit)
        4-7:   Longitude (int32, 1e-7 degrees per bit)
    """
    if len(data) < 8:
        logger.warning("PGN 129025: short data ({} bytes)", len(data))
        return None

    raw_lat, raw_lon = struct.unpack_from("<ii", data, 0)

    if raw_lat == _INT32_NA or raw_lon == _INT32_NA:
        logger.debug("PGN 129025: position not available")
        return None

    return PositionRecord(
        pgn=PGN_POSITION_RAPID,
        source_addr=source,
        timestamp=ts,
        latitude_deg=raw_lat * 1e-7,
        longitude_deg=raw_lon * 1e-7,
    )


def _decode_129026(data: bytes, source: int, ts: datetime) -> COGSOGRecord | None:
    """PGN 129026 — COG & SOG Rapid Update (8 bytes).

    Byte layout:
        0:     SID
        1:     COG Reference (bits 0-1: 0=true, 1=magnetic)
        2-3:   COG (uint16, 0.0001 rad per bit)
        4-5:   SOG (uint16, 0.01 m/s per bit)
        6-7:   Reserved
    """
    if len(data) < 8:
        logger.warning("PGN 129026: short data ({} bytes)", len(data))
        return None

    _sid, _ref, raw_cog, raw_sog = struct.unpack_from("<BBHH", data, 0)

    if raw_cog == _UINT16_NA or raw_sog == _UINT16_NA:
        logger.debug("PGN 129026: COG/SOG not available")
        return None

    return COGSOGRecord(
        pgn=PGN_COG_SOG_RAPID,
        source_addr=source,
        timestamp=ts,
        cog_deg=_rad_to_deg(raw_cog * 0.0001),
        sog_kts=_mps_to_kts(raw_sog * 0.01),
    )


def _decode_130306(data: bytes, source: int, ts: datetime) -> WindRecord | None:
    """PGN 130306 — Wind Data (6 bytes).

    Byte layout:
        0:     SID
        1-2:   Wind Speed (uint16, 0.01 m/s per bit)
        3-4:   Wind Angle (uint16, 0.0001 rad per bit)
        5:     Reference (bits 0-2: 0=true, 2=apparent, 3=boat)
    """
    if len(data) < 6:
        logger.warning("PGN 130306: short data ({} bytes)", len(data))
        return None

    _sid, raw_speed, raw_angle, raw_ref = struct.unpack_from("<BHHB", data, 0)

    if raw_speed == _UINT16_NA or raw_angle == _UINT16_NA:
        logger.debug("PGN 130306: wind data not available")
        return None

    reference = raw_ref & 0x07  # lower 3 bits

    return WindRecord(
        pgn=PGN_WIND_DATA,
        source_addr=source,
        timestamp=ts,
        wind_speed_kts=_mps_to_kts(raw_speed * 0.01),
        wind_angle_deg=_rad_to_deg(raw_angle * 0.0001),
        reference=reference,
    )


def _decode_130310(data: bytes, source: int, ts: datetime) -> EnvironmentalRecord | None:
    """PGN 130310 — Environmental Parameters (7 bytes).

    Byte layout:
        0:     SID
        1-2:   Water Temperature (uint16, 0.01 K per bit)
        3-4:   Atmospheric Pressure (uint16, 0.1 hPa per bit) — ignored here
        5-6:   Reserved
    """
    if len(data) < 7:
        logger.warning("PGN 130310: short data ({} bytes)", len(data))
        return None

    _sid, raw_temp = struct.unpack_from("<BH", data, 0)

    if raw_temp == _UINT16_NA:
        logger.debug("PGN 130310: water temperature not available")
        return None

    water_temp_k = raw_temp * 0.01
    return EnvironmentalRecord(
        pgn=PGN_ENVIRONMENTAL,
        source_addr=source,
        timestamp=ts,
        water_temp_c=water_temp_k - _KELVIN_OFFSET,
    )


# ---------------------------------------------------------------------------
# Dispatch table & public API
# ---------------------------------------------------------------------------

_DECODERS = {
    PGN_VESSEL_HEADING: _decode_127250,
    PGN_SPEED_THROUGH_WATER: _decode_128259,
    PGN_WATER_DEPTH: _decode_128267,
    PGN_POSITION_RAPID: _decode_129025,
    PGN_COG_SOG_RAPID: _decode_129026,
    PGN_WIND_DATA: _decode_130306,
    PGN_ENVIRONMENTAL: _decode_130310,
}


def decode(
    pgn: int,
    data: bytes,
    source: int,
    timestamp: float,
) -> PGNRecord | None:
    """Decode a raw CAN payload into a typed PGN record.

    Args:
        pgn:       The NMEA 2000 PGN number.
        data:      Raw payload bytes from the CAN frame.
        source:    Source address byte from the CAN arbitration ID.
        timestamp: Unix timestamp (seconds) of the CAN frame.

    Returns:
        A typed PGNRecord dataclass, or None if the PGN is unsupported or
        the data cannot be decoded.
    """
    decoder = _DECODERS.get(pgn)
    if decoder is None:
        return None

    ts = datetime.fromtimestamp(timestamp, tz=UTC)
    try:
        return decoder(data, source, ts)
    except struct.error as exc:
        logger.warning("PGN {}: struct decode error: {}", pgn, exc)
        return None
