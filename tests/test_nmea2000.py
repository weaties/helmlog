"""Tests for nmea2000.py — PGN extraction and decoding."""

from __future__ import annotations

import math
import struct
from datetime import UTC, datetime

from helmlog.can_reader import extract_pgn
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
    PositionRecord,
    SpeedRecord,
    WindRecord,
    decode,
)

_TS = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_UNIX_TS = _TS.timestamp()


# ---------------------------------------------------------------------------
# extract_pgn
# ---------------------------------------------------------------------------


def _arb_id(data_page: int, pf: int, ps: int, src: int = 5, priority: int = 6) -> int:
    return (priority << 26) | (data_page << 24) | (pf << 16) | (ps << 8) | src


class TestExtractPGN:
    def test_pgn_127250(self) -> None:
        # 127250 = 0x1_F1_12 → dp=1, pf=0xF1(241≥240 PDU2), ps=0x12
        arb_id = _arb_id(1, 0xF1, 0x12)
        assert extract_pgn(arb_id) == PGN_VESSEL_HEADING

    def test_pgn_128259(self) -> None:
        # 128259 = 0x1F503 → dp=1, pf=0xF5(245≥240), ps=0x03
        arb_id = _arb_id(1, 0xF5, 0x03)
        assert extract_pgn(arb_id) == PGN_SPEED_THROUGH_WATER

    def test_pgn_128267(self) -> None:
        # 128267 = 0x1F50B → dp=1, pf=0xF5, ps=0x0B
        arb_id = _arb_id(1, 0xF5, 0x0B)
        assert extract_pgn(arb_id) == PGN_WATER_DEPTH

    def test_pgn_129025(self) -> None:
        # 129025 = 0x1_F8_01 → dp=1, pf=0xF8(248≥240), ps=0x01
        arb_id = _arb_id(1, 0xF8, 0x01)
        assert extract_pgn(arb_id) == PGN_POSITION_RAPID

    def test_pgn_129026(self) -> None:
        # 129026 = 0x1_F8_02 → dp=1, pf=0xF8, ps=0x02
        arb_id = _arb_id(1, 0xF8, 0x02)
        assert extract_pgn(arb_id) == PGN_COG_SOG_RAPID

    def test_pgn_130306(self) -> None:
        # 130306 = 0x1_FD_02 → dp=1, pf=0xFD(253≥240), ps=0x02
        arb_id = _arb_id(1, 0xFD, 0x02)
        assert extract_pgn(arb_id) == PGN_WIND_DATA

    def test_pgn_130310(self) -> None:
        # 130310 = 0x1_FD_06 → dp=1, pf=0xFD, ps=0x06
        arb_id = _arb_id(1, 0xFD, 0x06)
        assert extract_pgn(arb_id) == PGN_ENVIRONMENTAL

    def test_pdu1_addressed_ignores_ps(self) -> None:
        # PDU1: pf < 240 → PS is destination, not part of PGN
        # Two IDs with same pf but different ps must give same PGN
        arb_id_a = _arb_id(0, 0xEA, 0x00)
        arb_id_b = _arb_id(0, 0xEA, 0xFF)
        assert extract_pgn(arb_id_a) == extract_pgn(arb_id_b)

    def test_data_page_zero(self) -> None:
        # data_page=0, pf=0xF1, ps=0x12 → PGN = 0x00_F1_12 = 61714
        arb_id = _arb_id(0, 0xF1, 0x12)
        assert extract_pgn(arb_id) == (0xF1 << 8) | 0x12


# ---------------------------------------------------------------------------
# decode dispatch
# ---------------------------------------------------------------------------


class TestDecodeDispatch:
    def test_unknown_pgn_returns_none(self) -> None:
        result = decode(pgn=99999, data=b"\x00" * 8, source=5, timestamp=_UNIX_TS)
        assert result is None

    def test_known_pgn_with_short_data_returns_none(self) -> None:
        result = decode(pgn=PGN_VESSEL_HEADING, data=b"\x00" * 2, source=5, timestamp=_UNIX_TS)
        assert result is None


# ---------------------------------------------------------------------------
# Individual decoders
# ---------------------------------------------------------------------------


class TestDecode127250:
    def _make_data(
        self,
        heading_rad: float,
        dev_raw: int = 0x7FFF,
        var_raw: int = 0x7FFF,
    ) -> bytes:
        raw_hdg = round(heading_rad / 0.0001)
        return struct.pack("<BHhhB", 0, raw_hdg, dev_raw, var_raw, 0)

    def test_heading_180_degrees(self) -> None:
        data = self._make_data(math.pi)
        result = decode(PGN_VESSEL_HEADING, data, 5, _UNIX_TS)
        assert isinstance(result, HeadingRecord)
        assert abs(result.heading_deg - 180.0) < 0.01

    def test_heading_zero_degrees(self) -> None:
        data = self._make_data(0.0)
        result = decode(PGN_VESSEL_HEADING, data, 5, _UNIX_TS)
        assert isinstance(result, HeadingRecord)
        assert abs(result.heading_deg) < 0.01

    def test_no_deviation_variation(self) -> None:
        data = self._make_data(math.pi)
        result = decode(PGN_VESSEL_HEADING, data, 5, _UNIX_TS)
        assert isinstance(result, HeadingRecord)
        assert result.deviation_deg is None
        assert result.variation_deg is None

    def test_with_deviation(self) -> None:
        # deviation = 5° → raw = round(5° in rad / 0.0001)
        dev_rad = math.radians(5.0)
        dev_raw = round(dev_rad / 0.0001)
        data = self._make_data(math.pi, dev_raw=dev_raw)
        result = decode(PGN_VESSEL_HEADING, data, 5, _UNIX_TS)
        assert isinstance(result, HeadingRecord)
        assert result.deviation_deg is not None
        assert abs(result.deviation_deg - 5.0) < 0.01

    def test_not_available_returns_none(self) -> None:
        data = struct.pack("<BHhhB", 0, 0xFFFF, 0x7FFF, 0x7FFF, 0)
        result = decode(PGN_VESSEL_HEADING, data, 5, _UNIX_TS)
        assert result is None

    def test_source_addr_recorded(self) -> None:
        data = self._make_data(math.pi)
        result = decode(PGN_VESSEL_HEADING, data, 42, _UNIX_TS)
        assert isinstance(result, HeadingRecord)
        assert result.source_addr == 42

    def test_timestamp_is_utc(self) -> None:
        data = self._make_data(math.pi)
        result = decode(PGN_VESSEL_HEADING, data, 5, _UNIX_TS)
        assert isinstance(result, HeadingRecord)
        assert result.timestamp.tzinfo is not None
        assert result.timestamp == _TS


class TestDecode128259:
    def test_speed_5_knots(self) -> None:
        mps = 5.0 / 1.94384449  # knots → m/s
        raw = round(mps / 0.01)
        data = struct.pack("<BHH B", 0, raw, 0xFFFF, 0)
        result = decode(PGN_SPEED_THROUGH_WATER, data, 5, _UNIX_TS)
        assert isinstance(result, SpeedRecord)
        assert abs(result.speed_kts - 5.0) < 0.05

    def test_not_available_returns_none(self) -> None:
        data = struct.pack("<BHH B", 0, 0xFFFF, 0xFFFF, 0)
        result = decode(PGN_SPEED_THROUGH_WATER, data, 5, _UNIX_TS)
        assert result is None


class TestDecode128267:
    def test_depth_10m(self) -> None:
        raw_depth = round(10.0 / 0.01)
        data = struct.pack("<BIh", 0, raw_depth, 0)
        result = decode(PGN_WATER_DEPTH, data, 5, _UNIX_TS)
        assert isinstance(result, DepthRecord)
        assert abs(result.depth_m - 10.0) < 0.01

    def test_offset_present(self) -> None:
        raw_depth = round(5.0 / 0.01)
        raw_offset = round(0.5 / 0.001)  # 0.5 m offset
        data = struct.pack("<BIh", 0, raw_depth, raw_offset)
        result = decode(PGN_WATER_DEPTH, data, 5, _UNIX_TS)
        assert isinstance(result, DepthRecord)
        assert result.offset_m is not None
        assert abs(result.offset_m - 0.5) < 0.001

    def test_not_available_returns_none(self) -> None:
        data = struct.pack("<BIh", 0, 0xFFFFFFFF, 0)
        result = decode(PGN_WATER_DEPTH, data, 5, _UNIX_TS)
        assert result is None


class TestDecode129025:
    def test_position_sf_bay(self) -> None:
        lat, lon = 37.8044, -122.2712
        raw_lat = round(lat / 1e-7)
        raw_lon = round(lon / 1e-7)
        data = struct.pack("<ii", raw_lat, raw_lon)
        result = decode(PGN_POSITION_RAPID, data, 5, _UNIX_TS)
        assert isinstance(result, PositionRecord)
        assert abs(result.latitude_deg - lat) < 1e-4
        assert abs(result.longitude_deg - lon) < 1e-4

    def test_negative_latitude(self) -> None:
        lat, lon = -33.8688, 151.2093  # Sydney
        raw_lat = round(lat / 1e-7)
        raw_lon = round(lon / 1e-7)
        data = struct.pack("<ii", raw_lat, raw_lon)
        result = decode(PGN_POSITION_RAPID, data, 5, _UNIX_TS)
        assert isinstance(result, PositionRecord)
        assert result.latitude_deg < 0


class TestDecode129026:
    def test_cog_sog(self) -> None:
        raw_cog = round(math.radians(90.0) / 0.0001)
        mps = 4.0 / 1.94384449
        raw_sog = round(mps / 0.01)
        data = struct.pack("<BBHHBB", 0, 0, raw_cog, raw_sog, 0, 0)
        result = decode(PGN_COG_SOG_RAPID, data, 5, _UNIX_TS)
        assert isinstance(result, COGSOGRecord)
        assert abs(result.cog_deg - 90.0) < 0.1
        assert abs(result.sog_kts - 4.0) < 0.05


class TestDecode130306:
    def test_wind_15kts_30deg_true(self) -> None:
        mps = 15.0 / 1.94384449
        raw_speed = round(mps / 0.01)
        raw_angle = round(math.radians(30.0) / 0.0001)
        data = struct.pack("<BHHB", 0, raw_speed, raw_angle, 0)
        result = decode(PGN_WIND_DATA, data, 5, _UNIX_TS)
        assert isinstance(result, WindRecord)
        assert abs(result.wind_speed_kts - 15.0) < 0.1
        assert abs(result.wind_angle_deg - 30.0) < 0.1
        assert result.reference == 0

    def test_apparent_wind_reference(self) -> None:
        mps = 10.0 / 1.94384449
        raw_speed = round(mps / 0.01)
        raw_angle = round(math.radians(45.0) / 0.0001)
        data = struct.pack("<BHHB", 0, raw_speed, raw_angle, 2)  # reference=2 apparent
        result = decode(PGN_WIND_DATA, data, 5, _UNIX_TS)
        assert isinstance(result, WindRecord)
        assert result.reference == 2


class TestDecode130310:
    def test_water_temp_20c(self) -> None:
        kelvin = 20.0 + 273.15
        raw_temp = round(kelvin / 0.01)
        data = struct.pack("<BH HBBBB", 0, raw_temp, 0xFFFF, 0, 0, 0, 0)
        result = decode(PGN_ENVIRONMENTAL, data, 5, _UNIX_TS)
        assert isinstance(result, EnvironmentalRecord)
        assert abs(result.water_temp_c - 20.0) < 0.1

    def test_not_available_returns_none(self) -> None:
        data = struct.pack("<BH HBBBB", 0, 0xFFFF, 0xFFFF, 0, 0, 0, 0)
        result = decode(PGN_ENVIRONMENTAL, data, 5, _UNIX_TS)
        assert result is None


# ---------------------------------------------------------------------------
# Unit conversion consistency
# ---------------------------------------------------------------------------


class TestUnitConversions:
    """Verify that round-trip conversions are consistent."""

    def test_radians_to_degrees_90(self) -> None:
        raw_hdg = round(math.radians(90.0) / 0.0001)
        data = struct.pack("<BHhhB", 0, raw_hdg, 0x7FFF, 0x7FFF, 0)
        result = decode(PGN_VESSEL_HEADING, data, 5, _UNIX_TS)
        assert isinstance(result, HeadingRecord)
        assert abs(result.heading_deg - 90.0) < 0.01

    def test_mps_to_knots_10kts(self) -> None:
        mps = 10.0 / 1.94384449
        raw = round(mps / 0.01)
        data = struct.pack("<BHH B", 0, raw, 0xFFFF, 0)
        result = decode(PGN_SPEED_THROUGH_WATER, data, 5, _UNIX_TS)
        assert isinstance(result, SpeedRecord)
        assert abs(result.speed_kts - 10.0) < 0.05

    def test_kelvin_to_celsius_0c(self) -> None:
        kelvin = 0.0 + 273.15
        raw_temp = round(kelvin / 0.01)
        data = struct.pack("<BH HBBBB", 0, raw_temp, 0xFFFF, 0, 0, 0, 0)
        result = decode(PGN_ENVIRONMENTAL, data, 5, _UNIX_TS)
        assert isinstance(result, EnvironmentalRecord)
        assert abs(result.water_temp_c - 0.0) < 0.1
