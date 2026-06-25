import { useEffect, useMemo, useRef, useState } from "react";
import { GlobalConfig, BoatConfig, simulateRace } from "./sim/beat";
import { MonteCarloConfig, MonteCarloResult, runMonteCarlo } from "./sim/montecarlo";
import { DEFAULT_BOATS, DEFAULT_GLOBAL, DEFAULT_MC_UI } from "./sim/presets";
import { Controls } from "./ui/Controls";
import { TrackPlot } from "./ui/TrackPlot";
import { ResultsTable } from "./ui/ResultsTable";
import { MonteCarloPanel } from "./ui/MonteCarloPanel";
import { fmtTime } from "./ui/format";

// Sim-seconds advanced per real second while the Race animation plays.
const PLAY_SPEED = 25;

export function App() {
  const [global, setGlobal] = useState<GlobalConfig>(DEFAULT_GLOBAL);
  const [boats, setBoats] = useState<BoatConfig[]>(DEFAULT_BOATS);
  const [velEnabled, setVelEnabled] = useState(false);
  const [mode, setMode] = useState<"single" | "montecarlo">("single");
  const [mc, setMc] = useState<MonteCarloConfig>(DEFAULT_MC_UI);
  const [mcResult, setMcResult] = useState<MonteCarloResult | null>(null);

  // Velocity oscillation is a toggle: when off we force its amplitude to zero
  // so "true" and "apparent" triggers behave identically (nothing to chase).
  const effectiveGlobal = useMemo<GlobalConfig>(
    () => (velEnabled ? global : { ...global, velAmpKt: 0 }),
    [velEnabled, global],
  );

  const race = useMemo(
    () => simulateRace(effectiveGlobal, boats),
    [effectiveGlobal, boats],
  );

  const maxT = useMemo(
    () => Math.max(1, ...race.boats.map((b) => b.track[b.track.length - 1]?.t ?? 0)),
    [race],
  );

  // --- Race animation transport -----------------------------------------
  const [playT, setPlayT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const rafRef = useRef<number | null>(null);
  const lastTsRef = useRef<number | null>(null);

  // Reset the playhead whenever the underlying race changes.
  useEffect(() => {
    setPlayT(0);
    setPlaying(false);
  }, [race]);

  useEffect(() => {
    if (!playing) return;
    const tick = (ts: number) => {
      const last = lastTsRef.current;
      lastTsRef.current = ts;
      if (last != null) {
        const dtReal = (ts - last) / 1000;
        setPlayT((prev) => {
          const next = prev + dtReal * PLAY_SPEED;
          if (next >= maxT) {
            setPlaying(false);
            return maxT;
          }
          return next;
        });
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      lastTsRef.current = null;
    };
  }, [playing, maxT]);

  const togglePlay = () => {
    if (playT >= maxT) setPlayT(0);
    setPlaying((p) => !p);
  };

  const runMc = () => setMcResult(runMonteCarlo(effectiveGlobal, boats, mc));

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>Beat Simulator</h1>
          <div className="sub">upwind strategy sandbox · local-first · offline</div>
        </div>
        <div className="sub">
          {boats.length} boats · {global.legNm} nm leg
        </div>
      </header>

      <div className="disclaimer">
        <strong>This is a strategy-geometry sandbox, not a VPP.</strong> Boatspeed is a
        single flat number (scaled only by the puff/lull oscillation) — there is no
        velocity-prediction polar, no gradient/height trade. The model ignores wind bend,
        current, bad air and boat-on-boat interaction: each boat sails its{" "}
        <em>own independent</em> wind realisation and never sees the others. Tacks are
        symmetric, costing only a fixed distance loss plus a reduced-speed recovery window.
        Laylines are approximated by a hard corridor wall. Use it to reason about{" "}
        <em>when to tack</em>, not to predict real finish times.
      </div>

      <div className="grid">
        <Controls
          global={global}
          setGlobal={setGlobal}
          boats={boats}
          setBoats={setBoats}
          velEnabled={velEnabled}
          setVelEnabled={setVelEnabled}
          mode={mode}
          setMode={setMode}
          mc={mc}
          setMc={setMc}
          onRunMc={runMc}
        />

        <div style={{ display: "grid", gap: 18 }}>
          <div className="panel">
            <h2>Track</h2>
            <TrackPlot
              boats={race.boats}
              corridorM={global.corridorM}
              legNm={global.legNm}
              playT={playing || playT > 0 ? playT : null}
            />
            <div className="toggle-row" style={{ marginTop: 10, alignItems: "center" }}>
              <button className="primary" onClick={togglePlay}>
                {playing ? "❚❚ Pause" : "▶ Race"}
              </button>
              <button
                onClick={() => {
                  setPlaying(false);
                  setPlayT(0);
                }}
              >
                ⟲ Reset
              </button>
              <input
                type="range"
                min={0}
                max={maxT}
                step={0.5}
                value={playT}
                style={{ width: 220 }}
                onChange={(e) => {
                  setPlaying(false);
                  setPlayT(parseFloat(e.target.value));
                }}
              />
              <span className="sub" style={{ fontVariantNumeric: "tabular-nums" }}>
                t = {fmtTime(playT)} / {fmtTime(maxT)}
              </span>
            </div>
          </div>

          {mode === "single" ? (
            <div className="panel">
              <h2>Single race result</h2>
              <ResultsTable race={race} boats={boats} />
              <p className="hint">
                Header tacks are coloured dots on each track; grey dots are forced tacks at
                the corridor wall. One race is one wind — switch to Monte Carlo to judge the
                strategy.
              </p>
            </div>
          ) : (
            <div className="panel">
              <h2>Monte Carlo</h2>
              {mcResult ? (
                <MonteCarloPanel result={mcResult} />
              ) : (
                <p className="hint">
                  Press “Run {mc.races} races” in the controls. Same seed ⇒ identical
                  results, every time.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
