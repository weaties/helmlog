import { describe, expect, it } from "vitest";
import { mulberry32, uniform } from "./rng";

describe("mulberry32", () => {
  it("is deterministic for a given seed", () => {
    const a = mulberry32(42);
    const b = mulberry32(42);
    const seqA = Array.from({ length: 5 }, () => a());
    const seqB = Array.from({ length: 5 }, () => b());
    expect(seqA).toEqual(seqB);
  });

  it("diverges for different seeds", () => {
    const a = mulberry32(1);
    const b = mulberry32(2);
    expect(a()).not.toBe(b());
  });

  it("stays within [0, 1)", () => {
    const r = mulberry32(7);
    for (let i = 0; i < 1000; i++) {
      const x = r();
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x).toBeLessThan(1);
    }
  });

  it("uniform maps into [min, max)", () => {
    const r = mulberry32(7);
    for (let i = 0; i < 1000; i++) {
      const x = uniform(r, -5, 5);
      expect(x).toBeGreaterThanOrEqual(-5);
      expect(x).toBeLessThan(5);
    }
  });
});
