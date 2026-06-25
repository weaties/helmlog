import { describe, expect, it } from "vitest";
import {
  BoatConfig,
  GlobalConfig,
  RaceResult,
  simulateBoat,
  simulateRace,
} from "./beat";

// A neutral leg: no shift, no puffs. Individual tests turn knobs on.
function baseGlobal(over: Partial<GlobalConfig> = {}): GlobalConfig {
  return {
    speedKt: 6,
    halfTackAngleDeg: 42,
    legNm: 1,
    corridorM: 400,
    persistDegMin: 0,
    boatLengthM: 10,
    velAmpKt: 0,
    velPeriodS: 40,
    speedGain: 0.15,
    apparentGainDeg: 4,
    ...over,
  };
}

function boat(id: string, over: Partial<BoatConfig> = {}): BoatConfig {
  return {
    id,
    name: id,
    color: "#fff",
    lossBL: 1,
    recovS: 8,
    threshDeg: 8,
    ampDeg: 0,
    periodS: 60,
    triggerMode: "true",
    ...over,
  };
}

describe("1. flat wind: straight, walls only, deterministic", () => {
  const g = baseGlobal({ persistDegMin: 0 });
  const b = boat("flat", { ampDeg: 0, threshDeg: 8 });

  it("finishes and tacks only at corridor walls", () => {
    const r = simulateBoat(g, b);
    expect(r.finished).toBe(true);
    expect(r.tackEvents.every((e) => e.reason === "wall")).toBe(true);
  });

  it("is bit-for-bit deterministic across runs", () => {
    const a = simulateBoat(g, b);
    const c = simulateBoat(g, b);
    expect(JSON.stringify(a)).toBe(JSON.stringify(c));
  });
});

describe("2. big oscillation + cheap tacks: low threshold beats high threshold", () => {
  it("the shift-sailing (low-threshold) boat reaches the mark first", () => {
    // Wide corridor so wall tacks are incidental; cheap tacks so the
    // low-threshold boat isn't punished for tacking on every header.
    const g = baseGlobal({ corridorM: 1200 });
    const cheap = { lossBL: 0.4, recovS: 3, ampDeg: 20, periodS: 80 };
    const low = boat("low", { ...cheap, threshDeg: 4 });
    const high = boat("high", { ...cheap, threshDeg: 40 }); // never header-tacks

    const rl = simulateBoat(g, low);
    const rh = simulateBoat(g, high);

    expect(rl.finished && rh.finished).toBe(true);
    expect(rh.tackEvents.some((e) => e.reason === "header")).toBe(false);
    expect(rl.tackEvents.some((e) => e.reason === "header")).toBe(true);
    expect(rl.timeS).toBeLessThan(rh.timeS);
  });
});

describe("3. persistent shift: sailing toward the shift beats metronome tacking", () => {
  it("the committed boat beats the boat that tacks on every wobble", () => {
    const g = baseGlobal({ persistDegMin: 2, corridorM: 600 });
    // A: rides the lifting tack toward the persistent shift, rarely tacks.
    const committed = boat("committed", {
      ampDeg: 0,
      threshDeg: 16,
      lossBL: 0.6,
      recovS: 5,
    });
    // B: short-period metronome that tacks on every oscillation header.
    const metronome = boat("metronome", {
      ampDeg: 18,
      periodS: 40,
      threshDeg: 3,
      lossBL: 0.6,
      recovS: 5,
    });

    const ra = simulateBoat(g, committed);
    const rb = simulateBoat(g, metronome);

    expect(ra.finished && rb.finished).toBe(true);
    expect(rb.tacks).toBeGreaterThan(ra.tacks);
    expect(ra.timeS).toBeLessThan(rb.timeS);
  });
});

describe("4. velocity-only headers: apparent-trigger boat over-tacks and loses", () => {
  it("the TRUE-wind boat beats the naive APPARENT-wind boat", () => {
    // No directional shift at all; only a TWS puff/lull cycle that swings the
    // apparent wind. There is nothing real to tack on.
    const g = baseGlobal({
      persistDegMin: 0,
      corridorM: 800,
      velAmpKt: 6,
      velPeriodS: 40,
      speedGain: 0.15,
      apparentGainDeg: 4, // 24 deg apparent swing at the puff peak
    });
    const common = { ampDeg: 0, threshDeg: 6, lossBL: 1, recovS: 8 };
    const trueBoat = boat("true", { ...common, triggerMode: "true" });
    const apparentBoat = boat("apparent", { ...common, triggerMode: "apparent" });

    const rt = simulateBoat(g, trueBoat);
    const ra = simulateBoat(g, apparentBoat);

    expect(rt.finished && ra.finished).toBe(true);
    // The naive boat chases phantom headers => far more tacks.
    expect(ra.tacks).toBeGreaterThan(rt.tacks);
    expect(ra.tackEvents.some((e) => e.reason === "header")).toBe(true);
    expect(rt.tackEvents.some((e) => e.reason === "header")).toBe(false);
    // ...and pays for them in elapsed time.
    expect(ra.timeS).toBeGreaterThan(rt.timeS);
  });
});

describe("simulateRace picks the fastest finisher", () => {
  it("returns the lower-time boat as winner", () => {
    const g = baseGlobal({ corridorM: 1200 });
    const cheap = { lossBL: 0.4, recovS: 3, ampDeg: 20, periodS: 80 };
    const boats = [
      boat("low", { ...cheap, threshDeg: 4 }),
      boat("high", { ...cheap, threshDeg: 40 }),
    ];
    const race: RaceResult = simulateRace(g, boats);
    expect(race.winnerId).toBe("low");
  });
});
