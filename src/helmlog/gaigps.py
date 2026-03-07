"""Gaia GPS track download and parsing.

Downloads track data from the Gaia GPS web API using session cookie auth.
Parses track geometry (MultiLineString with [lon, lat, elev, epoch] points)
into typed dataclasses. Computes SOG/COG from consecutive GPS fixes.

This module is imported only by main.py (CLI wiring). It has no dependency
on storage.py, web.py, or any hardware modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------

_ELEVATION_SENTINEL = -19999.0
_MPS_TO_KTS = 1.94384449


@dataclass
class TrackPoint:
    """A single GPS fix from a Gaia track."""

    lat: float
    lon: float
    elevation_m: float | None
    timestamp: datetime
    sog_kts: float | None = None  # computed from consecutive points
    cog_deg: float | None = None  # computed from consecutive points


@dataclass
class TrackSummary:
    """Lightweight metadata from the track list endpoint."""

    track_id: str
    title: str
    time_created: datetime
    distance_m: float
    total_time_s: float
    source: str


@dataclass
class GaiaTrack:
    """Full track with point-by-point geometry."""

    track_id: str
    name: str
    started_at: datetime
    ended_at: datetime
    points: list[TrackPoint]
    distance_m: float
    max_speed_mps: float
    source: str = "gaiagps"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_track_list(raw_items: list[dict[str, Any]]) -> list[TrackSummary]:
    """Parse the JSON array from GET /api/objects/track/."""
    results: list[TrackSummary] = []
    for item in raw_items:
        if item.get("deleted"):
            continue
        time_str = item.get("time_created", "")
        try:
            time_created = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            time_created = datetime.now(UTC)

        results.append(
            TrackSummary(
                track_id=item["id"],
                title=item.get("title") or "(untitled)",
                time_created=time_created,
                distance_m=float(item.get("distance") or 0),
                total_time_s=float(item.get("total_time") or 0),
                source=item.get("source") or "",
            )
        )
    return results


def parse_track_detail(data: dict[str, Any]) -> GaiaTrack:
    """Parse the JSON from GET /api/v3/tracks/{id}/."""
    geometry = data["geometry"]
    coords_lists = geometry["coordinates"]

    # Flatten MultiLineString segments into one point list
    raw_points: list[list[float]] = []
    for segment in coords_lists:
        raw_points.extend(segment)

    points: list[TrackPoint] = []
    for coord in raw_points:
        lon, lat, elev, epoch = coord[0], coord[1], coord[2], coord[3]
        elevation = None if elev == _ELEVATION_SENTINEL else elev
        ts = datetime.fromtimestamp(epoch, tz=UTC)
        points.append(TrackPoint(lat=lat, lon=lon, elevation_m=elevation, timestamp=ts))

    # Compute SOG/COG from consecutive points
    for i in range(1, len(points)):
        prev, cur = points[i - 1], points[i]
        dt_s = (cur.timestamp - prev.timestamp).total_seconds()
        if dt_s > 0:
            dist_m = _haversine_m(prev.lat, prev.lon, cur.lat, cur.lon)
            cur.sog_kts = (dist_m / dt_s) * _MPS_TO_KTS
            cur.cog_deg = _bearing_deg(prev.lat, prev.lon, cur.lat, cur.lon)

    stats = data.get("stats") or {}
    started_at = points[0].timestamp if points else datetime.now(UTC)
    ended_at = points[-1].timestamp if points else started_at

    return GaiaTrack(
        track_id=data["id"],
        name=data.get("name") or "(untitled)",
        started_at=started_at,
        ended_at=ended_at,
        points=points,
        distance_m=float(stats.get("distance") or 0),
        max_speed_mps=float(stats.get("max_speed") or 0),
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


async def list_tracks(
    session_id: str,
    csrf_token: str,
    *,
    since: datetime | None = None,
    page_size: int = 1000,
) -> list[TrackSummary]:
    """Fetch all track summaries from Gaia GPS."""
    import httpx

    headers = {
        "Cookie": f"sessionid={session_id}; csrftoken={csrf_token}",
        "X-CSRFToken": csrf_token,
    }

    all_summaries: list[TrackSummary] = []
    page = 1

    async with httpx.AsyncClient(base_url="https://www.gaiagps.com", timeout=30) as client:
        while True:
            resp = await client.get(
                "/api/objects/track/",
                params={
                    "sort_direction": "desc",
                    "sort_field": "create_date",
                    "show_archived": "false",
                    "show_filed": "true",
                    "page": page,
                    "count": page_size,
                },
                headers=headers,
            )
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break

            summaries = parse_track_list(items)
            for s in summaries:
                if since and s.time_created < since:
                    return all_summaries
                all_summaries.append(s)

            if len(items) < page_size:
                break
            page += 1

    logger.info("Fetched {} track summaries from Gaia GPS", len(all_summaries))
    return all_summaries


async def download_track(
    session_id: str,
    csrf_token: str,
    track_id: str,
) -> GaiaTrack:
    """Download full track detail from Gaia GPS."""
    import asyncio

    import httpx

    headers = {
        "Cookie": f"sessionid={session_id}; csrftoken={csrf_token}",
        "X-CSRFToken": csrf_token,
    }

    async with httpx.AsyncClient(base_url="https://www.gaiagps.com", timeout=30) as client:
        resp = await client.get(f"/api/v3/tracks/{track_id}/", headers=headers)
        resp.raise_for_status()
        track = parse_track_detail(resp.json())

    # Be polite — 1s delay between requests
    await asyncio.sleep(1)
    return track


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_000


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two points."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return _EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees [0, 360) from point 1 to point 2."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlon = lon2_r - lon1_r
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360
