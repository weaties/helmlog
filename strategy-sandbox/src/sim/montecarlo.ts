// montecarlo.ts — TASK 3. A single deterministic race is a trap: it tells you
// who won THAT wind, not whether a strategy is sound. This runs N races with
// randomised initial phase and small jitter on amplitude/period, then reports
// win rate and the p10/median/p90 spread of finish times per boat.
//
// The RNG is seedable (mulberry32) so an entire study is bit-reproducible:
// same seed in => same numbers out, every time, on every machine.

import {
  BoatConfig,
  GlobalConfig,
  RacePerturbation,
  SimOptions,
  simulateRace,
} from "./beat";
import { mulberry32, Rng, uniform } from "./rng";

export interface MonteCarloConfig {
  races: number; // N (default 200)
  seed: number; // RNG seed
  ampJitter: number; // +/- fractional jitter on amplitude (e.g. 0.1 = +/-10%)
  periodJitter: number; // +/- fractional jitter on period
}

export const DEFAULT_MC: MonteCarloConfig = {
  races: 200,
  seed: 1,
  ampJitter: 0.1,
  periodJitter: 0.1,
};

export interface BoatStats {
  id: string;
  name: string;
  color: string;
  wins: number;
  winRate: number; // wins / races
  finishRate: number; // fraction of races this boat actually finished
  p10S: number;
  medianS: number;
  p90S: number;
}

export interface MonteCarloResult {
  races: number;
  seed: number;
  boats: BoatStats[];
}

/** Nearest-rank percentile (q in [0,1]) over an already-unsorted array. */
function percentile(values: number[], q: number): number {
  if (values.length === 0) return NaN;
  const sorted = [...values].sort((a, b) => a - b);
  // nearest-rank: rank = ceil(q * n), clamped to [1, n]
  const rank = Math.min(sorted.length, Math.max(1, Math.ceil(q * sorted.length)));
  return sorted[rank - 1]!;
}

function drawPerturbation(rng: Rng, mc: MonteCarloConfig): RacePerturbation {
  // Order of draws is fixed so the sequence is reproducible for a given seed.
  const phaseRad = uniform(rng, 0, 2 * Math.PI);
  const ampFactor = uniform(rng, 1 - mc.ampJitter, 1 + mc.ampJitter);
  const periodFactor = uniform(rng, 1 - mc.periodJitter, 1 + mc.periodJitter);
  return { phaseRad, ampFactor, periodFactor };
}

export function runMonteCarlo(
  global: GlobalConfig,
  boats: BoatConfig[],
  mc: MonteCarloConfig = DEFAULT_MC,
  opts: SimOptions = {},
): MonteCarloResult {
  const rng = mulberry32(mc.seed);

  const wins = new Map<string, number>();
  const finishes = new Map<string, number>();
  const times = new Map<string, number[]>();
  for (const b of boats) {
    wins.set(b.id, 0);
    finishes.set(b.id, 0);
    times.set(b.id, []);
  }

  for (let i = 0; i < mc.races; i++) {
    const perturb = drawPerturbation(rng, mc);
    const race = simulateRace(global, boats, { ...opts, perturb });
    if (race.winnerId !== null) {
      wins.set(race.winnerId, (wins.get(race.winnerId) ?? 0) + 1);
    }
    for (const r of race.boats) {
      times.get(r.id)!.push(r.timeS);
      if (r.finished) finishes.set(r.id, (finishes.get(r.id) ?? 0) + 1);
    }
  }

  const boatStats: BoatStats[] = boats.map((b) => {
    const ts = times.get(b.id)!;
    return {
      id: b.id,
      name: b.name,
      color: b.color,
      wins: wins.get(b.id) ?? 0,
      winRate: (wins.get(b.id) ?? 0) / mc.races,
      finishRate: (finishes.get(b.id) ?? 0) / mc.races,
      p10S: percentile(ts, 0.1),
      medianS: percentile(ts, 0.5),
      p90S: percentile(ts, 0.9),
    };
  });

  return { races: mc.races, seed: mc.seed, boats: boatStats };
}

export { percentile };
