"""Tests for export.py — CSV, GPX, and JSON export."""

from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from helmlog.export import export_csv, export_gpx, export_json, export_to_file
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
)

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage

_TS = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_END = _TS + timedelta(seconds=2)  # 3-second window → 3 rows

# Expected CSV columns
_EXPECTED_COLS = {
    "timestamp",
    "HDG",
    "BSP",
    "DEPTH",
    "LAT",
    "LON",
    "COG",
    "SOG",
    "TWS",
    "TWA",
    "AWA",
    "AWS",
    "WTEMP",
    "video_url",
    "WX_TWS",
    "WX_TWD",
    "AIR_TEMP",
    "PRESSURE",
    "TIDE_HT",
    "crew_helm",
    "crew_main",
    "crew_jib",
    "crew_spin",
    "crew_tactician",
    "BSP_BASELINE",
    "BSP_DELTA",
}


async def _populate(storage: Storage) -> None:
    """Write one record of each type at _TS."""
    records = [
        HeadingRecord(PGN_VESSEL_HEADING, 5, _TS, 180.0, None, None),
        SpeedRecord(PGN_SPEED_THROUGH_WATER, 5, _TS, 5.0),
        DepthRecord(PGN_WATER_DEPTH, 5, _TS, 10.0, None),
        PositionRecord(PGN_POSITION_RAPID, 5, _TS, 37.8044, -122.2712),
        COGSOGRecord(PGN_COG_SOG_RAPID, 5, _TS, 45.0, 6.0),
        WindRecord(PGN_WIND_DATA, 5, _TS, 15.0, 30.0, 0),  # true wind
        EnvironmentalRecord(PGN_ENVIRONMENTAL, 5, _TS, 20.0),
    ]
    for r in records:
        await storage.write(r)


class TestExportCSV:
    async def test_columns_present(self, storage: Storage, tmp_path: Path) -> None:
        """Output CSV must have all expected columns."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            assert set(reader.fieldnames or []) == _EXPECTED_COLS

    async def test_row_count_matches_seconds(self, storage: Storage, tmp_path: Path) -> None:
        """3-second window [T, T+2] → 3 rows."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        rows_written = await export_csv(storage, _TS, _END, out)
        assert rows_written == 3

        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 3

    async def test_first_row_has_data(self, storage: Storage, tmp_path: Path) -> None:
        """First row (at _TS) should have populated values."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        first = rows[0]
        assert first["HDG"] != ""
        assert first["BSP"] != ""
        assert first["LAT"] != ""
        assert first["LON"] != ""
        assert first["WTEMP"] != ""

    async def test_missing_data_produces_empty_cells(
        self, storage: Storage, tmp_path: Path
    ) -> None:
        """Seconds with no data should have empty (not erroring) cells."""
        # Only write data at _TS; seconds _TS+1 and _TS+2 have nothing
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        # Second row (T+1) should have empty numeric fields
        second = rows[1]
        assert second["HDG"] == ""
        assert second["BSP"] == ""

    async def test_timestamp_ordering(self, storage: Storage, tmp_path: Path) -> None:
        """Timestamps must be strictly ascending in output."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        timestamps = [datetime.fromisoformat(r["timestamp"]) for r in rows]
        assert timestamps == sorted(timestamps)

    async def test_empty_range_writes_header_only(self, storage: Storage, tmp_path: Path) -> None:
        """Export on range with no data still writes a valid CSV with header."""
        out = tmp_path / "export.csv"
        # Use a past time range with nothing in the DB
        start = datetime(2020, 1, 1, tzinfo=UTC)
        end = datetime(2020, 1, 1, tzinfo=UTC)  # single second
        rows_written = await export_csv(storage, start, end, out)
        assert rows_written == 1  # one second = one row (all empty)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            assert set(reader.fieldnames or []) == _EXPECTED_COLS

    async def test_true_wind_vs_apparent_wind(self, storage: Storage, tmp_path: Path) -> None:
        """True wind (ref=0) → TWS/TWA; apparent wind (ref=2) → AWS/AWA."""
        # True wind
        await storage.write(WindRecord(PGN_WIND_DATA, 5, _TS, 15.0, 30.0, 0))
        # Apparent wind
        await storage.write(WindRecord(PGN_WIND_DATA, 5, _TS, 12.0, 25.0, 2))
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _TS, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        row = rows[0]
        assert row["TWS"] != ""
        assert row["AWA"] != ""
        assert row["TWS"] != row["AWS"]

    async def test_output_dir_created(self, storage: Storage, tmp_path: Path) -> None:
        """Export should create output directory if it doesn't exist."""
        out = tmp_path / "nested" / "deep" / "export.csv"
        await export_csv(storage, _TS, _TS, out)
        assert out.exists()

    async def test_numeric_values_correct(self, storage: Storage, tmp_path: Path) -> None:
        """Spot-check a numeric value in the CSV."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _TS, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        row = rows[0]
        assert abs(float(row["HDG"]) - 180.0) < 0.01
        assert abs(float(row["BSP"]) - 5.0) < 0.01
        assert abs(float(row["DEPTH"]) - 10.0) < 0.01
        assert abs(float(row["WTEMP"]) - 20.0) < 0.01


# ---------------------------------------------------------------------------
# GPX export
# ---------------------------------------------------------------------------

_GPX_NS = "http://www.topografix.com/GPX/1/1"
_SAIL_NS = "http://github.com/weaties/helmlog"


class TestExportGPX:
    async def test_produces_valid_gpx_xml(self, storage: Storage, tmp_path: Path) -> None:
        """Output file must be parseable XML with a GPX root element."""
        await _populate(storage)
        out = tmp_path / "race.gpx"
        await export_gpx(storage, _TS, _END, out)
        tree = ET.parse(out)
        root = tree.getroot()
        assert root.tag == f"{{{_GPX_NS}}}gpx"
        assert root.attrib["version"] == "1.1"

    async def test_trkpts_only_for_seconds_with_position(
        self, storage: Storage, tmp_path: Path
    ) -> None:
        """Only seconds where LAT/LON are available produce a <trkpt>."""
        await _populate(storage)  # position at _TS only
        out = tmp_path / "race.gpx"
        await export_gpx(storage, _TS, _END, out)
        trkpts = ET.parse(out).getroot().findall(f".//{{{_GPX_NS}}}trkpt")
        # _populate writes one position at _TS; _TS+1 and _TS+2 have none
        assert len(trkpts) == 1

    async def test_trkpt_has_correct_lat_lon(self, storage: Storage, tmp_path: Path) -> None:
        """<trkpt> lat/lon attributes match the stored position."""
        await _populate(storage)
        out = tmp_path / "race.gpx"
        await export_gpx(storage, _TS, _TS, out)
        trkpts = ET.parse(out).getroot().findall(f".//{{{_GPX_NS}}}trkpt")
        assert len(trkpts) == 1
        assert abs(float(trkpts[0].attrib["lat"]) - 37.8044) < 0.0001
        assert abs(float(trkpts[0].attrib["lon"]) - -122.2712) < 0.0001

    async def test_trkpt_has_time_element(self, storage: Storage, tmp_path: Path) -> None:
        """Each <trkpt> must have a <time> child element."""
        await _populate(storage)
        out = tmp_path / "race.gpx"
        await export_gpx(storage, _TS, _TS, out)
        trkpts = ET.parse(out).getroot().findall(f".//{{{_GPX_NS}}}trkpt")
        for pt in trkpts:
            assert pt.find(f"{{{_GPX_NS}}}time") is not None

    async def test_sailing_extensions_present(self, storage: Storage, tmp_path: Path) -> None:
        """Sailing data appears in the <extensions> block of each <trkpt>."""
        await _populate(storage)
        out = tmp_path / "race.gpx"
        await export_gpx(storage, _TS, _TS, out)
        trkpts = ET.parse(out).getroot().findall(f".//{{{_GPX_NS}}}trkpt")
        ext = trkpts[0].find(f"{{{_GPX_NS}}}extensions")
        assert ext is not None
        hdg_el = ext.find(f"{{{_SAIL_NS}}}HDG")
        assert hdg_el is not None
        assert abs(float(hdg_el.text or "0") - 180.0) < 0.01

    async def test_returns_total_seconds(self, storage: Storage, tmp_path: Path) -> None:
        """Return value is total seconds in range, not just trkpt count."""
        await _populate(storage)
        out = tmp_path / "race.gpx"
        count = await export_gpx(storage, _TS, _END, out)
        assert count == 3  # _END = _TS + 2s → 3-second window

    async def test_output_dir_created(self, storage: Storage, tmp_path: Path) -> None:
        out = tmp_path / "sub" / "race.gpx"
        await export_gpx(storage, _TS, _TS, out)
        assert out.exists()


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


class TestExportJSON:
    async def test_produces_valid_json(self, storage: Storage, tmp_path: Path) -> None:
        """Output file must be parseable JSON."""
        await _populate(storage)
        out = tmp_path / "race.json"
        await export_json(storage, _TS, _END, out)
        with out.open() as fh:
            doc = json.load(fh)
        assert isinstance(doc, dict)

    async def test_structure(self, storage: Storage, tmp_path: Path) -> None:
        """Top-level keys: generated, start, end, rows."""
        await _populate(storage)
        out = tmp_path / "race.json"
        await export_json(storage, _TS, _END, out)
        with out.open() as fh:
            doc = json.load(fh)
        assert "generated" in doc
        assert "start" in doc
        assert "end" in doc
        assert "rows" in doc
        assert isinstance(doc["rows"], list)

    async def test_row_count(self, storage: Storage, tmp_path: Path) -> None:
        """3-second window → 3 rows."""
        await _populate(storage)
        out = tmp_path / "race.json"
        count = await export_json(storage, _TS, _END, out)
        assert count == 3
        with out.open() as fh:
            doc = json.load(fh)
        assert len(doc["rows"]) == 3

    async def test_numeric_values_are_floats(self, storage: Storage, tmp_path: Path) -> None:
        """Populated numeric fields must be JSON numbers, not strings."""
        await _populate(storage)
        out = tmp_path / "race.json"
        await export_json(storage, _TS, _TS, out)
        with out.open() as fh:
            doc = json.load(fh)
        row = doc["rows"][0]
        assert isinstance(row["HDG"], float)
        assert isinstance(row["BSP"], float)
        assert abs(row["HDG"] - 180.0) < 0.01
        assert abs(row["BSP"] - 5.0) < 0.01

    async def test_missing_values_are_null(self, storage: Storage, tmp_path: Path) -> None:
        """Seconds with no data must have null values, not empty strings."""
        await _populate(storage)
        out = tmp_path / "race.json"
        await export_json(storage, _TS, _END, out)
        with out.open() as fh:
            doc = json.load(fh)
        # Second row (_TS+1) has no instrument data
        second_row = doc["rows"][1]
        assert second_row["HDG"] is None
        assert second_row["BSP"] is None

    async def test_output_dir_created(self, storage: Storage, tmp_path: Path) -> None:
        out = tmp_path / "sub" / "race.json"
        await export_json(storage, _TS, _TS, out)
        assert out.exists()


# ---------------------------------------------------------------------------
# export_to_file dispatch
# ---------------------------------------------------------------------------


class TestExportToFile:
    async def test_csv_extension(self, storage: Storage, tmp_path: Path) -> None:
        out = tmp_path / "race.csv"
        await export_to_file(storage, _TS, _TS, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames is not None

    async def test_gpx_extension(self, storage: Storage, tmp_path: Path) -> None:
        await _populate(storage)
        out = tmp_path / "race.gpx"
        await export_to_file(storage, _TS, _TS, out)
        root = ET.parse(out).getroot()
        assert root.tag == f"{{{_GPX_NS}}}gpx"

    async def test_json_extension(self, storage: Storage, tmp_path: Path) -> None:
        out = tmp_path / "race.json"
        await export_to_file(storage, _TS, _TS, out)
        with out.open() as fh:
            doc = json.load(fh)
        assert "rows" in doc

    async def test_unknown_extension_falls_back_to_csv(
        self, storage: Storage, tmp_path: Path
    ) -> None:
        out = tmp_path / "race.dat"
        await export_to_file(storage, _TS, _TS, out)
        with out.open() as fh:
            first_line = fh.readline()
        assert "timestamp" in first_line


# ---------------------------------------------------------------------------
# Polar baseline columns
# ---------------------------------------------------------------------------


class TestPolarBaselineColumns:
    async def test_bsp_baseline_null_without_polar(self, storage: Storage, tmp_path: Path) -> None:
        """Export with no polar baseline data → BSP_BASELINE and BSP_DELTA are empty."""
        await _populate(storage)
        out = tmp_path / "export.csv"
        await export_csv(storage, _TS, _END, out)
        with out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        first = rows[0]
        assert first["BSP_BASELINE"] == ""
        assert first["BSP_DELTA"] == ""
