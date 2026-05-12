"""Tests for derived water current (set/drift) from boat velocity vectors.

Convention: "set" is the compass direction the current is flowing *toward*
(oceanographic), degrees 0..360 with 0=N, 90=E. "drift" is current speed in knots.

Derivation:
    v_ground = (SOG, COG)  # boat velocity over ground
    v_water  = (STW, HDG)  # boat velocity through water
    current  = v_ground - v_water
"""

from __future__ import annotations

import math

import pytest

from helmlog.current import compute_set_drift


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


class TestComputeSetDrift:
    def test_no_current_when_ground_matches_water(self) -> None:
        set_deg, drift_kts = compute_set_drift(sog=5.0, cog=0.0, stw=5.0, hdg=0.0)
        assert drift_kts == pytest.approx(0.0, abs=1e-9)
        # Set direction is undefined when drift is zero; implementation returns 0.0.
        assert set_deg == 0.0

    def test_pure_eastward_push_on_northbound_boat(self) -> None:
        # Boat steering 5 kt due north through water, but made good 5 kt
        # due east over ground. Water pushed the boat east and south equally:
        # current = (5,0) ground - (0,5) water = (5,-5). Toward SE, mag √50.
        set_deg, drift_kts = compute_set_drift(sog=5.0, cog=90.0, stw=5.0, hdg=0.0)
        assert drift_kts == pytest.approx(math.sqrt(50), rel=1e-6)
        assert set_deg == pytest.approx(135.0, abs=1e-6)

    def test_head_current_slows_boat(self) -> None:
        # Boat steering north at 5 kt water, only making 3 kt COG due north.
        # Current = (0,3)-(0,5) = (0,-2). Toward south, 2 kt.
        set_deg, drift_kts = compute_set_drift(sog=3.0, cog=0.0, stw=5.0, hdg=0.0)
        assert drift_kts == pytest.approx(2.0, abs=1e-9)
        assert set_deg == pytest.approx(180.0, abs=1e-6)

    def test_drifting_with_stw_zero(self) -> None:
        # Sails down, STW=0, but making 1 kt south over ground.
        set_deg, drift_kts = compute_set_drift(sog=1.0, cog=180.0, stw=0.0, hdg=0.0)
        assert drift_kts == pytest.approx(1.0, abs=1e-9)
        assert set_deg == pytest.approx(180.0, abs=1e-6)

    def test_wraps_to_360_range(self) -> None:
        # Boat steering east through water, pushed slightly north.
        # water = (5,0) (east component, north component)... using math convention
        # with compass angles, we trust the function to wrap [0, 360).
        set_deg, _ = compute_set_drift(sog=5.05, cog=85.0, stw=5.0, hdg=90.0)
        assert 0.0 <= set_deg < 360.0

    def test_returns_none_for_missing_inputs(self) -> None:
        assert compute_set_drift(sog=None, cog=0.0, stw=5.0, hdg=0.0) is None
        assert compute_set_drift(sog=5.0, cog=None, stw=5.0, hdg=0.0) is None
        assert compute_set_drift(sog=5.0, cog=0.0, stw=None, hdg=0.0) is None
        assert compute_set_drift(sog=5.0, cog=0.0, stw=5.0, hdg=None) is None


class TestLeewayCorrection:
    """Without leeway correction, the derived current flips direction on
    every tack — the un-corrected heading vector misses the leeward slide
    that flips sides with the boat. Leeway correction (lee = K·heel/STW²)
    keeps the derived current consistent across tacks.
    """

    def test_leeway_correction_keeps_set_consistent_across_tack(self) -> None:
        # Boat sailing through a 1 kt south-flowing current (set=180°).
        # On port tack: HDG=315° (NW), heeled to starboard (heel = +20°).
        # On starboard tack: HDG=045° (NE), heeled to port (heel = -20°).
        # STW = 6.0 on both tacks. Leeway K = 12.
        #
        # With leeway K=12 and heel=±20° at stw=6: lee = 12·20/36 ≈ 6.7°
        # downwind of heading on each tack.
        #
        # Compose ground vectors so that the *true* current is south at 1 kt
        # for both tacks. Then assert the recovered set is close to 180°
        # in both cases — proves the per-tack flip is gone.
        import math as _m

        def _ground(
            stw: float, eff_hdg_deg: float, drift: float, set_deg: float
        ) -> tuple[float, float]:
            r = _m.radians(eff_hdg_deg)
            n_w, e_w = stw * _m.cos(r), stw * _m.sin(r)
            r2 = _m.radians(set_deg)
            n_c, e_c = drift * _m.cos(r2), drift * _m.sin(r2)
            n_g, e_g = n_w + n_c, e_w + e_c
            sog = _m.hypot(n_g, e_g)
            cog = _m.degrees(_m.atan2(e_g, n_g)) % 360.0
            return sog, cog

        K = 12.0
        STW = 6.0
        # Port tack: HDG=315°, heel=+20° → lee=+6.7°, eff_hdg=321.7°
        port_eff = (315.0 + K * 20.0 / STW**2) % 360.0
        sog_p, cog_p = _ground(STW, port_eff, 1.0, 180.0)
        sd_p = compute_set_drift(
            sog=sog_p, cog=cog_p, stw=STW, hdg=315.0, heel_deg=20.0, leeway_k=K
        )
        assert sd_p is not None
        set_p, drift_p = sd_p
        # Starboard tack: mirror
        stbd_eff = (45.0 + K * (-20.0) / STW**2) % 360.0
        sog_s, cog_s = _ground(STW, stbd_eff, 1.0, 180.0)
        sd_s = compute_set_drift(
            sog=sog_s, cog=cog_s, stw=STW, hdg=45.0, heel_deg=-20.0, leeway_k=K
        )
        assert sd_s is not None
        set_s, drift_s = sd_s

        # Both tacks should recover ~180° set ± a degree, ~1 kt drift.
        for tag, val in (("port", set_p), ("stbd", set_s)):
            norm = ((val - 180 + 180) % 360) - 180
            assert abs(norm) < 1.0, f"{tag} tack set={val} not within 1° of 180°"
        assert drift_p == pytest.approx(1.0, abs=0.05)
        assert drift_s == pytest.approx(1.0, abs=0.05)

    def test_without_leeway_correction_set_flips_at_tack(self) -> None:
        """Sanity: without K, the same scenario produces *different*
        recovered set on each tack — the bug we're fixing."""
        import math as _m

        STW = 6.0
        # Compose ground vectors with NO leeway correction in synthesis,
        # but the boat IS slipping leeward in reality. Pretend the
        # "true" effective heading is HDG offset by leeway, and the
        # detector sees only HDG without correction.
        K = 12.0
        port_eff = (315.0 + K * 20.0 / STW**2) % 360.0
        n_w_p = STW * _m.cos(_m.radians(port_eff))
        e_w_p = STW * _m.sin(_m.radians(port_eff))
        n_c, e_c = 1.0 * _m.cos(_m.radians(180.0)), 1.0 * _m.sin(_m.radians(180.0))
        sog_p = _m.hypot(n_w_p + n_c, e_w_p + e_c)
        cog_p = _m.degrees(_m.atan2(e_w_p + e_c, n_w_p + n_c)) % 360.0

        stbd_eff = (45.0 + K * (-20.0) / STW**2) % 360.0
        n_w_s = STW * _m.cos(_m.radians(stbd_eff))
        e_w_s = STW * _m.sin(_m.radians(stbd_eff))
        sog_s = _m.hypot(n_w_s + n_c, e_w_s + e_c)
        cog_s = _m.degrees(_m.atan2(e_w_s + e_c, n_w_s + n_c)) % 360.0

        # Without K, the recovered sets diverge wildly on opposite tacks.
        sd_p = compute_set_drift(sog=sog_p, cog=cog_p, stw=STW, hdg=315.0)
        sd_s = compute_set_drift(sog=sog_s, cog=cog_s, stw=STW, hdg=45.0)
        assert sd_p is not None and sd_s is not None
        set_p_uncorr = sd_p[0]
        set_s_uncorr = sd_s[0]
        # The two should differ by tens of degrees — proves the bug
        # exists when leeway isn't applied.
        diff = abs(((set_p_uncorr - set_s_uncorr + 180) % 360) - 180)
        assert diff > 30.0, (
            f"expected large set divergence without leeway correction, "
            f"got {set_p_uncorr}° vs {set_s_uncorr}° (diff={diff})"
        )

    def test_leeway_k_zero_is_no_op(self) -> None:
        """Backward-compat: K=0 (or unset) → identical to plain compute."""
        sd_with = compute_set_drift(
            sog=5.0, cog=10.0, stw=5.0, hdg=0.0, heel_deg=15.0, leeway_k=0.0
        )
        sd_without = compute_set_drift(sog=5.0, cog=10.0, stw=5.0, hdg=0.0)
        assert sd_with == sd_without

    def test_leeway_low_speed_floor_avoids_singularity(self) -> None:
        """At very low STW, the K/STW² factor would explode. The floor
        at STW=1 keeps the correction bounded."""
        sd = compute_set_drift(sog=0.5, cog=10.0, stw=0.1, hdg=0.0, heel_deg=20.0, leeway_k=12.0)
        assert sd is not None
        set_v, drift_v = sd
        assert 0.0 <= set_v < 360.0
        assert math.isfinite(drift_v)
