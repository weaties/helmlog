"""Exponential moving average smoothing for live instrument values.

Server-side smoothing applied in ``Storage.update_live`` so the WS
broadcast and the in-memory ``self._live`` cache reflect a smoothed
value. SQLite writes are unaffected — the historical replay path still
sees raw samples and can do its own averaging if it wants to.

The math
--------
Variable-dt EMA so smoothing behaves consistently regardless of how
often instrument records arrive::

    alpha = dt / (tau + dt)
    smoothed = alpha * raw + (1 - alpha) * smoothed_prev

``tau`` is the time constant in seconds — roughly the lag the user will
see. With ``tau=5`` the smoothed signal reaches ~63 % of a step input
after 5 s and ~95 % after 15 s.

Angles
------
Bearings (TWD, TWA, AWA, COG, HDG) wrap at 360°, so a naive scalar EMA
crosses the wrap boundary by going the long way around. ``AngleEma``
smooths the (sin, cos) components separately and reconstructs the
direction with ``atan2`` — the same convention used by the wind/current
overlays.

Configuration
-------------
Time constants live in the ``app_settings`` table under keys of the
form ``smoothing.<channel>.tau_s`` so admins can tune them without
restarting the service. ``SmoothingConfig.from_storage`` loads the
current values; ``refresh`` re-reads them and rebuilds the smoothers
in place (preserving the last smoothed value so a tau change doesn't
glitch the gauges).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from loguru import logger

# Default time constants (seconds). Overrideable via app_settings rows
# named ``smoothing.<channel>.tau_s``.
DEFAULT_TAUS: dict[str, float] = {
    "tws_kts": 5.0,
    "twa_deg": 5.0,
    "twd_deg": 5.0,
    "aws_kts": 5.0,
    "awa_deg": 5.0,
    "sog_kts": 3.0,
    "bsp_kts": 3.0,
    "heading_deg": 2.0,
    "cog_deg": 2.0,
    # Set / drift derive from sog/cog/stw/hdg, which are themselves
    # smoothed; applying a longer EMA on top damps residual jitter from
    # the four-input vector subtraction (a few degrees of HDG drift can
    # swing drift by more than a knot — see #729).
    "set_deg": 8.0,
    "drift_kts": 8.0,
}

# Channels whose values are bearings (0–360°) and need vector smoothing
# rather than scalar.
ANGLE_CHANNELS: frozenset[str] = frozenset(
    {"twa_deg", "twd_deg", "awa_deg", "heading_deg", "cog_deg", "set_deg"}
)

# Floor on tau so a stuck-zero setting can't cause divide-by-zero or
# pass-through behaviour (defeats the purpose of smoothing).
_MIN_TAU_S: float = 0.05


@dataclass
class Ema:
    """Scalar exponential moving average with variable dt."""

    tau_s: float
    value: float | None = None
    last_t: float | None = None

    def update(self, raw: float, t: float | None = None) -> float:
        now = t if t is not None else time.monotonic()
        tau = max(self.tau_s, _MIN_TAU_S)
        if self.value is None or self.last_t is None:
            self.value = raw
            self.last_t = now
            return raw
        dt = now - self.last_t
        if dt <= 0:
            return self.value
        alpha = dt / (tau + dt)
        self.value = alpha * raw + (1 - alpha) * self.value
        self.last_t = now
        return self.value


@dataclass
class AngleEma:
    """Vector EMA on (sin, cos) so wrap-around at 360° is handled cleanly."""

    tau_s: float
    _sin: float | None = None
    _cos: float | None = None
    last_t: float | None = None

    def update(self, raw_deg: float, t: float | None = None) -> float:
        now = t if t is not None else time.monotonic()
        tau = max(self.tau_s, _MIN_TAU_S)
        rad = math.radians(raw_deg)
        s_raw, c_raw = math.sin(rad), math.cos(rad)
        if self._sin is None or self._cos is None or self.last_t is None:
            self._sin, self._cos = s_raw, c_raw
            self.last_t = now
            return raw_deg % 360.0
        dt = now - self.last_t
        if dt <= 0:
            out = math.degrees(math.atan2(self._sin, self._cos)) % 360.0
            return out
        alpha = dt / (tau + dt)
        self._sin = alpha * s_raw + (1 - alpha) * self._sin
        self._cos = alpha * c_raw + (1 - alpha) * self._cos
        self.last_t = now
        return math.degrees(math.atan2(self._sin, self._cos)) % 360.0


@dataclass
class SmoothingConfig:
    """Container for per-channel smoothers, kept in Storage."""

    smoothers: dict[str, Ema | AngleEma] = field(default_factory=dict)

    @classmethod
    def from_taus(cls, taus: dict[str, float]) -> SmoothingConfig:
        cfg = cls()
        for ch, tau in taus.items():
            cfg.smoothers[ch] = AngleEma(tau_s=tau) if ch in ANGLE_CHANNELS else Ema(tau_s=tau)
        return cfg

    def update(self, channel: str, raw: float) -> float:
        """Push a raw value through the smoother. Channels with no
        configured smoother also pass through — useful for fields like
        rudder/heel where smoothing isn't wanted. Caller handles None at
        the boundary so the return type stays float.
        """
        sm = self.smoothers.get(channel)
        if sm is None:
            return raw
        try:
            return sm.update(float(raw))
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("smoothing update failed for {}: {}", channel, exc)
            return raw

    def set_tau(self, channel: str, tau_s: float) -> None:
        """Update the time constant for ``channel`` in place — keeps the
        last smoothed value so the gauge doesn't glitch on a setting change.
        """
        sm = self.smoothers.get(channel)
        if sm is None:
            self.smoothers[channel] = (
                AngleEma(tau_s=tau_s) if channel in ANGLE_CHANNELS else Ema(tau_s=tau_s)
            )
        else:
            sm.tau_s = max(tau_s, _MIN_TAU_S)


def parse_tau(raw: str | None, default: float) -> float:
    """Parse a tau value from the app_settings table. Falls back to default
    on any malformed input — the live path can't tolerate exceptions.
    """
    if raw is None:
        return default
    try:
        v = float(raw)
        if v <= 0 or math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def apply_ema_to_series(
    samples: list[tuple[float, float]],
    tau_s: float,
    is_angle: bool = False,
) -> list[float]:
    """Apply a variable-dt EMA over ``samples`` (already sorted by ts).

    ``samples`` is a list of ``(epoch_seconds, value)`` tuples; the
    returned list aligns 1:1 with the inputs. ``is_angle=True`` uses the
    same (sin, cos) vector smoother as :class:`AngleEma` so values wrap
    cleanly at 360°.

    This mirrors the live path's per-tick semantics — the analysis path
    can replay a session and produce identical smoothing to what the
    live broadcast would have shown if the same τ had been active.
    Stateless: a fresh smoother is allocated per call, so callers can
    use it on per-session series without worrying about reuse.
    """
    if not samples:
        return []
    sm: Ema | AngleEma = AngleEma(tau_s=tau_s) if is_angle else Ema(tau_s=tau_s)
    return [sm.update(value, t=ts) for ts, value in samples]


def tau_hash(taus: dict[str, float]) -> str:
    """Stable 16-char hex digest of a per-channel tau map (#749).

    Folded into cache keys so a τ change busts both the per-session
    enrichment cache and the cross-session overlay T2 cache. Sorted
    keys + fixed float repr keeps the hash deterministic.
    """
    import hashlib

    parts = [f"{k}={float(v):.6f}" for k, v in sorted(taus.items())]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


__all__ = [
    "ANGLE_CHANNELS",
    "DEFAULT_TAUS",
    "AngleEma",
    "Ema",
    "SmoothingConfig",
    "apply_ema_to_series",
    "parse_tau",
    "tau_hash",
]
