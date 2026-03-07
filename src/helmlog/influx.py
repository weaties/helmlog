"""InfluxDB sink for session notes and historical track backfill.

Writes notes to InfluxDB as ``session_notes`` points so they appear as
Grafana annotations via the existing InfluxDB datasource — no extra plugin
required.

Also supports writing historical GPS track data for Grafana dashboard
visualisation of backfilled races.

All writes are best-effort: if InfluxDB is unreachable or misconfigured the
error is logged at WARNING level and the caller continues normally.

Configuration (env vars, all optional):
  INFLUX_URL    http://localhost:8086
  INFLUX_TOKEN  operator token (from ~/influx-token.txt on the Pi)
  INFLUX_ORG    helmlog
  INFLUX_BUCKET signalk
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.gaigps import GaiaTrack


def _client() -> tuple[Any, Any] | tuple[None, None]:
    """Return a configured InfluxDB WriteApi client, or None if unconfigured."""
    try:
        from influxdb_client import InfluxDBClient  # type: ignore[attr-defined]
        from influxdb_client.client.write_api import SYNCHRONOUS
    except ImportError:
        return None, None

    url = os.environ.get("INFLUX_URL", "http://localhost:8086")
    token = os.environ.get("INFLUX_TOKEN", "")
    org = os.environ.get("INFLUX_ORG", "helmlog")
    if not token:
        return None, None

    client = InfluxDBClient(url=url, token=token, org=org)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    return client, write_api


def write_note(
    *,
    ts_iso: str,
    note_type: str,
    body: str | None,
    race_id: int | None,
    note_id: int,
) -> None:
    """Write a single note to InfluxDB as a ``session_notes`` point.

    Call this after successfully inserting the note into SQLite.
    Errors are caught and logged — callers must not rely on this succeeding.
    """
    client, write_api = _client()
    if write_api is None:
        return

    bucket = os.environ.get("INFLUX_BUCKET", "signalk")
    org = os.environ.get("INFLUX_ORG", "helmlog")

    try:
        from influxdb_client import Point  # type: ignore[attr-defined]

        ts = datetime.fromisoformat(ts_iso)
        point: Any = (
            Point("session_notes")  # type: ignore[no-untyped-call]
            .tag("note_type", note_type)
            .tag("race_id", str(race_id) if race_id is not None else "")
            .field("body", body or "")
            .field("note_id", note_id)
            .time(ts)
        )
        write_api.write(bucket=bucket, org=org, record=point)
    except Exception as exc:  # noqa: BLE001
        logger.warning("InfluxDB note write failed (non-fatal): {}", exc)
    finally:
        if client:
            client.close()


def write_historical_track(
    track: GaiaTrack,
    session_id: int,
    session_type: str = "race",
    *,
    batch_size: int = 500,
) -> int:
    """Write all track points to InfluxDB with their original timestamps.

    Each point is written as a ``sailing`` measurement with fields for
    lat, lon, sog, cog and tags for source, session_id, session_type.

    Returns the number of points written.
    """
    client, write_api = _client()
    if write_api is None:
        logger.warning("InfluxDB not configured — skipping track backfill")
        return 0

    bucket = os.environ.get("INFLUX_BUCKET", "signalk")
    org = os.environ.get("INFLUX_ORG", "helmlog")
    written = 0

    try:
        from influxdb_client import Point  # type: ignore[attr-defined]

        batch: list[Any] = []
        for p in track.points:
            point: Any = (
                Point("sailing")  # type: ignore[no-untyped-call]
                .tag("source", "gaiagps")
                .tag("session_id", str(session_id))
                .tag("session_type", session_type)
                .field("latitude", p.lat)
                .field("longitude", p.lon)
                .time(p.timestamp)
            )
            if p.sog_kts is not None:
                point = point.field("sog_kts", p.sog_kts)
            if p.cog_deg is not None:
                point = point.field("cog_deg", p.cog_deg)
            if p.elevation_m is not None:
                point = point.field("elevation_m", p.elevation_m)
            batch.append(point)

            if len(batch) >= batch_size:
                write_api.write(bucket=bucket, org=org, record=batch)
                written += len(batch)
                batch = []

        if batch:
            write_api.write(bucket=bucket, org=org, record=batch)
            written += len(batch)

        logger.info(
            "Wrote {} points to InfluxDB for session {} ({})",
            written,
            session_id,
            track.name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("InfluxDB track backfill failed (non-fatal): {}", exc)
    finally:
        if client:
            client.close()

    return written
