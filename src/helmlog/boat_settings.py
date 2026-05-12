"""Boat tuning parameters — canonical definitions and metadata.

Each parameter has a canonical name, display label, unit, input type, and
category. This module is the single source of truth for the parameter list
used by the storage layer and API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SettingCategory = Literal[
    "rig", "sail_controls", "deck_hardware", "crew", "conditions", "instrument_calibration"
]
InputType = Literal["number", "preset"]


@dataclass(frozen=True)
class ParameterDef:
    """Metadata for one tuning parameter."""

    name: str
    label: str
    unit: str
    input_type: InputType
    category: SettingCategory


# ---------------------------------------------------------------------------
# Canonical parameter list (order within category = display order)
# ---------------------------------------------------------------------------

PARAMETERS: tuple[ParameterDef, ...] = (
    # Rig — set pre-race, rarely change
    ParameterDef("shroud_tension_upper", "Shroud tension upper", "Loos", "number", "rig"),
    ParameterDef("shroud_tension_d2", "Shroud tension D2", "Loos", "number", "rig"),
    ParameterDef("shroud_tension_lowers", "Shroud tension lowers", "Loos", "number", "rig"),
    # Sail controls — change during race
    ParameterDef("main_halyard", "Main halyard", "in", "number", "sail_controls"),
    ParameterDef("jib_halyard", "Jib halyard", "in", "number", "sail_controls"),
    ParameterDef("vang", "Vang", "in", "number", "sail_controls"),
    ParameterDef("cunningham", "Cunningham", "in", "number", "sail_controls"),
    ParameterDef("outhaul", "Outhaul", "in", "number", "sail_controls"),
    ParameterDef("backstay", "Backstay", "in", "number", "sail_controls"),
    ParameterDef("main_sheet_tension", "Main sheet tension", "in", "number", "sail_controls"),
    ParameterDef(
        "jib_sheet_tension_port", "Jib sheet tension port", "in", "number", "sail_controls"
    ),
    ParameterDef(
        "jib_sheet_tension_starboard",
        "Jib sheet tension starboard",
        "in",
        "number",
        "sail_controls",
    ),
    ParameterDef("traveler_position", "Traveler position", "in", "number", "sail_controls"),
    # Deck hardware — hole numbers
    ParameterDef("car_position_port", "Car position port", "hole", "number", "deck_hardware"),
    ParameterDef(
        "car_position_starboard", "Car position starboard", "hole", "number", "deck_hardware"
    ),
    # Crew
    ParameterDef("weight_distribution", "Weight distribution", "", "preset", "crew"),
    # Conditions — sea state
    ParameterDef("swell_height", "Swell height", "ft", "number", "conditions"),
    ParameterDef("swell_period", "Swell period", "s", "number", "conditions"),
    ParameterDef("chop", "Chop", "ft", "number", "conditions"),
    # Instrument calibration — B&G calibration order: speed → compass → wind → depth → other
    ParameterDef("speed_correction", "Speed correction", "%", "number", "instrument_calibration"),
    ParameterDef("speed_damping", "Speed damping", "0–9", "number", "instrument_calibration"),
    ParameterDef("heading_offset", "Heading offset", "°", "number", "instrument_calibration"),
    ParameterDef("heading_damping", "Heading damping", "0–9", "number", "instrument_calibration"),
    ParameterDef(
        "wind_angle_offset", "MHU wind angle offset", "°", "number", "instrument_calibration"
    ),
    ParameterDef(
        "wind_speed_correction", "Wind speed correction", "%", "number", "instrument_calibration"
    ),
    ParameterDef("wind_damping", "Wind damping", "0–9", "number", "instrument_calibration"),
    ParameterDef("depth_offset", "Depth offset", "m", "number", "instrument_calibration"),
    ParameterDef("depth_damping", "Depth damping", "0–9", "number", "instrument_calibration"),
    ParameterDef("sea_temp_offset", "Sea temp offset", "°C", "number", "instrument_calibration"),
    ParameterDef("heel_offset", "Heel offset", "°", "number", "instrument_calibration"),
    ParameterDef("trim_offset", "Trim offset", "°", "number", "instrument_calibration"),
    ParameterDef(
        "leeway_coefficient",
        "Leeway coefficient",
        "",
        "number",
        "instrument_calibration",
    ),
    ParameterDef(
        "rudder_angle_offset",
        "Rudder angle offset",
        "°",
        "number",
        "instrument_calibration",
    ),
    ParameterDef(
        "compass_offset_port",
        "Compass offset (port tack)",
        "°",
        "number",
        "instrument_calibration",
    ),
    ParameterDef(
        "compass_offset_stbd",
        "Compass offset (stbd tack)",
        "°",
        "number",
        "instrument_calibration",
    ),
    ParameterDef("mast_height", "Mast height", "m", "number", "instrument_calibration"),
)

PARAMETER_NAMES: frozenset[str] = frozenset(p.name for p in PARAMETERS)

WEIGHT_DISTRIBUTION_PRESETS: tuple[str, ...] = (
    "rail",
    "hike",
    "stack to weather",
    "weight forward",
    "centered",
    "aft",
)

# Category display order and labels
CATEGORY_ORDER: tuple[tuple[SettingCategory, str], ...] = (
    ("sail_controls", "Sail Controls"),
    ("deck_hardware", "Deck Hardware"),
    ("rig", "Rig"),
    ("crew", "Crew"),
    ("conditions", "Conditions"),
    ("instrument_calibration", "Instrument Calibration"),
)


def parameters_by_category() -> dict[SettingCategory, list[ParameterDef]]:
    """Return parameters grouped by category in display order."""
    result: dict[SettingCategory, list[ParameterDef]] = {}
    for cat, _label in CATEGORY_ORDER:
        result[cat] = [p for p in PARAMETERS if p.category == cat]
    return result
