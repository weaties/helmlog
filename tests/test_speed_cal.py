"""Tests for the heel-dependent STW correction (#810).

Sign convention under test: ``heel_deg`` positive = starboard-down = port
tack, and the paddlewheel over-reads *most* on port tack, so the fitted
slope ``b`` is positive and the correction divides the reading down more as
heel grows.
"""

from __future__ import annotations

import pytest

from helmlog.speed_cal import corrected_stw


class TestIdentityDefault:
    """R4 — shipped defaults (a=1.0, b=0.0) are an exact no-op."""

    @pytest.mark.parametrize("heel", [-25.0, -10.0, 0.0, 10.0, 25.0, None])
    @pytest.mark.parametrize("raw", [3.0, 5.5, 6.789, 8.0])
    def test_default_is_exact_identity(self, raw: float, heel: float | None) -> None:
        assert corrected_stw(raw, heel) == raw

    def test_base_one_zero_slope_explicit(self) -> None:
        assert corrected_stw(6.0, 12.0, a=1.0, b=0.0) == 6.0


class TestCorrectionMath:
    """R1 — corrected = raw / (a + b * heel)."""

    def test_base_only_scales_by_a(self) -> None:
        # a=1.09 removes a ~9% over-read; heel term off.
        assert corrected_stw(6.54, 0.0, a=1.09, b=0.0) == pytest.approx(6.0)

    def test_heel_term_applied_with_sign(self) -> None:
        # k = 1.0 + 0.01 * 10 = 1.1  →  6.6 / 1.1 = 6.0
        assert corrected_stw(6.6, 10.0, a=1.0, b=0.01) == pytest.approx(6.0)

    def test_starboard_heel_smaller_divisor(self) -> None:
        # heel < 0 (starboard tack) → k < a → less reduction than port.
        # k = 1.0 + 0.01 * -10 = 0.9  →  5.4 / 0.9 = 6.0
        assert corrected_stw(5.4, -10.0, a=1.0, b=0.01) == pytest.approx(6.0)


class TestSignAndMonotonicity:
    """The port-over-reads-most physics: corrected STW falls as heel rises."""

    def test_monotonic_decreasing_in_heel_for_positive_slope(self) -> None:
        raw = 6.0
        vals = [corrected_stw(raw, h, a=1.0, b=0.01) for h in (-20, -10, 0, 10, 20)]
        assert vals is not None
        # strictly decreasing
        assert all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))  # type: ignore[operator]

    def test_port_corrected_below_starboard_at_equal_raw(self) -> None:
        # Same raw reading on each tack: port (heel>0) is scaled down more.
        port = corrected_stw(6.0, 15.0, a=1.0, b=0.008)
        stbd = corrected_stw(6.0, -15.0, a=1.0, b=0.008)
        assert port is not None and stbd is not None
        assert port < stbd


class TestMissingHeel:
    """R2 — missing heel drops the heel term (k = a)."""

    def test_none_heel_uses_base_only(self) -> None:
        assert corrected_stw(6.54, None, a=1.09, b=0.05) == pytest.approx(6.0)

    def test_none_heel_with_default_base_is_identity(self) -> None:
        assert corrected_stw(6.0, None, a=1.0, b=0.05) == 6.0


class TestInvalidSpeed:
    """R5 — absent / non-positive speed passes through untouched."""

    def test_none_speed_passthrough(self) -> None:
        assert corrected_stw(None, 10.0, a=1.09, b=0.01) is None

    def test_zero_speed_passthrough(self) -> None:
        assert corrected_stw(0.0, 10.0, a=1.09, b=0.01) == 0.0

    def test_negative_speed_passthrough(self) -> None:
        assert corrected_stw(-2.0, 10.0, a=1.09, b=0.01) == -2.0


class TestSaneBandGuard:
    """R3 — a divisor outside [0.5, 2.0] falls back to raw (no wild scaling)."""

    def test_factor_below_floor_falls_back(self) -> None:
        # k = 1.0 + 0.05 * -20 = 0.0  → out of band → raw returned
        assert corrected_stw(6.0, -20.0, a=1.0, b=0.05) == 6.0

    def test_factor_above_ceiling_falls_back(self) -> None:
        # k = 1.0 + 0.1 * 20 = 3.0 → out of band → raw returned
        assert corrected_stw(6.0, 20.0, a=1.0, b=0.1) == 6.0

    def test_factor_just_inside_band_applies(self) -> None:
        # k = 0.5 exactly is still applied (inclusive bound)
        assert corrected_stw(3.0, -10.0, a=1.0, b=0.05) == pytest.approx(6.0)
