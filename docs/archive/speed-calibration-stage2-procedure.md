---
date: 2026-04-30
status: draft procedure (not yet executed)
related: speed-calibration-2026-04-18.md
---

# Speed Calibration — Stage 2 Procedure (Under Sail)

## Why a stage 2

The 2026-04-18 calibration ([speed-calibration-2026-04-18.md](speed-calibration-2026-04-18.md))
ran reciprocal-heading legs **at constant RPM under power, near-zero wind, flat
heel** and concluded the paddlewheel was reading 8% high → set the Triton²
boat-speed correction to 89.34%. That was the right test for the average paddlewheel
over-read at flat-deck conditions, and the data confirms it worked: post-cal
drift/BSP ratio dropped from 18.3% → 15.3% across 42k samples.

What that test could **not** measure, by design:

1. **Heel-induced paddlewheel asymmetry.** Under sail at 12° heel, water flows
   past the through-hull at a different angle than at 0° heel. Paddlewheels are
   sensitive to flow angle and aeration. The cal-day method explicitly averages
   reciprocal headings — so any port/stbd flow bias cancels in the math.
2. **Speed-dependent linearity.** Cal was performed at one steady RPM
   (~6.3 kt true STW). Paddlewheels often have non-linear response above and
   below the cal point.
3. **Sail-trim flow effects.** Hull asymmetries in heeled flow that depend on
   sail trim cannot be measured under bare-pole motoring.

Cross-session asymmetry analysis on 25 sessions of post-cal racing data
(see `/tmp/asymmetry_findings_v2.md` for the full report) finds **starboard
upwind reads ~6–8% slower than port at matched TWS bins**, persistent across
14 of 15 sessions at TWS 12. That's the signal a stage-2 cal is intended to
quantify and correct.

## Goal

Produce per-tack cal factors so that, on matched conditions, port and starboard
upwind BSP read within ±2% of each other. Same target downwind.

## Method

Same reciprocal-pair math as stage 1, but the "reciprocal" pairs are
**port-tack vs starboard-tack** at matched point-of-sail rather than
opposite compass headings under power. Across each pair, the assumption
is that *true through-water speed* is equal, so any difference in displayed
BSP is paddlewheel error.

For each pair:
- `true_STW = (BSP_port + BSP_stbd) / 2`
- `factor_port = true_STW / BSP_port`
- `factor_stbd = true_STW / BSP_stbd`
- New per-tack cal = current_global_cal × factor_<tack>

Corvo's instrument system is B&G Triton² (display-only — no race processor),
so the cal must be applied in HelmLog software. Triton² supports only a
single global boat-speed correction percentage; it has no per-tack STW cal,
no heel-compensation table, and no leeway model. Leave the Triton² global at
89.34% and add a HelmLog-side tack-aware correction (see
"Software-side per-tack correction" below).

## Conditions required

- **Wind**: 8–12 kt, steady within ±1 kt over the test, no significant shifts.
  Light enough to keep heel manageable, heavy enough to load the rig
  representatively. Avoid building or fading breeze — the test takes ~25 min
  total and conclusions assume conditions are stationary.
- **Water**: flat-ish (chop ≤ 0.5 ft). Waves bias paddlewheel readings asymmetrically
  on each tack.
- **Tide**: known steady current (a *constant* current is fine — it cancels in
  the reciprocal-pair math). **Avoid building/slacking tide**; if you must
  test through one, run the upwind pairs and downwind pairs as close together
  in time as possible.
- **Crew**: same crew weight, same trim, same sails as a normal race. The cal
  must reflect race conditions, not delivery conditions.

## Logging setup

This procedure is recorded by HelmLog in normal `helmlog run` mode — no
special configuration. The analysis script discriminates the legs by
timestamp and tack from the recorded data.

Recommended: **start a new race-classifier session** named
`STAGE2-CAL-<date>` so the legs are anchored under one `races.id` and easy
to extract. (`helmlog run` plus mark a manual race start at the moment you
begin Leg 1 below.)

## Procedure

Total time: ~25 minutes of continuous sailing.

### Phase A — upwind pair

1. **Settle on close-hauled port tack.** Trim for race conditions. Wait
   until BSP, heel, and TWA are steady (variance ≤ 5% over 30 s).
2. **Sail steady for 5 minutes** (Leg 1). Same trim throughout — no
   pumping, no helm-feathering experiments.
3. **Tack to starboard.** Settle for **30 seconds** before starting timing
   (post-tack BSP recovery + flow stabilization).
4. **Sail steady for 5 minutes** (Leg 2).

Do **not** alter sail trim, traveler, or runner tension between Legs 1
and 2. The whole point is identical-rig configuration on both tacks.

### Phase B — downwind pair (immediately after Phase A)

5. **Bear away to a deep run on starboard** (TWA ~150°). If flying a kite
   for racing, fly the kite. If two-sail running, two-sail run. Match the
   downwind setup you actually race.
6. **Sail steady for 5 minutes** (Leg 3).
7. **Gybe to port.** Settle for **30 seconds**.
8. **Sail steady for 5 minutes** (Leg 4).

If conditions held, you now have one reciprocal pair upwind and one
downwind, all in ~25 min — close enough that tide and wind have not
materially changed.

### Optional Phase C — second upwind pair (if conditions allow)

Repeat Phase A immediately after Phase B. Two upwind pairs let you check
internal consistency — like the W/E vs N/S agreement check in the stage-1
report. If the two upwind factors agree within 1.5%, the cal is robust.

## Analysis

Once the data is in SQLite, the analysis is the same reciprocal-pair math
as stage 1, applied per-leg. A draft script lives at
`/tmp/asymmetry_summary.py` and the per-tack pieces at
`/tmp/asymmetry_charts.py` — these can be adapted into a proper
`scripts/calibrate_per_tack.py` once the procedure has been run once.

Manual sketch of the math, with placeholder numbers:

| Leg | Duration | Tack | Mean BSP | Mean SOG | Mean heel |
|---|---|---|---:|---:|---:|
| 1 (upwind port) | 5 min | port | 5.85 | 5.40 | +12° |
| 2 (upwind stbd) | 5 min | starboard | 5.32 | 5.45 | −11° |
| 3 (dwwind stbd) | 5 min | starboard | 6.12 | 6.05 | −1° |
| 4 (dwwind port) | 5 min | port | 5.95 | 6.00 | +1° |

Upwind:
- `true_STW_upwind = (5.85 + 5.32) / 2 = 5.585`
- `factor_port = 5.585 / 5.85 = 0.9547`  (port reads 4.5% high)
- `factor_stbd = 5.585 / 5.32 = 1.0498` (stbd reads 5% low)
- New cals (on top of current 89.34%):
  - port: 89.34 × 0.9547 = **85.3%**
  - stbd: 89.34 × 1.0498 = **93.8%**

Downwind, same math.

If port and stbd factors come out within ±1% of each other (i.e., within
the noise floor), there is no meaningful per-tack asymmetry and the
existing global cal is sufficient. The race-data analysis suggests this
is *not* the case for this boat.

## Sanity checks

After applying per-tack cals:
- Re-run `/tmp/asymmetry_summary.py` against the post-stage-2 DB. The
  TWS-12 upwind delta column should drop from ≈ −8% to within ±2%.
- Median current `set` per tack (downwind especially) should pull toward
  each other rather than pointing in opposite directions.
- Drift/BSP ratio should drop further from the current 15.3% — likely to
  10–12% range, with the residual being real current + leeway + measurement
  noise.

## Caveats

- Triton² has no heel-compensation table or per-tack cal on STW — only a
  single global cal multiplier and basic damping. That actually simplifies
  this test: the measured port/stbd asymmetry reflects paddlewheel-vs-flow
  physics + hull/heel asymmetry directly, with no instrument-side heel
  correction confounding the result. If we ever upgrade to a processor
  (H5000 / Nemesis³), revisit this caveat — those systems do apply heel
  tables that need to be either matched on each tack or disabled for the
  test.
- Heel-induced compass deviation will affect the COG−HDG diagnostic in
  the same data; that's a separate calibration (compass swing at race
  heel) and won't bias the per-tack STW result, since the cal math uses
  scalar BSP and SOG, not headings.
- 5 minutes per leg is the minimum for a stable mean. 7–8 minutes is better
  if you can keep conditions steady that long.
- Building or fading breeze across the 25 min totally invalidates the
  cal. If conditions look unstable, abort and reschedule.

## When to run

Next light-air practice day (8–12 kt forecast, flat water). Avoid:
- Race days (no time to set up properly)
- Building-tide windows (Phase B vs Phase A current strength differs)
- Days with crew changes from a normal race configuration

## Software-side per-tack correction

Triton² has no per-tack STW cal, so the per-tack correction must be
applied at read-time in HelmLog:

1. Add `speed_correction_port` and `speed_correction_starboard`
   parameters to `boat_settings.py`.
2. Wire them into the same code path that should be applying
   `leeway_coefficient` (currently unread — see separate finding).
3. The current calc in `current.py` reads `heel_deg` from `attitudes`,
   selects the per-tack scale, and applies it to STW before forming the
   water vector.

The on-boat Triton² displays continue to show STW under the single
global 89.34% cal (no per-tack difference shown to the helm), while
HelmLog's stored/exported data and the post-race analysis reflect
per-tack reality.

## Cross-references

- Stage-1 cal: [speed-calibration-2026-04-18.md](speed-calibration-2026-04-18.md)
- Asymmetry analysis underpinning this procedure: see commits/issue tied
  to this PR (analysis scripts at `/tmp/asymmetry_*.py`, charts at
  `/tmp/asym_charts/`).
- Related software gaps surfaced during the analysis (filed separately):
  - `boat_settings.speed_correction` is metadata-only, never applied by code
  - `boat_settings.leeway_coefficient` is metadata-only, never applied by code
  - `current.py` does not subtract leeway from HDG when forming the water vector
  - `current.py` has no per-tack compass-deviation correction
