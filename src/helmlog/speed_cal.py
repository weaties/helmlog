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

from loguru import logger

# Sane band for the correction factor. A physically plausible paddlewheel cal
# factor sits near 1.0 (±~15%); a divisor outside this band means a bad fit or
# a garbage heel sample, so we fall back to the raw reading rather than emit a
# wildly scaled — or negative/infinite — speed.
_K_MIN = 0.5
_K_MAX = 2.0


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
    k = a + b * heel

    if not (_K_MIN <= k <= _K_MAX):
        logger.warning(
            "speed_cal: correction factor k={:.4f} out of band [{}, {}] "
            "(heel={}, a={}, b={}); using raw STW",
            k,
            _K_MIN,
            _K_MAX,
            heel_deg,
            a,
            b,
        )
        return raw_kts

    return raw_kts / k
