"""Data-driven screen layout model for the e-paper repeater (#822).

The #820 repeater renders one fixed layout.  This module makes a *screen* a
data-driven, server-rendered layout so the crew can compose new screens from
the web UI — no reflashing, no code deploy.

A **screen** is a *template* plus *slots*:

- A **template** (``two-big-four-small``, ``single``, ``quad``, ``compass``)
  fixes the grid geometry — where each slot sits on the 960x540 panel and how
  big its value/label fonts are.  Templates are code, not config: they are the
  reusable primitives.
- Each **slot** is bound to a **metric** (STW, VMG, TWA/TWS/AWA/AWS, HDG/COG/
  SOG/TWD, rudder, clock) with an optional label override and number format.

Screens are stored as **JSON** in the ``DISPLAY_SCREENS`` app-setting (v1)
rather than a dedicated table, so shipping this needs no ``storage.py``
migration (a Critical-tier change).  It can be promoted to a table later; the
``?screen=`` / ``/screens`` API contract is identical either way.

This module is pure data + math (no Pillow) so it stays trivially testable and
importable without the imaging stack; ``display_render.py`` consumes it to draw
pixels.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# Panel geometry (LilyGo T5-S3 4.7"). Origin top-left, landscape. The header
# strip is shared by every screen; templates lay out the area below it.
WIDTH = 960
HEIGHT = 540
HEADER_H = 66

# App-setting key holding the JSON screen list. Kept out of the generic
# settings registry (SETTINGS_BY_KEY) so it is edited via the composer UI,
# not as a raw textarea.
SCREENS_SETTING_KEY = "DISPLAY_SCREENS"

# Number-format tokens a slot may request. "auto" means "use the metric's
# own default format".
FORMATS = ("auto", "num0", "num1", "num2", "angle", "clock")


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricDef:
    """One bindable value: where it comes from and how it reads by default."""

    key: str  # registry key, e.g. "stw"
    label: str  # default cell label, e.g. "STW"
    unit: str  # display unit appended to the label, e.g. "kts" ("" for none)
    fmt: str  # default format token (one of FORMATS, never "auto")
    column: str | None = None  # key in a latest_instruments() snapshot
    source: str = "instrument"  # "instrument" | "vmg" | "clock"


# Ordered so the composer UI lists them sensibly. Columns match
# Storage.latest_instruments() keys.
METRICS: dict[str, MetricDef] = {
    "stw": MetricDef("stw", "STW", "kts", "num1", column="bsp_kts"),
    "vmg": MetricDef("vmg", "VMG", "kts", "num1", source="vmg"),
    "sog": MetricDef("sog", "SOG", "kts", "num1", column="sog_kts"),
    "tws": MetricDef("tws", "TWS", "kts", "num1", column="tws_kts"),
    "aws": MetricDef("aws", "AWS", "kts", "num1", column="aws_kts"),
    "twa": MetricDef("twa", "TWA", "", "angle", column="twa_deg"),
    "awa": MetricDef("awa", "AWA", "", "angle", column="awa_deg"),
    "twd": MetricDef("twd", "TWD", "", "angle", column="twd_deg"),
    "hdg": MetricDef("hdg", "HDG", "", "angle", column="heading_deg"),
    "cog": MetricDef("cog", "COG", "", "angle", column="cog_deg"),
    "rudder": MetricDef("rudder", "RUD", "", "angle", column="rudder_deg"),
    "clock": MetricDef("clock", "TIME", "", "clock", source="clock"),
}


def compute_vmg(stw_kts: float | None, twa_deg: float | None) -> float | None:
    """Velocity made good to the wind: ``STW * cos(TWA)``.

    Matches HelmLog's semantic-layer definition (see ``semantic_layer.py``).
    Positive upwind (TWA near 0), negative downwind (TWA near 180) — i.e. the
    component of boat speed directly toward the true-wind direction.  Returns
    None if either input is missing.
    """
    if stw_kts is None or twa_deg is None:
        return None
    return round(stw_kts * math.cos(math.radians(twa_deg)), 2)


def metric_value(metric_key: str, instruments: Mapping[str, float | None]) -> float | None:
    """Resolve a metric's numeric value from an instrument snapshot.

    ``clock`` has no numeric value (it reads the render time) and returns None
    here; the renderer formats it from ``now``.  Derived metrics (VMG) are
    computed from the snapshot.
    """
    m = METRICS[metric_key]
    if m.source == "clock":
        return None
    if m.source == "vmg":
        return compute_vmg(instruments.get("bsp_kts"), instruments.get("twa_deg"))
    assert m.column is not None  # every "instrument" metric declares a column
    return instruments.get(m.column)


def resolve_format(metric_key: str, fmt: str) -> str:
    """Return the concrete format token for a slot, resolving ``"auto"``."""
    if fmt == "auto":
        return METRICS[metric_key].fmt
    return fmt


# ---------------------------------------------------------------------------
# Templates (fixed geometry) and screens (config)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One placement within a template's grid.

    ``box`` is an absolute ``(x0, y0, x1, y1)`` pixel rectangle on the panel.
    ``kind`` is ``"value"`` for a labelled number or ``"compass"`` for a dial
    whose bound metric is drawn as a heading needle.
    """

    id: str
    box: tuple[int, int, int, int]
    value_size: int
    label_size: int
    kind: str = "value"


@dataclass(frozen=True)
class Template:
    """A reusable layout: named slots plus the separator lines between them."""

    id: str
    name: str
    slots: tuple[Slot, ...]
    separators: tuple[tuple[int, int, int, int], ...] = ()

    def slot_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.slots)

    def slot(self, slot_id: str) -> Slot | None:
        for s in self.slots:
            if s.id == slot_id:
                return s
        return None


_MID_X = WIDTH // 2
_ROW1_BOT = HEADER_H + 268  # matches the #820 fixed layout
_QUARTER = WIDTH // 4
_HALF_Y = HEADER_H + (HEIGHT - HEADER_H) // 2

TEMPLATES: dict[str, Template] = {
    # The #820 headline layout: two big numbers over a row of four small ones.
    "two-big-four-small": Template(
        id="two-big-four-small",
        name="2 big + 4 small",
        slots=(
            Slot("big1", (0, HEADER_H, _MID_X, _ROW1_BOT), value_size=180, label_size=34),
            Slot("big2", (_MID_X, HEADER_H, WIDTH, _ROW1_BOT), value_size=180, label_size=34),
            Slot("s1", (0, _ROW1_BOT, _QUARTER, HEIGHT), value_size=86, label_size=30),
            Slot("s2", (_QUARTER, _ROW1_BOT, 2 * _QUARTER, HEIGHT), value_size=86, label_size=30),
            Slot(
                "s3", (2 * _QUARTER, _ROW1_BOT, 3 * _QUARTER, HEIGHT), value_size=86, label_size=30
            ),
            Slot("s4", (3 * _QUARTER, _ROW1_BOT, WIDTH, HEIGHT), value_size=86, label_size=30),
        ),
        separators=(
            (0, _ROW1_BOT, WIDTH, _ROW1_BOT),
            (_MID_X, HEADER_H, _MID_X, _ROW1_BOT),
            (_QUARTER, _ROW1_BOT, _QUARTER, HEIGHT),
            (2 * _QUARTER, _ROW1_BOT, 2 * _QUARTER, HEIGHT),
            (3 * _QUARTER, _ROW1_BOT, 3 * _QUARTER, HEIGHT),
        ),
    ),
    # One huge number filling the panel.
    "single": Template(
        id="single",
        name="Single huge number",
        slots=(Slot("main", (0, HEADER_H, WIDTH, HEIGHT), value_size=300, label_size=48),),
    ),
    # Four equal cells.
    "quad": Template(
        id="quad",
        name="Four equal cells",
        slots=(
            Slot("q1", (0, HEADER_H, _MID_X, _HALF_Y), value_size=150, label_size=34),
            Slot("q2", (_MID_X, HEADER_H, WIDTH, _HALF_Y), value_size=150, label_size=34),
            Slot("q3", (0, _HALF_Y, _MID_X, HEIGHT), value_size=150, label_size=34),
            Slot("q4", (_MID_X, _HALF_Y, WIDTH, HEIGHT), value_size=150, label_size=34),
        ),
        separators=(
            (_MID_X, HEADER_H, _MID_X, HEIGHT),
            (0, _HALF_Y, WIDTH, _HALF_Y),
        ),
    ),
    # A compass dial: one slot, bound to a heading-type metric.
    "compass": Template(
        id="compass",
        name="Compass dial",
        slots=(
            Slot(
                "dial", (0, HEADER_H, WIDTH, HEIGHT), value_size=96, label_size=32, kind="compass"
            ),
        ),
    ),
}


@dataclass(frozen=True)
class SlotBinding:
    """A slot's metric assignment, with optional label/format overrides."""

    metric: str
    label: str | None = None
    fmt: str = "auto"


@dataclass(frozen=True)
class Screen:
    """A named, ordered, toggleable layout instance."""

    id: str
    name: str
    template: str
    order: int = 0
    enabled: bool = True
    slots: dict[str, SlotBinding] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Seed / default screens
# ---------------------------------------------------------------------------


def default_screens() -> list[Screen]:
    """The screens a fresh install ships with.

    The first screen reproduces the #820 fixed layout, so the ``?screen=``-less
    endpoints render exactly what they did before this feature landed.
    """
    return [
        Screen(
            id="wind-speed",
            name="Wind & Speed",
            template="two-big-four-small",
            order=0,
            slots={
                "big1": SlotBinding("stw"),
                "big2": SlotBinding("vmg"),
                "s1": SlotBinding("twa"),
                "s2": SlotBinding("tws"),
                "s3": SlotBinding("awa"),
                "s4": SlotBinding("aws"),
            },
        ),
        Screen(
            id="big-speed",
            name="Big Speed",
            template="single",
            order=1,
            slots={"main": SlotBinding("stw")},
        ),
        Screen(
            id="compass",
            name="Compass",
            template="compass",
            order=2,
            slots={"dial": SlotBinding("hdg")},
        ),
        Screen(
            id="numbers",
            name="Numbers",
            template="quad",
            order=3,
            slots={
                "q1": SlotBinding("twa"),
                "q2": SlotBinding("tws"),
                "q3": SlotBinding("stw"),
                "q4": SlotBinding("vmg"),
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Parsing / validation / serialization
# ---------------------------------------------------------------------------


class ScreenConfigError(ValueError):
    """Raised when a screen JSON payload is structurally or semantically bad."""


def _parse_slot(raw: object, *, screen_id: str, slot_id: str, template: Template) -> SlotBinding:
    if not isinstance(raw, dict):
        raise ScreenConfigError(f"screen {screen_id!r} slot {slot_id!r}: must be an object")
    if template.slot(slot_id) is None:
        valid = ", ".join(template.slot_ids())
        raise ScreenConfigError(
            f"screen {screen_id!r}: slot {slot_id!r} is not in template "
            f"{template.id!r} (valid: {valid})"
        )
    metric = raw.get("metric")
    if not isinstance(metric, str) or metric not in METRICS:
        raise ScreenConfigError(f"screen {screen_id!r} slot {slot_id!r}: unknown metric {metric!r}")
    fmt = raw.get("fmt", "auto")
    if not isinstance(fmt, str) or fmt not in FORMATS:
        raise ScreenConfigError(f"screen {screen_id!r} slot {slot_id!r}: bad format {fmt!r}")
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise ScreenConfigError(f"screen {screen_id!r} slot {slot_id!r}: label must be a string")
    return SlotBinding(metric=metric, label=label, fmt=fmt)


def parse_screen(raw: object) -> Screen:
    """Validate and build a single :class:`Screen` from a plain dict.

    Raises :class:`ScreenConfigError` on any structural or semantic problem so
    callers can surface a 400 with a specific message.
    """
    if not isinstance(raw, dict):
        raise ScreenConfigError("screen must be an object")

    screen_id = raw.get("id")
    if not isinstance(screen_id, str) or not screen_id.strip():
        raise ScreenConfigError("screen id must be a non-empty string")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScreenConfigError(f"screen {screen_id!r}: name must be a non-empty string")

    template_id = raw.get("template")
    if not isinstance(template_id, str) or template_id not in TEMPLATES:
        raise ScreenConfigError(f"screen {screen_id!r}: unknown template {template_id!r}")
    template = TEMPLATES[template_id]

    order = raw.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool):
        raise ScreenConfigError(f"screen {screen_id!r}: order must be an integer")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ScreenConfigError(f"screen {screen_id!r}: enabled must be a boolean")

    raw_slots = raw.get("slots", {})
    if not isinstance(raw_slots, dict):
        raise ScreenConfigError(f"screen {screen_id!r}: slots must be an object")

    slots: dict[str, SlotBinding] = {}
    for slot_id, slot_raw in raw_slots.items():
        slots[slot_id] = _parse_slot(
            slot_raw, screen_id=screen_id, slot_id=slot_id, template=template
        )

    return Screen(
        id=screen_id,
        name=name,
        template=template_id,
        order=order,
        enabled=enabled,
        slots=slots,
    )


def parse_screens(raw: str | list[Any]) -> list[Screen]:
    """Validate a JSON string (or already-decoded list) into ``Screen``s.

    Enforces unique screen ids.  Raises :class:`ScreenConfigError` on any
    problem.
    """
    data: object
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScreenConfigError(f"invalid JSON: {exc}") from exc
    else:
        data = raw

    if not isinstance(data, list):
        raise ScreenConfigError("screens config must be a JSON list")

    screens = [parse_screen(item) for item in data]

    seen: set[str] = set()
    for s in screens:
        if s.id in seen:
            raise ScreenConfigError(f"duplicate screen id {s.id!r}")
        seen.add(s.id)
    return screens


def screen_to_dict(screen: Screen) -> dict[str, object]:
    """Serialize a :class:`Screen` to a JSON-safe dict (round-trips parsing)."""
    return {
        "id": screen.id,
        "name": screen.name,
        "template": screen.template,
        "order": screen.order,
        "enabled": screen.enabled,
        "slots": {
            slot_id: {"metric": b.metric, "label": b.label, "fmt": b.fmt}
            for slot_id, b in screen.slots.items()
        },
    }


def screens_to_json(screens: list[Screen]) -> str:
    return json.dumps([screen_to_dict(s) for s in screens])


def enabled_screens(screens: list[Screen]) -> list[Screen]:
    """Enabled screens, ordered by ``order`` then ``id`` (stable device menu)."""
    return sorted((s for s in screens if s.enabled), key=lambda s: (s.order, s.id))


def resolve_screen(screens: list[Screen], screen_id: str | None) -> Screen | None:
    """Pick the screen to render.

    With an explicit *screen_id*, return that screen (enabled or not) or None
    if it does not exist.  Without one, return the default: the first *enabled*
    screen by order, or None if none are enabled.
    """
    if screen_id is not None:
        for s in screens:
            if s.id == screen_id:
                return s
        return None
    ordered = enabled_screens(screens)
    return ordered[0] if ordered else None
