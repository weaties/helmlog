# Beat Simulator — upwind strategy sandbox

A local-first, fully-offline single-page app that races three boats up one
windward leg in a shifting, puffing breeze and compares **time-to-mark** under
different tack penalties and tacking strategies.

It is a **strategy-geometry sandbox, not a VPP.** Boatspeed is a single flat
number (scaled only by the puff/lull oscillation); there is no velocity
prediction, no gradient/height trade, no current, no wind bend, no bad air, and
no boat-on-boat interaction. Each boat sails its **own independent** wind
realisation. Tacks are symmetric, costing a fixed distance loss plus a
reduced-speed recovery window. Laylines are approximated by a hard corridor
wall. Use it to reason about *when to tack*, not to predict real finish times.

## Stack

Vite + React + TypeScript (strict), Vitest. **No runtime deps beyond React** —
the SVG plot and charts are hand-rolled. No network calls.

```bash
npm install
npm run dev      # local dev server
npm run build    # tsc --noEmit && vite build
npm test         # vitest (physics unit tests)
npm run typecheck
```

## Layout

```
src/sim/    pure, framework-agnostic physics (no React imports)
  units.ts        kt/nm/deg <-> SI
  rng.ts          mulberry32 seedable PRNG
  beat.ts         simulateBoat / simulateRace  (geometry + velocity headers)
  montecarlo.ts   runMonteCarlo                (N reproducible races)
  presets.ts      default scenario for the UI
  *.test.ts       vitest unit tests
src/ui/     React components (Controls, TrackPlot, ResultsTable, MonteCarloPanel)
src/App.tsx single-page app: state, race animation transport, layout
```

## The model (what each task implements)

**Geometry.** Wind-direction shift `s(t) = ampDeg·sin(2π t/period) +
persistentRate·t`. On tack `±1`, heading off the rhumbline is
`h = tack·halfTackAngle − s`; `vmg = speed·cos h`, `cross = speed·sin h`. A boat
tacks when a real header pushes `|h| − halfTackAngle` past its threshold, or when
forced at the corridor wall. Each tack costs `lossBL` boatlengths and a
recovery window at reduced speed.

**Velocity headers.** A TWS puff/lull cycle, independent of any directional
shift, (a) scales boatspeed and (b) swings the *apparent* wind. A boat whose
tack trigger reads **apparent** wind chases these phantom headers and
over-tacks; a boat reading **true** wind correctly ignores them. The
velocity-induced apparent shift pollutes only the naive trigger — never the true
VMG — which is the whole point: acting on it is wasted motion.

**Monte Carlo.** A single deterministic race tells you who won *that* wind. This
runs N races (default 200) with randomised initial phase and small jitter on
amplitude/period via a seedable mulberry32 RNG, and reports per-boat **win rate**
and **p10 / median / p90** finish times. Same seed ⇒ bit-reproducible.

## Tests

`src/sim/*.test.ts` assert the behaviour the sandbox exists to demonstrate:

1. amplitude = 0, persistent = 0 → straight; tacks only at walls; deterministic.
2. big oscillation + cheap tacks → low-threshold boat beats high-threshold boat.
3. persistent shift → committed shift-sailor beats a metronome header-tacker.
4. velocity-only headers → apparent-trigger boat over-tacks and loses to true.
5. Monte Carlo with a fixed seed is bit-reproducible across runs.
