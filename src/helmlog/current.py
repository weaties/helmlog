"""Water current derivation from boat velocity vectors.

The boat's velocity through the water (STW, HDG) and over the ground
(SOG, COG) differ by the water current acting on the hull. Subtracting
the two vectors yields the current the boat experienced at that moment.

Convention: "set" is the compass direction the current is flowing *toward*
(oceanographic), degrees 0..360 with 0=N, 90=E. "drift" is current speed
in knots.
"""

from __future__ import annotations

import math


def _polar_to_ne(speed: float, compass_deg: float) -> tuple[float, float]:
    rad = math.radians(compass_deg)
    return (speed * math.cos(rad), speed * math.sin(rad))


def compute_set_drift(
    sog: float | None,
    cog: float | None,
    stw: float | None,
    hdg: float | None,
    heel_deg: float | None = None,
    leeway_k: float | None = None,
) -> tuple[float, float] | None:
    """Return (set_deg, drift_kts) or None if any required input is missing.

    set_deg is the direction the current flows *toward*, 0..360.
    When drift is effectively zero, set_deg is reported as 0.0.

    Leeway correction
    -----------------
    STW is measured along the boat's heading, but a sailboat actually
    moves through the water at an angle to its heading because of
    leeward slip from heel. Without correcting for that, the derived
    "current" appears to flip direction every tack — the un-corrected
    heading vector misses the leeward slide on each side.

    When ``heel_deg`` and ``leeway_k`` are both supplied, the heading
    used for the water-velocity vector is shifted by

        lee_deg = leeway_k * heel_deg / max(stw, 1.0)**2

    (the standard B&G formula). Sign of leeway follows sign of heel,
    which flips with the tack — so the corrected current stays
    consistent across maneuvers.

    The ``stw**2`` in the denominator means leeway falls off rapidly as
    the boat speeds up, and the ``max(..., 1.0)`` floor avoids the
    singularity at low STW where leeway is meaningless anyway.

    ``leeway_k`` is boat-specific (typically 8–15 for a 30–40 ft
    sailboat). On HelmLog it lives in ``boat_settings.leeway_coefficient``.
    """
    if sog is None or cog is None or stw is None or hdg is None:
        return None

    if heel_deg is not None and leeway_k is not None and leeway_k != 0.0:
        lee_deg = leeway_k * heel_deg / (max(abs(stw), 1.0) ** 2)
        hdg = (hdg + lee_deg) % 360.0

    n_g, e_g = _polar_to_ne(sog, cog)
    n_w, e_w = _polar_to_ne(stw, hdg)
    n_c, e_c = n_g - n_w, e_g - e_w

    drift = math.hypot(n_c, e_c)
    if drift < 1e-9:
        return (0.0, 0.0)
    set_deg = math.degrees(math.atan2(e_c, n_c)) % 360.0
    return (set_deg, drift)
