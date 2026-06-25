import { MonteCarloResult } from "../sim/montecarlo";
import { fmtTime } from "./format";

export function MonteCarloPanel({ result }: { result: MonteCarloResult }) {
  const maxRate = Math.max(0.0001, ...result.boats.map((b) => b.winRate));
  return (
    <div>
      <p className="hint">
        {result.races} races · seed {result.seed} · randomised phase, ±jitter on
        amplitude/period. A single race tells you who won <em>that</em> wind; this tells
        you whether the strategy holds up.
      </p>

      <h3>Win rate</h3>
      <div className="mc-bars">
        {result.boats.map((b) => (
          <div className="bar-row" key={b.id}>
            <span style={{ width: 110, color: "#c8d3e0" }}>{b.name}</span>
            <span className="bar-track">
              <span
                className="bar-fill"
                style={{ width: `${(b.winRate / maxRate) * 100}%`, background: b.color }}
              />
            </span>
            <span style={{ width: 48, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              {(b.winRate * 100).toFixed(1)}%
            </span>
          </div>
        ))}
      </div>

      <h3>Finish-time spread</h3>
      <div className="results">
        <table>
          <thead>
            <tr>
              <th className="name">Boat</th>
              <th>p10</th>
              <th>median</th>
              <th>p90</th>
              <th>finish %</th>
            </tr>
          </thead>
          <tbody>
            {result.boats.map((b) => (
              <tr key={b.id}>
                <td className="name">
                  <span
                    className="swatch"
                    style={{ background: b.color, display: "inline-block", marginRight: 6 }}
                  />
                  {b.name}
                </td>
                <td>{fmtTime(b.p10S)}</td>
                <td>{fmtTime(b.medianS)}</td>
                <td>{fmtTime(b.p90S)}</td>
                <td>{(b.finishRate * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
