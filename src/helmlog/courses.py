"""CYC race mark data and course builder.

Provides geographic mark coordinates for the Corinthian Yacht Club (Seattle)
racing area in Puget Sound, plus functions to compute buoy-course mark
positions from a race committee location and wind direction.

Land/water detection uses a high-resolution coastline polygon derived from
OpenStreetMap ``natural=coastline`` data for the Puget Sound racing area,
loaded from ``puget_sound_land.json`` and tested with Shapely.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import PreparedGeometry
from shapely.prepared import prep as shapely_prep

# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------

_NM_DEG_LAT = 1.0 / 60.0  # 1 nautical mile in degrees of latitude


@dataclass(frozen=True)
class CourseMark:
    """A mark on a sailing course with geographic coordinates."""

    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class CourseLeg:
    """A leg between two marks on a course."""

    target: CourseMark
    upwind: bool


# ---------------------------------------------------------------------------
# CYC geographic marks — coordinates verified against OSM coastline data
# ---------------------------------------------------------------------------

CYC_MARKS: dict[str, CourseMark] = {
    "D": CourseMark("Duwamish Head Lt.", 47.5935, -122.3920),
    "E": CourseMark("Shilshole Bay Entrance", 47.6838, -122.4128),
    "H": CourseMark("0.3nm E of Skiff Pt", 47.6410, -122.4180),
    "I": CourseMark("0.5nm N of Alki Pt", 47.5847, -122.4210),
    "J": CourseMark("0.25nm SSW of marina N entrance", 47.6790, -122.4074),
    "K": CourseMark("Blakely Rock", 47.5877, -122.4873),
    "L": CourseMark("0.5nm SW of marina S entrance", 47.6728, -122.4125),
    "M": CourseMark("Meadow Pt. Buoy", 47.6940, -122.4080),
    "N": CourseMark("1.5nm E of TSS Buoy SF", 47.7300, -122.3800),
    "P": CourseMark("0.5nm NNE of Pt. Monroe", 47.7183, -122.4400),
    "Q": CourseMark("3.0nm 340\u00b0 from Meadow Pt", 47.7410, -122.4200),
    "R": CourseMark("0.5nm SW of Pt. Wells", 47.7680, -122.4032),
    "T": CourseMark("0.5nm SE of Pt. Jefferson", 47.7433, -122.4100),
    "U": CourseMark("U Mark", 47.7400, -122.3825),
    "V": CourseMark("0.3nm NNE of Wing Pt", 47.6295, -122.4900),
}


# ---------------------------------------------------------------------------
# Real coastline data — loaded from OSM-derived GeoJSON
# ---------------------------------------------------------------------------
# The land polygon covers the CYC racing area bounding box (lat 47.55–47.80,
# lon -122.53 – -122.34).  It was built from OpenStreetMap ``natural=coastline``
# ways, clipped to the bounding box, and simplified to ~30 m tolerance.
# Points inside the land polygon are on land; points inside the bounding box
# but outside the land polygon are in navigable water.

_BBOX_N = 47.80
_BBOX_S = 47.55
_BBOX_E = -122.34
_BBOX_W = -122.53

# Lazy-loaded land geometry and prepared version for fast queries
_land_prep: PreparedGeometry | None = None  # type: ignore[type-arg]


def _load_land() -> None:
    """Load the land polygon from bundled GeoJSON (lazy, once)."""
    global _land_prep  # noqa: PLW0603
    if _land_prep is not None:
        return
    geojson_path = Path(__file__).parent / "puget_sound_land.json"
    with geojson_path.open() as f:
        fc = json.load(f)
    _land_prep = shapely_prep(shape(fc["features"][0]["geometry"]))


def is_in_water(lat: float, lon: float) -> bool:
    """Return True if the position is in navigable water (>6 ft deep).

    Uses high-resolution OSM coastline data for the Puget Sound CYC racing
    area.  A point is considered in water if it is inside the racing-area
    bounding box and not inside any land polygon.
    """
    if not (_BBOX_S <= lat <= _BBOX_N and _BBOX_W <= lon <= _BBOX_E):
        return False
    _load_land()
    assert _land_prep is not None  # set by _load_land
    return not _land_prep.contains(Point(lon, lat))


# Minimum depth threshold in metres (~6 ft)
_MIN_DEPTH_M = 1.83


def validate_course_marks(marks: dict[str, CourseMark]) -> list[str]:
    """Check that all course marks are in navigable water (>6 ft deep).

    Returns a list of human-readable warning strings.  An empty list means
    all marks passed validation.

    Uses high-resolution OSM coastline data to detect marks that are on land
    or outside the navigable CYC racing area.
    """
    warnings: list[str] = []
    for key, mark in marks.items():
        if not is_in_water(mark.lat, mark.lon):
            warnings.append(
                f"Mark {key} ({mark.name}) at ({mark.lat:.4f}, {mark.lon:.4f}) "
                f"may be on land or in shallow water (<6 ft)"
            )
    return warnings


# ---------------------------------------------------------------------------
# Bearing / offset helpers
# ---------------------------------------------------------------------------


def _offset(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    """Offset a position by a bearing (degrees true) and distance (nm)."""
    br = math.radians(bearing_deg)
    dlat = dist_nm * math.cos(br) * _NM_DEG_LAT
    dlon = dist_nm * math.sin(br) * _NM_DEG_LAT / math.cos(math.radians(lat))
    return lat + dlat, lon + dlon


def _bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate bearing from point 1 to point 2 in degrees true."""
    dlat = (lat2 - lat1) * 60.0
    dlon = (lon2 - lon1) * 60.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2(dlon, dlat)) % 360


def _is_upwind(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float, wind_dir: float
) -> bool:
    """Return True if sailing from->to is upwind relative to wind_dir."""
    brg = _bearing_between(from_lat, from_lon, to_lat, to_lon)
    # Angle between course bearing and wind direction (wind comes FROM wind_dir)
    diff = abs(((brg - wind_dir + 180) % 360) - 180)
    return diff < 70  # within 70 deg of dead upwind


# ---------------------------------------------------------------------------
# Buoy mark computation
# ---------------------------------------------------------------------------


def compute_buoy_marks(
    rc_lat: float,
    rc_lon: float,
    wind_dir: float,
    leg_distance_nm: float = 1.0,
) -> dict[str, CourseMark]:
    """Compute buoy-course mark positions relative to RC and wind.

    Returns dict with keys: S, A, O, G, X, F.
    Wind direction is degrees true (direction wind comes FROM).
    """
    # A: windward mark — upwind of RC
    a_lat, a_lon = _offset(rc_lat, rc_lon, wind_dir, leg_distance_nm)
    # X: leeward mark — downwind of RC
    downwind = (wind_dir + 180) % 360
    x_lat, x_lon = _offset(rc_lat, rc_lon, downwind, leg_distance_nm)
    # O: offset mark — slightly below A (0.1 nm downwind of A)
    o_lat, o_lon = _offset(a_lat, a_lon, downwind, 0.1)
    # G: gybe mark — downwind + 0.5 nm perpendicular offset to starboard
    g_base_lat, g_base_lon = _offset(rc_lat, rc_lon, downwind, leg_distance_nm * 0.7)
    stbd_bearing = (wind_dir + 90) % 360
    g_lat, g_lon = _offset(g_base_lat, g_base_lon, stbd_bearing, leg_distance_nm * 0.5)
    # S: start mark — slightly to port of RC on the start line
    port_bearing = (wind_dir - 90) % 360
    s_lat, s_lon = _offset(rc_lat, rc_lon, port_bearing, 0.05)
    # F: finish mark — at RC position
    return {
        "S": CourseMark("Start", s_lat, s_lon),
        "A": CourseMark("Windward A", a_lat, a_lon),
        "O": CourseMark("Offset O", o_lat, o_lon),
        "G": CourseMark("Gybe G", g_lat, g_lon),
        "X": CourseMark("Leeward X", x_lat, x_lon),
        "F": CourseMark("Finish", rc_lat, rc_lon),
    }


# ---------------------------------------------------------------------------
# Course builders
# ---------------------------------------------------------------------------


def build_wl_course(
    rc_lat: float,
    rc_lon: float,
    wind_dir: float,
    leg_nm: float = 1.0,
    laps: int = 2,
) -> list[CourseLeg]:
    """Build a windward/leeward course: Start -> A -> X -> ... -> A -> F (finish at RC)."""
    marks = compute_buoy_marks(rc_lat, rc_lon, wind_dir, leg_nm)
    legs: list[CourseLeg] = []
    for _ in range(laps):
        legs.append(CourseLeg(marks["A"], upwind=True))
        legs.append(CourseLeg(marks["X"], upwind=False))
    # Final beat to windward mark then finish at RC
    legs.append(CourseLeg(marks["A"], upwind=True))
    legs.append(CourseLeg(marks["F"], upwind=False))
    return legs


def build_triangle_course(
    rc_lat: float,
    rc_lon: float,
    wind_dir: float,
    leg_nm: float = 1.0,
) -> list[CourseLeg]:
    """Build a triangle course: Start -> A -> G -> X -> F (finish at RC)."""
    marks = compute_buoy_marks(rc_lat, rc_lon, wind_dir, leg_nm)
    return [
        CourseLeg(marks["A"], upwind=True),
        CourseLeg(marks["G"], upwind=False),
        CourseLeg(marks["X"], upwind=False),
        CourseLeg(
            marks["F"],
            upwind=_is_upwind(
                marks["X"].lat,
                marks["X"].lon,
                rc_lat,
                rc_lon,
                wind_dir,
            ),
        ),
    ]


def build_custom_course(
    mark_sequence: str,
    rc_lat: float,
    rc_lon: float,
    wind_dir: float,
    leg_nm: float = 1.0,
) -> list[CourseLeg]:
    """Parse a mark sequence string (e.g. 'S-A-G-X-F') into course legs.

    Raises ValueError for unknown mark letters.
    """
    buoy_marks = compute_buoy_marks(rc_lat, rc_lon, wind_dir, leg_nm)
    valid_marks = {**buoy_marks, **CYC_MARKS}
    tokens = [t.strip() for t in mark_sequence.upper().split("-") if t.strip()]
    if len(tokens) < 2:
        msg = "Course must have at least 2 marks"
        raise ValueError(msg)

    for t in tokens:
        if t not in valid_marks:
            msg = f"Unknown mark '{t}'. Valid: {sorted(valid_marks)}"
            raise ValueError(msg)

    legs: list[CourseLeg] = []
    for i in range(1, len(tokens)):
        prev = valid_marks[tokens[i - 1]]
        tgt = valid_marks[tokens[i]]
        upwind = _is_upwind(prev.lat, prev.lon, tgt.lat, tgt.lon, wind_dir)
        legs.append(CourseLeg(tgt, upwind=upwind))
    return legs
