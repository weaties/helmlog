// mulberry32 — a tiny, fast, seedable PRNG. Deterministic for a given seed,
// which is the whole point: Monte Carlo runs must be bit-reproducible so a
// strategy comparison can be re-checked, shared, and trusted.
//
// This is NOT cryptographically secure and makes no claim to be. It is good
// enough for jittering wind phase / amplitude across a few hundred races.

export type Rng = () => number;

export function mulberry32(seed: number): Rng {
  let a = seed >>> 0;
  return function next(): number {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Uniform float in [min, max). */
export function uniform(rng: Rng, min: number, max: number): number {
  return min + (max - min) * rng();
}
