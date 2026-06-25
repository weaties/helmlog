import { BoatConfig, GlobalConfig } from "../sim/beat";
import { MonteCarloConfig } from "../sim/montecarlo";

interface NumFieldProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  onChange: (v: number) => void;
}

function Slider({ label, value, min, max, step, unit, onChange }: NumFieldProps) {
  return (
    <div className="field">
      <label>{label}</label>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
      />
      <span className="val">
        {value}
        {unit ? ` ${unit}` : ""}
      </span>
    </div>
  );
}

interface Props {
  global: GlobalConfig;
  setGlobal: (g: GlobalConfig) => void;
  boats: BoatConfig[];
  setBoats: (b: BoatConfig[]) => void;
  velEnabled: boolean;
  setVelEnabled: (v: boolean) => void;
  mode: "single" | "montecarlo";
  setMode: (m: "single" | "montecarlo") => void;
  mc: MonteCarloConfig;
  setMc: (m: MonteCarloConfig) => void;
  onRunMc: () => void;
}

export function Controls(p: Props) {
  const { global, setGlobal, boats, setBoats } = p;
  const g = (patch: Partial<GlobalConfig>) => setGlobal({ ...global, ...patch });
  const setBoat = (i: number, patch: Partial<BoatConfig>) =>
    setBoats(boats.map((b, j) => (j === i ? { ...b, ...patch } : b)));

  return (
    <div className="panel">
      <h2>Wind &amp; course</h2>
      <Slider label="Boatspeed" value={global.speedKt} min={3} max={9} step={0.1} unit="kt" onChange={(v) => g({ speedKt: v })} />
      <Slider label="½ tack angle" value={global.halfTackAngleDeg} min={30} max={55} step={1} unit="°" onChange={(v) => g({ halfTackAngleDeg: v })} />
      <Slider label="Leg length" value={global.legNm} min={0.3} max={3} step={0.1} unit="nm" onChange={(v) => g({ legNm: v })} />
      <Slider label="Corridor" value={global.corridorM} min={150} max={1500} step={50} unit="m" onChange={(v) => g({ corridorM: v })} />
      <Slider label="Persistent shift" value={global.persistDegMin} min={0} max={6} step={0.5} unit="°/min" onChange={(v) => g({ persistDegMin: v })} />
      <Slider label="Boat length" value={global.boatLengthM} min={6} max={18} step={0.5} unit="m" onChange={(v) => g({ boatLengthM: v })} />

      <h3>
        <label style={{ display: "flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={p.velEnabled}
            onChange={(e) => p.setVelEnabled(e.target.checked)}
          />
          Velocity oscillation (puffs/lulls)
        </label>
      </h3>
      {p.velEnabled && (
        <>
          <Slider label="TWS amplitude" value={global.velAmpKt} min={0} max={8} step={0.5} unit="kt" onChange={(v) => g({ velAmpKt: v })} />
          <Slider label="Vel period" value={global.velPeriodS} min={15} max={120} step={5} unit="s" onChange={(v) => g({ velPeriodS: v })} />
          <Slider label="Speed gain" value={global.speedGain} min={0} max={0.4} step={0.02} unit="kt/kt" onChange={(v) => g({ speedGain: v })} />
          <Slider label="App. swing" value={global.apparentGainDeg} min={0} max={6} step={0.5} unit="°/kt" onChange={(v) => g({ apparentGainDeg: v })} />
          <p className="hint">
            A puff swings the <em>apparent</em> wind with no true directional shift. A boat
            tacking on apparent wind will chase these and over-tack.
          </p>
        </>
      )}

      <h2 style={{ marginTop: 18 }}>Boats</h2>
      {boats.map((b, i) => (
        <div className="boat-card" key={b.id}>
          <div className="boat-head">
            <span className="swatch" style={{ background: b.color }} />
            <input
              className="boat-name"
              value={b.name}
              onChange={(e) => setBoat(i, { name: e.target.value })}
            />
          </div>
          <Slider label="Tack threshold" value={b.threshDeg} min={1} max={40} step={1} unit="°" onChange={(v) => setBoat(i, { threshDeg: v })} />
          <Slider label="Dir. amplitude" value={b.ampDeg} min={0} max={30} step={1} unit="°" onChange={(v) => setBoat(i, { ampDeg: v })} />
          <Slider label="Dir. period" value={b.periodS} min={20} max={160} step={5} unit="s" onChange={(v) => setBoat(i, { periodS: v })} />
          <Slider label="Tack loss" value={b.lossBL} min={0} max={4} step={0.1} unit="BL" onChange={(v) => setBoat(i, { lossBL: v })} />
          <Slider label="Recovery" value={b.recovS} min={0} max={20} step={1} unit="s" onChange={(v) => setBoat(i, { recovS: v })} />
          <div className="field">
            <label>Tack trigger</label>
            <select
              value={b.triggerMode}
              onChange={(e) => setBoat(i, { triggerMode: e.target.value as BoatConfig["triggerMode"] })}
            >
              <option value="true">true wind (correct)</option>
              <option value="apparent">apparent (naive)</option>
            </select>
          </div>
        </div>
      ))}

      <h2 style={{ marginTop: 18 }}>Mode</h2>
      <div className="toggle-row">
        <button
          className={p.mode === "single" ? "active" : ""}
          onClick={() => p.setMode("single")}
        >
          Single race
        </button>
        <button
          className={p.mode === "montecarlo" ? "active" : ""}
          onClick={() => p.setMode("montecarlo")}
        >
          Monte Carlo
        </button>
      </div>
      {p.mode === "montecarlo" && (
        <>
          <div className="field">
            <label>Races (N)</label>
            <input
              type="number"
              value={p.mc.races}
              min={10}
              max={2000}
              step={10}
              onChange={(e) => p.setMc({ ...p.mc, races: Math.max(1, parseInt(e.target.value || "1", 10)) })}
            />
          </div>
          <div className="field">
            <label>Seed</label>
            <input
              type="number"
              value={p.mc.seed}
              onChange={(e) => p.setMc({ ...p.mc, seed: parseInt(e.target.value || "0", 10) })}
            />
          </div>
          <Slider label="Amp jitter" value={p.mc.ampJitter} min={0} max={0.5} step={0.05} onChange={(v) => p.setMc({ ...p.mc, ampJitter: v })} />
          <Slider label="Period jitter" value={p.mc.periodJitter} min={0} max={0.5} step={0.05} onChange={(v) => p.setMc({ ...p.mc, periodJitter: v })} />
          <div className="toggle-row">
            <button className="primary" onClick={p.onRunMc}>
              Run {p.mc.races} races
            </button>
          </div>
        </>
      )}
    </div>
  );
}
