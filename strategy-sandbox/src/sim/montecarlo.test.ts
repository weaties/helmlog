import { describe, expect, it } from "vitest";
import { BoatConfig, GlobalConfig } from "./beat";
import { MonteCarloConfig, percentile, runMonteCarlo } from "./montecarlo";

function global(over: Partial<GlobalConfig> = {}): GlobalConfig {
  return {
    speedKt: 6,
    halfTackAngleDeg: 42,
    legNm: 1,
    corridorM: 600,
    persistDegMin: 0,
    boatLengthM: 10,
    velAmpKt: 0,
    velPeriodS: 40,
    speedGain: 0.15,
    apparentGainDeg: 4,
    ...over,
  };
}

const boats: BoatConfig[] = [
  {
    id: "a",
    name: "A",
    color: "#0ff",
    lossBL: 0.5,
    recovS: 4,
    threshDeg: 5,
    ampDeg: 18,
    periodS: 70,
    triggerMode: "true",
  },
  {
    id: "b",
    name: "B",
    color: "#f0f",
    lossBL: 0.5,
    recovS: 4,
    threshDeg: 30,
    ampDeg: 18,
    periodS: 70,
    triggerMode: "true",
  },
];

describe("5. Monte Carlo is bit-reproducible for a fixed seed", () => {
  const mc: MonteCarloConfig = { races: 50, seed: 12345, ampJitter: 0.1, periodJitter: 0.1 };

  it("produces identical results across two runs with the same seed", () => {
    const r1 = runMonteCarlo(global(), boats, mc);
    const r2 = runMonteCarlo(global(), boats, mc);
    expect(JSON.stringify(r1)).toBe(JSON.stringify(r2));
  });

  it("produces different results for a different seed", () => {
    const r1 = runMonteCarlo(global(), boats, mc);
    const r2 = runMonteCarlo(global(), boats, { ...mc, seed: 999 });
    expect(JSON.stringify(r1)).not.toBe(JSON.stringify(r2));
  });

  it("win rates over all boats sum to <= 1 and percentiles are ordered", () => {
    const r = runMonteCarlo(global(), boats, mc);
    const totalWinRate = r.boats.reduce((s, b) => s + b.winRate, 0);
    expect(totalWinRate).toBeLessThanOrEqual(1 + 1e-9);
    for (const b of r.boats) {
      expect(b.p10S).toBeLessThanOrEqual(b.medianS);
      expect(b.medianS).toBeLessThanOrEqual(b.p90S);
    }
  });
});

describe("percentile (nearest-rank)", () => {
  it("matches known ranks", () => {
    const v = [50, 10, 30, 20, 40]; // sorted: 10 20 30 40 50
    expect(percentile(v, 0.1)).toBe(10);
    expect(percentile(v, 0.5)).toBe(30);
    expect(percentile(v, 0.9)).toBe(50);
  });
});
