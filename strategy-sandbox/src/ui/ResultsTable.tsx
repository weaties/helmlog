import { BoatConfig, RaceResult } from "../sim/beat";
import { fmtTime } from "./format";

interface Props {
  race: RaceResult;
  boats: BoatConfig[]; // for the trigger-mode column
}

export function ResultsTable({ race, boats }: Props) {
  const triggerById = new Map(boats.map((b) => [b.id, b.triggerMode]));
  return (
    <div className="results">
      <table>
        <thead>
          <tr>
            <th className="name">Boat</th>
            <th>Time</th>
            <th>Tacks</th>
            <th>Avg VMG</th>
            <th>Trigger</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {race.boats.map((b) => {
            const win = b.id === race.winnerId;
            return (
              <tr key={b.id} className={win ? "winner" : undefined}>
                <td className="name">
                  <span
                    className="swatch"
                    style={{ background: b.color, display: "inline-block", marginRight: 6 }}
                  />
                  {b.name}
                </td>
                <td>{fmtTime(b.timeS)}</td>
                <td>{b.tacks}</td>
                <td>{b.avgVmgKt.toFixed(2)} kt</td>
                <td>{triggerById.get(b.id) === "apparent" ? "apparent" : "true"}</td>
                <td>
                  {!b.finished ? (
                    <span className="tag">DNF</span>
                  ) : win ? (
                    <span className="tag win">1st</span>
                  ) : (
                    <span className="tag">finished</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default ResultsTable;
