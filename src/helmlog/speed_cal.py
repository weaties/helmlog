"""Heel-dependent boatspeed (STW) correction for the off-center paddlewheel.

Corvo's paddlewheel sits ~10 cm to port of centerline feeding a display-only
B&G Triton², so under heel it reads differently on port vs. starboard tack
(#810). Port tack heels the boat to starboard, lifting the port-offset wheel
so it over-reads; starboard tack immerses it. Current-corrected
GPS-through-water is symmetric tack-to-tack, confirming this is a sensor
artifact, not real boatspeed.

The correction is applied at **read time** in the derivation path — raw
``speeds``/``attitudes`` rows are never mutated:

    k(heel) = a + b * heel_signed          # heel_signed = attitudes.heel_deg,
                                           # +ve = starboard-down = port tack
    corrected_STW = raw_STW / k(heel)

``k`` is a divisor because the paddlewheel *over-reads*; ``k > 1`` scales the
reading down. The fitted slope ``b`` is expected **positive**: port tack
(heel > 0) lifts the wheel so it over-reads most and needs the largest
divisor. ``a`` is a configurable base factor (default 1.0) — the mean
over-read is expected to be carried by the Triton²'s own global cal, so the
shipped default (a=1.0, b=0.0) is an exact no-op.

Coefficients live in ``boat_settings`` (``speed_cal_base`` /
``speed_cal_heel_slope``) alongside ``leeway_coefficient`` and the compass
offsets, and re-fit per rig/sail era via ``scripts/analysis/calibrate_speed_heel.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# Sane band for the correction factor. A physically plausible paddlewheel cal
# factor sits near 1.0 (±~15%); a divisor outside this band means a bad fit or
# a garbage heel sample, so we fall back to the raw reading rather than emit a
# wildly scaled — or negative/infinite — speed.
_K_MIN = 0.5
_K_MAX = 2.0


def _apply_factor(raw_kts: float, k: float, ctx: str = "") -> float:
    """Divide by ``k`` after a sane-band guard; fall back to raw on a bad ``k``."""
    if not (_K_MIN <= k <= _K_MAX):
        logger.warning(
            "speed_cal: correction factor k={:.4f} out of band [{}, {}]{}; using raw STW",
            k,
            _K_MIN,
            _K_MAX,
            f" ({ctx})" if ctx else "",
        )
        return raw_kts
    return raw_kts / k


def corrected_stw(
    raw_kts: float | None,
    heel_deg: float | None,
    a: float = 1.0,
    b: float = 0.0,
) -> float | None:
    """Return heel-corrected STW, or the raw value when correction can't apply.

    Args:
        raw_kts: Raw paddlewheel speed-through-water (knots), as logged.
        heel_deg: Signed heel (``attitudes.heel_deg``); +ve = starboard-down =
            port tack. ``None`` when no attitude sensor / sample is available.
        a: Base cal factor (``speed_cal_base``). Default 1.0.
        b: Heel slope per degree (``speed_cal_heel_slope``). Default 0.0.

    Behavior:
        * ``raw_kts`` that is ``None``, zero, or negative is passed through
          untouched — there is no meaningful correction of an absent or
          non-positive reading.
        * ``heel_deg`` of ``None`` drops the heel term (``k = a``).
        * A computed factor outside ``[0.5, 2.0]`` falls back to the raw value
          and logs a warning.
        * With the shipped defaults (a=1.0, b=0.0) the result is exactly
          ``raw_kts`` for every input (safe no-op).
    """
    if raw_kts is None or raw_kts <= 0.0:
        return raw_kts

    heel = heel_deg if heel_deg is not None else 0.0
    return _apply_factor(raw_kts, a + b * heel, f"heel={heel_deg}, a={a}, b={b}")


# ---------------------------------------------------------------------------
# TWS × tack table + breeze gate (#810 follow-up)
# ---------------------------------------------------------------------------
#
# Live-data regression showed the off-center paddlewheel's tack bias is not a
# simple function of heel: it *reverses sign* with wind speed (port over-reads
# most at 12–15 kt but under-reads below ~9 kt) AND differs upwind vs downwind,
# so a single heel slope can't fit it. Two data-driven refinements live here:
#
#   * a **breeze gate** — below ``gate_min_tws`` the noisy, sign-reversing
#     light-air regime is left alone (only the base mean factor applies), and
#   * a **TWS × point-of-sail × tack lookup table** — per-(TWS bin, upwind/
#     downwind, tack) factors, the model the data actually supports, used
#     wherever TWS + point-of-sail are known (the polar path).
#
# The heel-linear ``corrected_stw`` above remains the fallback for callers
# without TWS/point-of-sail (the live set/drift compute) and when no bin matches.

# Valid point-of-sail labels for a table entry (must match polar._point_of_sail).
_POINTS_OF_SAIL = ("upwind", "downwind")

# One resolved table entry: (tws_min, tws_max, point_of_sail, port_k, stbd_k).
TableRow = tuple[float, float, str, float, float]


@dataclass(frozen=True)
class SpeedCal:
    """Bundle of STW-correction coefficients, resolved by ``resolve_stw``.

    Attributes:
        base: Mean factor ``a`` (always applied). Default 1.0 (no-op).
        heel_slope: Heel slope ``b`` per degree, used only in the heel-linear
            fallback. Default 0.0.
        gate_min_tws: Below this TWS (kt) the tack/heel term is suppressed and
            only ``base`` applies. 0.0 disables the gate.
        table: Sorted ``(tws_min, tws_max, point_of_sail, port_k, stbd_k)``
            bins; ``tws_max`` exclusive, ``point_of_sail`` in ``upwind`` /
            ``downwind``. Empty → no table.
    """

    base: float = 1.0
    heel_slope: float = 0.0
    gate_min_tws: float = 0.0
    table: tuple[TableRow, ...] = field(default_factory=tuple)


DEFAULT_CAL = SpeedCal()


def parse_table(spec: str | list[dict[str, Any]] | None) -> tuple[TableRow, ...]:
    """Parse a TWS × point-of-sail × tack table from JSON (or a decoded list).

    Each entry is
    ``{"tws_min":.., "tws_max":.., "pos":"upwind"|"downwind", "port":.., "stbd":..}``.
    Rows are returned sorted by ``(tws_min, pos)``. Empty/blank/malformed input —
    including an unknown ``pos`` label — yields an empty table (correction
    disabled) rather than raising: a bad admin value must never take out the
    read path.
    """
    if not spec:
        return ()
    try:
        rows = json.loads(spec) if isinstance(spec, str) else spec
        parsed = []
        for r in rows:
            pos = str(r["pos"])
            if pos not in _POINTS_OF_SAIL:
                raise ValueError(f"unknown point-of-sail {pos!r}")
            parsed.append(
                (
                    float(r["tws_min"]),
                    float(r["tws_max"]),
                    pos,
                    float(r["port"]),
                    float(r["stbd"]),
                )
            )
    except (ValueError, TypeError, KeyError) as e:
        logger.warning(
            "speed_cal: ignoring malformed speed_cal_table ({}): {}", type(e).__name__, e
        )
        return ()
    return tuple(sorted(parsed, key=lambda r: (r[0], r[2])))


def _table_lookup(
    table: tuple[TableRow, ...],
    tws_kts: float,
    point_of_sail: str,
    tack: str,
) -> float | None:
    """Return ``k`` for the bin matching ``tws_kts`` + ``point_of_sail`` on
    ``tack``, or None if no bin matches."""
    for lo, hi, pos, port_k, stbd_k in table:
        if lo <= tws_kts < hi and pos == point_of_sail:
            return port_k if tack == "port" else stbd_k
    return None


def resolve_stw(
    raw_kts: float | None,
    heel_deg: float | None,
    tws_kts: float | None,
    cal: SpeedCal = DEFAULT_CAL,
    point_of_sail: str | None = None,
) -> float | None:
    """Correct STW using the full model: gate → table → heel-linear fallback.

    Precedence:
        1. Invalid ``raw_kts`` (None / ≤ 0) is passed through unchanged.
        2. **Gate:** if ``tws_kts`` is known and below ``gate_min_tws``, apply
           only ``base`` (light air's tack bias is noisy and sign-reversing).
        3. **Table:** if a table is configured and ``tws_kts``, ``heel_deg`` and
           ``point_of_sail`` are all known, use the matching
           ``(TWS bin, point-of-sail, tack)`` factor.
        4. **Fallback:** heel-linear ``corrected_stw`` (base + slope·heel) when
           there's no table, a missing input, or no matching bin.
    """
    if raw_kts is None or raw_kts <= 0.0:
        return raw_kts

    if tws_kts is not None and cal.gate_min_tws > 0.0 and tws_kts < cal.gate_min_tws:
        return _apply_factor(raw_kts, cal.base, f"gated tws={tws_kts}<{cal.gate_min_tws}")

    if cal.table and tws_kts is not None and heel_deg is not None and point_of_sail is not None:
        tack = "port" if heel_deg > 0 else "stbd"
        k = _table_lookup(cal.table, tws_kts, point_of_sail, tack)
        if k is not None:
            return _apply_factor(raw_kts, k, f"table tws={tws_kts} {point_of_sail} tack={tack}")

    return corrected_stw(raw_kts, heel_deg, cal.base, cal.heel_slope)
