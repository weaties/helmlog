// beat.ts — pure upwind-leg physics. No React, no DOM, no randomness of its
// own (Monte Carlo passes a per-race RNG-derived `RacePerturbation`).
//
// WHAT THIS MODEL IS: a strategy-geometry sandbox. It answers "given a wind
// that shifts and puffs like THIS, and a boat that decides to tack like THAT,
// who reaches the windward mark first?" It exists to make tacking-strategy
// trade-offs visible and testable.
//
// WHAT THIS MODEL DELIBERATELY IGNORES (do not let the UI imply otherwise):
//   - No velocity prediction program. Boatspeed is a flat polar: one speed,
//     scaled only by the velocity oscillation. No gradient/height trade.
//   - No gradient wind bend, no geographic/persistent bend beyond a linear rate.
//   - No current / tide.
//   - No bad air, no lee-bow, no boats interacting at all. Each boat sails its
//     OWN independent wind realisation — they never share water. (This is why
//     each boat carries its own ampDeg/periodS.)
//   - Tacks are symmetric and instantaneous in heading; their only costs are a
//     fixed distance loss and a fixed reduced-speed recovery window.
//   - The mark is dead upwind; the rhumbline is the y-axis; "cross" is signed
//     distance off it. Laylines are approximated by a hard corridor wall.
//
// It is a teaching toy for shift strategy, not a VPP.

import { deg2rad, kt2ms, m2nm, ms2kt, nm2m } from "./units";

/** Why a tack happened — useful for tests and for annotating the plot. */
export type TackReason = "header" | "wall";

export interface TackEvent {
  t: number; // seconds into the race
  reason: TackReason;
  cross: number; // signed cross-track position (m) at the tack
  made: number; // distance made good toward mark (m) at the tack
}

export interface TrackPoint {
  t: number; // seconds
  made: number; // distance made good along rhumbline toward mark (m)
  cross: number; // signed cross-track offset (m); 0 = on the rhumbline
}

/** Naive vs correct tack trigger (TASK 2). */
export type TriggerMode = "true" | "apparent";

export interface GlobalConfig {
  speedKt: number; // flat close-hauled boatspeed (knots), before velocity osc.
  halfTackAngleDeg: number; // half the tacking angle = heading off the rhumbline
  legNm: number; // length of the (dead-upwind) leg, nautical miles
  corridorM: number; // total corridor width (m); walls at +/- corridorM/2
  persistDegMin: number; // persistent wind veer/back rate (deg per minute)
  boatLengthM: number; // for converting tack distance-loss (boatlengths -> m)

  // --- velocity oscillation (TASK 2) -------------------------------------
  // A puff/lull cycle in TRUE wind SPEED, independent of any directional
  // shift. It (a) scales boatspeed and (b) swings the APPARENT wind, creating
  // an apparent header/lift that has NO true directional component. A boat
  // that tacks on apparent wind will chase these and over-tack.
  velAmpKt: number; // amplitude of the TWS oscillation (knots). 0 disables it.
  velPeriodS: number; // period of the velocity oscillation (s)
  speedGain: number; // boatspeed sensitivity: knots of BS per knot of TWS delta
  apparentGainDeg: number; // apparent-wind swing: degrees per knot of TWS delta
}

export interface BoatConfig {
  id: string;
  name: string;
  color: string;
  lossBL: number; // distance lost per tack, in boatlengths
  recovS: number; // post-tack recovery window (s) at reduced speed
  threshDeg: number; // tack when a header exceeds this many degrees
  ampDeg: number; // amplitude of this boat's directional oscillation (deg)
  periodS: number; // period of this boat's directional oscillation (s)
  triggerMode: TriggerMode; // tack on TRUE (correct) or APPARENT (naive) wind
}

/** Per-race wind perturbation, supplied by Monte Carlo (TASK 3). */
export interface RacePerturbation {
  phaseRad: number; // initial phase of both oscillations
  ampFactor: number; // multiplicative jitter on amplitude (~1.0)
  periodFactor: number; // multiplicative jitter on period (~1.0)
}

export interface SimOptions {
  dt?: number; // integration step (s)
  recoveryFactor?: number; // speed multiplier during recovery window
  maxTimeS?: number; // hard cap; if exceeded the boat is "did not finish"
  perturb?: RacePerturbation;
}

export interface BoatResult {
  id: string;
  name: string;
  color: string;
  timeS: number; // time to the mark (or maxTimeS if it never finished)
  tacks: number;
  avgVmgKt: number; // average velocity-made-good toward the mark, knots
  finished: boolean;
  track: TrackPoint[];
  tackEvents: TackEvent[];
}

const DEFAULTS = {
  dt: 0.5,
  recoveryFactor: 0.6,
} as const;

const NO_PERTURB: RacePerturbation = {
  phaseRad: 0,
  ampFactor: 1,
  periodFactor: 1,
};

/**
 * Simulate one boat sailing the leg. Pure: identical inputs -> identical output.
 */
export function simulateBoat(
  global: GlobalConfig,
  boat: BoatConfig,
  opts: SimOptions = {},
): BoatResult {
  const dt = opts.dt ?? DEFAULTS.dt;
  const recoveryFactor = opts.recoveryFactor ?? DEFAULTS.recoveryFactor;
  const perturb = opts.perturb ?? NO_PERTURB;

  const legM = nm2m(global.legNm);
  const halfTackDeg = global.halfTackAngleDeg;
  const halfWall = global.corridorM / 2;
  const baseSpeedMs = kt2ms(global.speedKt);
  const persistDegPerSec = global.persistDegMin / 60;

  // A finish should be reachable in legM / (baseSpeed * cos(halfTack)) at best;
  // cap generously so a pathological over-tacker still terminates.
  const bestVmg = baseSpeedMs * Math.cos(deg2rad(halfTackDeg));
  const maxTimeS = opts.maxTimeS ?? (legM / Math.max(bestVmg, 1e-3)) * 6;

  // Jittered oscillation parameters for this race.
  const dirAmpDeg = boat.ampDeg * perturb.ampFactor;
  const dirPeriodS = Math.max(boat.periodS * perturb.periodFactor, 1e-3);
  const velPeriodS = Math.max(global.velPeriodS * perturb.periodFactor, 1e-3);
  const velAmpKt = global.velAmpKt * perturb.ampFactor;
  const dirOmega = (2 * Math.PI) / dirPeriodS;
  const velOmega = (2 * Math.PI) / velPeriodS;

  // s(t): TRUE wind-direction shift off its mean, in degrees.
  const sTrueDeg = (t: number): number =>
    dirAmpDeg * Math.sin(dirOmega * t + perturb.phaseRad) + persistDegPerSec * t;

  // g(t): TRUE wind-SPEED oscillation off its mean, in knots (puff>0, lull<0).
  const gKt = (t: number): number =>
    velAmpKt * Math.sin(velOmega * t + perturb.phaseRad);

  let t = 0;
  let made = 0; // m made good toward the mark
  let cross = 0; // signed cross-track offset, m
  let tack = 1; // +1 or -1; start on starboard heading +halfTack off rhumbline
  let recoveryUntil = -Infinity;
  let tacks = 0;
  let vmgSum = 0;
  let vmgCount = 0;

  const track: TrackPoint[] = [{ t: 0, made: 0, cross: 0 }];
  const tackEvents: TackEvent[] = [];

  let finished = false;

  while (t < maxTimeS) {
    const sNow = sTrueDeg(t);
    const gNow = gKt(t);

    // TRUE geometry drives actual progress. Heading offset from rhumbline:
    //   h = tack*halfTack - s   (a positive shift lifts the +1 tack)
    const hTrueDeg = tack * halfTackDeg - sNow;
    const hTrueRad = deg2rad(hTrueDeg);

    // Boatspeed scales with the velocity oscillation (puff = faster), then is
    // cut during the post-tack recovery window.
    let speedMs = kt2ms(global.speedKt + global.speedGain * gNow);
    if (speedMs < 0) speedMs = 0;
    const inRecovery = t < recoveryUntil;
    if (inRecovery) speedMs *= recoveryFactor;
    // keep baseSpeedMs referenced for readers; bestVmg already used it.
    void baseSpeedMs;

    const vmg = speedMs * Math.cos(hTrueRad); // toward the mark
    const crossV = speedMs * Math.sin(hTrueRad); // sideways

    made += vmg * dt;
    cross += crossV * dt;
    t += dt;
    vmgSum += vmg;
    vmgCount += 1;
    track.push({ t, made, cross });

    if (made >= legM) {
      finished = true;
      break;
    }

    // --- tack decision -----------------------------------------------------
    // The naive boat measures the header against APPARENT wind, which the
    // velocity oscillation has polluted with a phantom directional component;
    // the correct boat measures against TRUE wind only.
    const sApparentDeg =
      boat.triggerMode === "apparent" ? sNow + global.apparentGainDeg * gNow : sNow;
    const hTrigDeg = tack * halfTackDeg - sApparentDeg;

    // A real header pushes |h| past the close-hauled angle by more than thresh.
    const headerExcess = Math.abs(hTrigDeg) - halfTackDeg;
    const atWall = Math.abs(cross) >= halfWall;

    // Don't re-tack mid-recovery (prevents thrash and models the cost window).
    if (!inRecovery && (atWall || headerExcess > boat.threshDeg)) {
      tack = -tack; // symmetric tack: header on one tack is a lift on the other
      tacks += 1;
      // One-off distance loss (boatlengths -> m), never below zero.
      made = Math.max(0, made - boat.lossBL * global.boatLengthM);
      recoveryUntil = t + boat.recovS;
      tackEvents.push({ t, reason: atWall ? "wall" : "header", cross, made });
    }
  }

  const timeS = finished ? t : maxTimeS;
  // Headline number: net VMG made good over the whole leg, in knots. Tack
  // distance-losses and recovery windows are baked in because they inflate
  // timeS. (vmgSum/count is the instantaneous-VMG mean; kept for callers.)
  void vmgSum;
  void vmgCount;
  const avgVmgKt = ms2kt((finished ? legM : made) / timeS);

  return {
    id: boat.id,
    name: boat.name,
    color: boat.color,
    timeS,
    tacks,
    avgVmgKt,
    finished,
    track,
    tackEvents,
  };
}

export interface RaceResult {
  boats: BoatResult[];
  winnerId: string | null; // fastest finisher; null if nobody finished
}

/** Run every boat over the same leg/perturbation and pick the fastest. */
export function simulateRace(
  global: GlobalConfig,
  boats: BoatConfig[],
  opts: SimOptions = {},
): RaceResult {
  const results = boats.map((b) => simulateBoat(global, b, opts));
  let winnerId: string | null = null;
  let best = Infinity;
  for (const r of results) {
    if (r.finished && r.timeS < best) {
      best = r.timeS;
      winnerId = r.id;
    }
  }
  return { boats: results, winnerId };
}

/** Convenience for the UI/results table. */
export const legDistanceNm = (g: GlobalConfig): number => g.legNm;
export const trackPointToNm = (p: TrackPoint): { madeNm: number; crossNm: number } => ({
  madeNm: m2nm(p.made),
  crossNm: m2nm(p.cross),
});
