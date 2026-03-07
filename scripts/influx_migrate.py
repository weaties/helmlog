"""One-shot script: migrate data from old InfluxDB instance to current one.

Reads all Signal K measurements from the old instance (port 8087) and writes
them to the running instance (port 8086) in batches, chunked by 6-hour windows
to avoid query timeouts on large measurements.

Usage:
    uv run python scripts/influx_migrate.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point  # type: ignore[attr-defined]  # noqa: E402
from influxdb_client.client.write_api import SYNCHRONOUS  # noqa: E402

load_dotenv()

OLD_TOKEN = (  # noqa: S105
    "SS4dyfN7qrsCLKE5ng5xaFrobZvSdeXYXPPFJjn-uVk39ImChHV-7YbJzqcPKuIB6-WTNEXriRstwAAXsmhh6g=="
)
OLD_URL = "http://localhost:8087"
OLD_ORG = "j105"
OLD_BUCKET = "signalk"

NEW_URL = os.environ.get("INFLUX_URL", "http://localhost:8086")
NEW_TOKEN = os.environ.get("INFLUX_TOKEN", "")
NEW_ORG = os.environ.get("INFLUX_ORG", "j105")
NEW_BUCKET = os.environ.get("INFLUX_BUCKET", "signalk")

BATCH_SIZE = 5000
START = datetime(2026, 2, 26, tzinfo=UTC)
STOP = datetime(2026, 3, 2, tzinfo=UTC)
CHUNK_HOURS = 6  # query in 6-hour windows to avoid timeouts

# Measurements already migrated (from previous partial run)
ALREADY_DONE = {
    "",
    "design.aisShipType",
    "displays.a3be9d46-ee05-4c59-85d2-4236ec625997",
    "environment.depth.belowSurface",
    "environment.depth.belowTransducer",
    "environment.depth.surfaceToTransducer",
    "environment.water.temperature",
    "environment.wind.angleApparent",
    "environment.wind.angleTrueGround",
    "environment.wind.angleTrueWater",
    "environment.wind.directionMagnetic",
    "environment.wind.directionTrue",
    "environment.wind.speedApparent",
    "environment.wind.speedOverGround",
    "environment.wind.speedTrue",
    "navigation.attitude.pitch",
    "navigation.attitude.roll",
    "navigation.courseOverGroundMagnetic",
    "navigation.courseOverGroundTrue",
    "navigation.courseRhumbline.nextPoint.position",
    "navigation.currentRoute.name",
    "navigation.currentRoute.waypoints",
    "navigation.datetime",
    "navigation.gnss.antennaAltitude",
    "navigation.gnss.geoidalSeparation",
    "navigation.gnss.horizontalDilution",
    "navigation.gnss.integrity",
    "navigation.gnss.methodQuality",
    "navigation.gnss.positionDilution",
    "navigation.gnss.satellites",
    "navigation.gnss.satellitesInView",
}


def _query_and_write(
    query_api: object,
    write_api: object,
    measurement: str,
    start: datetime,
    stop: datetime,
) -> int:
    """Query one measurement for one time window and write to new instance."""
    start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    stop_s = stop.strftime("%Y-%m-%dT%H:%M:%SZ")

    tables = query_api.query(  # type: ignore[union-attr]
        f'from(bucket: "{OLD_BUCKET}")'
        f" |> range(start: {start_s}, stop: {stop_s})"
        f' |> filter(fn: (r) => r._measurement == "{measurement}")'
    )

    batch: list[object] = []
    count = 0
    for table in tables:
        for record in table.records:
            p = Point(measurement)  # type: ignore[no-untyped-call]
            for key, val in record.values.items():
                if key.startswith("_") or key in ("result", "table"):
                    continue
                p = p.tag(key, str(val))

            field_name = record.get_field()
            field_value = record.get_value()
            if isinstance(field_value, (float, int, bool, str)):
                p = p.field(field_name, field_value)
            else:
                continue

            p = p.time(record.get_time())
            batch.append(p)
            count += 1

            if len(batch) >= BATCH_SIZE:
                write_api.write(bucket=NEW_BUCKET, org=NEW_ORG, record=batch)  # type: ignore[union-attr]
                batch = []

    if batch:
        write_api.write(bucket=NEW_BUCKET, org=NEW_ORG, record=batch)  # type: ignore[union-attr]

    return count


def main() -> None:
    if not NEW_TOKEN:
        print("ERROR: INFLUX_TOKEN not set in .env")
        sys.exit(1)

    old_client = InfluxDBClient(url=OLD_URL, token=OLD_TOKEN, org=OLD_ORG, timeout=120_000)
    new_client = InfluxDBClient(url=NEW_URL, token=NEW_TOKEN, org=NEW_ORG)
    write_api = new_client.write_api(write_options=SYNCHRONOUS)
    query_api = old_client.query_api()

    # Get all measurements
    result = query_api.query(
        f'import "influxdata/influxdb/schema"\nschema.measurements(bucket: "{OLD_BUCKET}")'
    )
    measurements: list[str] = []
    for table in result:
        for record in table.records:
            m = record.values.get("_value")
            if m and m not in ("system_health", "session_notes"):
                measurements.append(m)

    # Skip already-migrated measurements
    remaining = [m for m in sorted(measurements) if m not in ALREADY_DONE]
    print(f"Found {len(measurements)} measurements, {len(remaining)} to migrate")

    total_points = 0
    for i, measurement in enumerate(remaining, 1):
        m_count = 0
        # Chunk by time windows
        chunk_start = START
        while chunk_start < STOP:
            chunk_stop = min(chunk_start + timedelta(hours=CHUNK_HOURS), STOP)
            n = _query_and_write(query_api, write_api, measurement, chunk_start, chunk_stop)
            m_count += n
            chunk_start = chunk_stop

        total_points += m_count
        print(f"  [{i}/{len(remaining)}] {measurement}: {m_count:,} points")

    print(f"\nMigration complete: {total_points:,} total points written")
    old_client.close()
    new_client.close()


if __name__ == "__main__":
    main()
