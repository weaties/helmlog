"""Unit tests for instrument-value smoothing (#727)."""

from __future__ import annotations

import math

import pytest

from helmlog.smoothing import (
    DEFAULT_TAUS,
    AngleEma,
    Ema,
    SmoothingConfig,
    parse_tau,
)


def test_ema_first_sample_passes_through() -> None:
    """First raw value seeds the smoother — there's nothing to blend with yet."""
    e = Ema(tau_s=5.0)
    assert e.update(10.0, t=0.0) == 10.0


def test_ema_step_response_approaches_target() -> None:
    """tau=1s: after ~5 tau the smoothed value is within 1% of a step input."""
    e = Ema(tau_s=1.0)
    e.update(0.0, t=0.0)
    # Step to 10.0 sustained at 0.1 s cadence.
    last = 0.0
    for i in range(1, 60):  # 6 s ≈ 6 tau
        last = e.update(10.0, t=i * 0.1)
    assert last == pytest.approx(10.0, abs=0.1)


def test_ema_short_tau_responds_faster_than_long_tau() -> None:
    """Sanity: tau=1s tracks a step faster than tau=10s at the same dt."""
    fast = Ema(tau_s=1.0)
    slow = Ema(tau_s=10.0)
    fast.update(0.0, t=0.0)
    slow.update(0.0, t=0.0)
    for i in range(1, 11):  # 1 s of step input at 0.1 s cadence
        fast.update(10.0, t=i * 0.1)
        slow.update(10.0, t=i * 0.1)
    assert fast.value > slow.value > 0


def test_ema_clamps_min_tau() -> None:
    """tau=0 is treated as a tiny positive number — no divide-by-zero, but
    the smoother still applies non-trivial smoothing if dt is very small."""
    e = Ema(tau_s=0.0)
    e.update(0.0, t=0.0)
    out = e.update(10.0, t=1.0)  # large dt → alpha near 1, fast catch-up
    assert out == pytest.approx(10.0, abs=0.5)


def test_ema_zero_dt_returns_previous_value() -> None:
    """Two updates at the same monotonic time → smoothed value unchanged."""
    e = Ema(tau_s=5.0)
    e.update(10.0, t=0.0)
    out = e.update(20.0, t=0.0)
    assert out == 10.0


def test_angle_ema_handles_wrap_at_360() -> None:
    """Smoothing 359° → 1° must not swing through 180° — the vector form
    crosses the wrap boundary directly. Average lands near 0°."""
    a = AngleEma(tau_s=1.0)
    a.update(359.0, t=0.0)
    out = a.update(1.0, t=1.0)  # alpha=0.5
    # Expected: vector mean of unit vectors at 359° and 1° is ~0°.
    # Allow some slack for the alpha ≈ 0.5 weighting.
    norm = ((out + 180) % 360) - 180
    assert abs(norm) < 5.0, f"angle EMA crossed 360 the long way: got {out}°"


def test_angle_ema_180_opposite_inputs_average_to_one_end() -> None:
    """Two opposite directions (0° and 180°) average to drift back toward
    one of them rather than collapsing to zero magnitude. The exact value
    depends on alpha; we just check it stays in 0..360 and isn't NaN."""
    a = AngleEma(tau_s=1.0)
    a.update(0.0, t=0.0)
    out = a.update(180.0, t=1.0)
    assert 0.0 <= out < 360.0
    assert not math.isnan(out)


def test_smoothing_config_dispatches_angle_vs_scalar() -> None:
    """Channels listed in ANGLE_CHANNELS get an AngleEma; others get Ema."""
    cfg = SmoothingConfig.from_taus({"tws_kts": 5.0, "twa_deg": 5.0})
    assert isinstance(cfg.smoothers["tws_kts"], Ema)
    assert isinstance(cfg.smoothers["twa_deg"], AngleEma)


def test_smoothing_config_passes_through_unknown_channel() -> None:
    """A channel without a smoother (e.g. rudder_deg) just returns the raw."""
    cfg = SmoothingConfig.from_taus({"tws_kts": 5.0})
    assert cfg.update("rudder_deg", 12.5) == 12.5


def test_set_tau_preserves_state() -> None:
    """Changing tau on the fly must not glitch the gauge — the last
    smoothed value is preserved, only the time constant changes."""
    cfg = SmoothingConfig.from_taus({"tws_kts": 5.0})
    cfg.update("tws_kts", 10.0)  # seeds the EMA at 10
    cfg.set_tau("tws_kts", 1.0)
    sm = cfg.smoothers["tws_kts"]
    assert isinstance(sm, Ema)
    assert sm.value == 10.0
    assert sm.tau_s == 1.0


def test_set_tau_creates_smoother_for_unknown_channel() -> None:
    """set_tau on a channel without a smoother creates one (admin can
    configure smoothing for a previously-unsmoothed channel)."""
    cfg = SmoothingConfig()
    cfg.set_tau("twa_deg", 5.0)
    assert isinstance(cfg.smoothers["twa_deg"], AngleEma)
    cfg.set_tau("tws_kts", 5.0)
    assert isinstance(cfg.smoothers["tws_kts"], Ema)


def test_default_taus_cover_expected_channels() -> None:
    """The hard-coded defaults include every channel the GAUGES card binds."""
    expected = {
        "tws_kts",
        "twa_deg",
        "twd_deg",
        "aws_kts",
        "awa_deg",
        "sog_kts",
        "bsp_kts",
        "heading_deg",
        "cog_deg",
    }
    assert expected.issubset(DEFAULT_TAUS.keys())


def test_parse_tau_handles_malformed_input() -> None:
    """parse_tau falls back to default for None, garbage, NaN, and <=0."""
    assert parse_tau(None, 5.0) == 5.0
    assert parse_tau("not-a-number", 5.0) == 5.0
    assert parse_tau("nan", 5.0) == 5.0
    assert parse_tau("0", 5.0) == 5.0
    assert parse_tau("-1.5", 5.0) == 5.0
    assert parse_tau("3.5", 5.0) == 3.5
