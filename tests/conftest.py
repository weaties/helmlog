"""Shared pytest fixtures."""

from __future__ import annotations

import struct
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from helmlog.can_reader import CANFrame
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
from helmlog.storage import Storage, StorageConfig

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_TS = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_TS2 = datetime(2024, 6, 15, 12, 0, 1, tzinfo=UTC)
_TS3 = datetime(2024, 6, 15, 12, 0, 2, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Storage fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    """In-memory Storage instance, fully migrated."""
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# CANFrame fixtures
# ---------------------------------------------------------------------------


def _make_heading_frame() -> CANFrame:
    """PGN 127250 — 180° heading, no deviation/variation."""
    # heading = 180° → radians = π → raw = round(π / 0.0001) = 31416
    raw_hdg = round(3.14159265358979 / 0.0001)
    raw_dev = 0x7FFF  # not available
    raw_var = 0x7FFF  # not available
    data = struct.pack("<BHhhB", 0, raw_hdg, raw_dev, raw_var, 0)
    # Build arbitration_id for PGN 127250
    # PGN 127250 = 0x1F112; pdu_format = 0xF1 (≥240, PDU2)
    # arb_id = (priority << 26) | (data_page << 24) | (pdu_format << 16)
    #          | (pdu_specific << 8) | src
    # 127250 = 0x1_F1_12 → data_page=1, pf=0xF1, ps=0x12
    arb_id = (6 << 26) | (1 << 24) | (0xF1 << 16) | (0x12 << 8) | 0x05
    return CANFrame(arbitration_id=arb_id, data=data, timestamp=_TS.timestamp())


def _make_speed_frame() -> CANFrame:
    """PGN 128259 — 5 knots (≈ 2.572 m/s)."""
    # 5 kts = 2.5722 m/s → raw = round(2.5722 / 0.01) = 257
    raw_speed = 257
    data = struct.pack("<BHH B", 0, raw_speed, 0xFFFF, 0)
    # PGN 128259 = 0x1F503 → data_page=1, pf=0xF5, ps=0x03
    arb_id = (6 << 26) | (1 << 24) | (0xF5 << 16) | (0x03 << 8) | 0x05
    return CANFrame(arbitration_id=arb_id, data=data, timestamp=_TS.timestamp())


def _make_depth_frame() -> CANFrame:
    """PGN 128267 — 10 m depth."""
    raw_depth = 1000  # 1000 * 0.01 = 10.0 m
    raw_offset = 0  # 0 m offset
    data = struct.pack("<BIh", 0, raw_depth, raw_offset)
    # PGN 128267 = 0x1F50B → data_page=1, pf=0xF5, ps=0x0B
    arb_id = (6 << 26) | (1 << 24) | (0xF5 << 16) | (0x0B << 8) | 0x05
    return CANFrame(arbitration_id=arb_id, data=data, timestamp=_TS.timestamp())


def _make_position_frame() -> CANFrame:
    """PGN 129025 — 37.8044° N, 122.2712° W (San Francisco Bay)."""
    raw_lat = round(37.8044 / 1e-7)
    raw_lon = round(-122.2712 / 1e-7)
    data = struct.pack("<ii", raw_lat, raw_lon)
    # PGN 129025 = 0x1_F8_01 → data_page=1, pf=0xF8, ps=0x01
    arb_id = (6 << 26) | (1 << 24) | (0xF8 << 16) | (0x01 << 8) | 0x05
    return CANFrame(arbitration_id=arb_id, data=data, timestamp=_TS.timestamp())


def _make_cogsog_frame() -> CANFrame:
    """PGN 129026 — COG=45°, SOG=6 kts."""
    import math

    raw_cog = round(math.radians(45.0) / 0.0001)
    raw_sog = round(6.0 / 1.94384449 / 0.01)  # 6 kts → m/s → raw
    data = struct.pack("<BBHHBB", 0, 0, raw_cog, raw_sog, 0, 0)
    # PGN 129026 = 0x1_F8_02 → data_page=1, pf=0xF8, ps=0x02
    arb_id = (6 << 26) | (1 << 24) | (0xF8 << 16) | (0x02 << 8) | 0x05
    return CANFrame(arbitration_id=arb_id, data=data, timestamp=_TS.timestamp())


def _make_wind_frame() -> CANFrame:
    """PGN 130306 — 15 kts true wind at 30°."""
    import math

    raw_speed = round(15.0 / 1.94384449 / 0.01)  # 15 kts → m/s → raw
    raw_angle = round(math.radians(30.0) / 0.0001)
    data = struct.pack("<BHHB", 0, raw_speed, raw_angle, 0)  # reference=0 (true)
    # PGN 130306 = 0x1_FD_02 → data_page=1, pf=0xFD, ps=0x02
    arb_id = (6 << 26) | (1 << 24) | (0xFD << 16) | (0x02 << 8) | 0x05
    return CANFrame(arbitration_id=arb_id, data=data, timestamp=_TS.timestamp())


def _make_environmental_frame() -> CANFrame:
    """PGN 130310 — water temp 20°C (293.15 K)."""
    raw_temp = round(293.15 / 0.01)  # 293.15 K → raw
    data = struct.pack("<BH HBBBB", 0, raw_temp, 0xFFFF, 0, 0, 0, 0)
    # PGN 130310 = 0x1_FD_06 → data_page=1, pf=0xFD, ps=0x06
    arb_id = (6 << 26) | (1 << 24) | (0xFD << 16) | (0x06 << 8) | 0x05
    return CANFrame(arbitration_id=arb_id, data=data, timestamp=_TS.timestamp())


@pytest.fixture
def sample_can_frames() -> list[CANFrame]:
    """One CANFrame per supported PGN type."""
    return [
        _make_heading_frame(),
        _make_speed_frame(),
        _make_depth_frame(),
        _make_position_frame(),
        _make_cogsog_frame(),
        _make_wind_frame(),
        _make_environmental_frame(),
    ]


# ---------------------------------------------------------------------------
# Decoded record fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_records() -> list[PGNRecord]:
    """One decoded record per PGN type, with known values."""
    return [
        HeadingRecord(
            pgn=PGN_VESSEL_HEADING,
            source_addr=5,
            timestamp=_TS,
            heading_deg=180.0,
            deviation_deg=None,
            variation_deg=None,
        ),
        SpeedRecord(
            pgn=PGN_SPEED_THROUGH_WATER,
            source_addr=5,
            timestamp=_TS2,
            speed_kts=5.0,
        ),
        DepthRecord(
            pgn=PGN_WATER_DEPTH,
            source_addr=5,
            timestamp=_TS3,
            depth_m=10.0,
            offset_m=None,
        ),
        PositionRecord(
            pgn=PGN_POSITION_RAPID,
            source_addr=5,
            timestamp=_TS,
            latitude_deg=37.8044,
            longitude_deg=-122.2712,
        ),
        COGSOGRecord(
            pgn=PGN_COG_SOG_RAPID,
            source_addr=5,
            timestamp=_TS2,
            cog_deg=45.0,
            sog_kts=6.0,
        ),
        WindRecord(
            pgn=PGN_WIND_DATA,
            source_addr=5,
            timestamp=_TS3,
            wind_speed_kts=15.0,
            wind_angle_deg=30.0,
            reference=0,
        ),
        EnvironmentalRecord(
            pgn=PGN_ENVIRONMENTAL,
            source_addr=5,
            timestamp=_TS,
            water_temp_c=20.0,
        ),
    ]
