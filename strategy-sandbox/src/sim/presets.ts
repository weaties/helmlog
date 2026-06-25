// Default scenario for the UI: a believable J/105-ish beat with three boats
// running distinct strategies, so the panel shows something interesting on
// first load. These are sandbox defaults, not a calibrated polar.

import { BoatConfig, GlobalConfig } from "./beat";
import { MonteCarloConfig } from "./montecarlo";

export const DEFAULT_GLOBAL: GlobalConfig = {
  speedKt: 6.2,
  halfTackAngleDeg: 42,
  legNm: 1,
  corridorM: 600,
  persistDegMin: 1.5,
  boatLengthM: 10.5,
  velAmpKt: 0,
  velPeriodS: 45,
  speedGain: 0.18,
  apparentGainDeg: 4,
};

export const DEFAULT_BOATS: BoatConfig[] = [
  {
    id: "shift",
    name: "Shift sailor",
    color: "#36d399", // green
    lossBL: 1.0,
    recovS: 8,
    threshDeg: 5,
    ampDeg: 14,
    periodS: 70,
    triggerMode: "true",
  },
  {
    id: "metronome",
    name: "Metronome",
    color: "#facc15", // amber
    lossBL: 1.0,
    recovS: 8,
    threshDeg: 2,
    ampDeg: 14,
    periodS: 70,
    triggerMode: "apparent",
  },
  {
    id: "banger",
    name: "Corner banger",
    color: "#f87272", // red
    lossBL: 1.0,
    recovS: 8,
    threshDeg: 30,
    ampDeg: 14,
    periodS: 70,
    triggerMode: "true",
  },
];

export const DEFAULT_MC_UI: MonteCarloConfig = {
  races: 200,
  seed: 1,
  ampJitter: 0.1,
  periodJitter: 0.1,
};
