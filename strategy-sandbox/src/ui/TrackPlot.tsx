import { BoatResult } from "../sim/beat";
import { posAtTime } from "./format";

interface Props {
  boats: BoatResult[];
  corridorM: number;
  legNm: number;
  // When set (Race animation), tracks are drawn up to this time and a moving
  // boat marker is shown at the interpolated position.
  playT?: number | null;
}

const W = 540;
const H = 660;
const PAD_X = 26;
const PAD_T = 30;
const PAD_B = 26;

export function TrackPlot({ boats, corridorM, legNm, playT }: Props) {
  const legM = legNm * 1852;
  const halfWall = corridorM / 2;
  const plotW = W - PAD_X * 2;
  const plotH = H - PAD_T - PAD_B;

  // cross in [-halfWall, +halfWall] -> x; made in [0, legM] -> y (mark at top)
  const mx = (cross: number) => PAD_X + ((cross + halfWall) / corridorM) * plotW;
  const my = (made: number) => PAD_T + (1 - made / legM) * plotH;

  const animating = playT != null;

  return (
    <div className="plot-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Upwind track plot">
        {/* corridor */}
        <rect
          x={mx(-halfWall)}
          y={my(legM)}
          width={plotW}
          height={plotH}
          fill="#0c1320"
          stroke="#243044"
        />
        {/* rhumbline */}
        <line
          x1={mx(0)}
          y1={my(0)}
          x2={mx(0)}
          y2={my(legM)}
          stroke="#2a3a52"
          strokeDasharray="4 6"
        />
        {/* mark (top) + start (bottom) */}
        <circle cx={mx(0)} cy={my(legM)} r={5} fill="#f59e0b" />
        <text x={mx(0) + 9} y={my(legM) + 4} fill="#7c8aa0" fontSize="11">
          mark
        </text>
        <rect x={mx(0) - 4} y={my(0) - 2} width={8} height={4} fill="#7c8aa0" />
        <text x={mx(0) + 9} y={my(0) + 3} fill="#7c8aa0" fontSize="11">
          start
        </text>

        {/* wall labels */}
        <text x={PAD_X} y={H - 8} fill="#56627a" fontSize="10">
          −{Math.round(halfWall)} m
        </text>
        <text x={W - PAD_X} y={H - 8} fill="#56627a" fontSize="10" textAnchor="end">
          +{Math.round(halfWall)} m
        </text>

        {boats.map((b) => {
          const pts = animating
            ? b.track.filter((p) => p.t <= (playT as number))
            : b.track;
          const d = pts.map((p) => `${mx(p.cross).toFixed(1)},${my(p.made).toFixed(1)}`).join(" ");
          return (
            <g key={b.id}>
              <polyline
                points={d}
                fill="none"
                stroke={b.color}
                strokeWidth={1.6}
                opacity={animating ? 0.55 : 0.9}
              />
              {/* tack markers */}
              {!animating &&
                b.tackEvents.map((e, i) => (
                  <circle
                    key={i}
                    cx={mx(e.cross)}
                    cy={my(e.made)}
                    r={e.reason === "wall" ? 2.6 : 2.2}
                    fill={e.reason === "wall" ? "#56627a" : b.color}
                    stroke={e.reason === "header" ? "#0a0e14" : "none"}
                    strokeWidth={0.6}
                  />
                ))}
            </g>
          );
        })}

        {/* animated boat markers */}
        {animating &&
          boats.map((b) => {
            const p = posAtTime(b.track, playT as number);
            const done = (playT as number) >= b.track[b.track.length - 1]!.t && b.finished;
            return (
              <g key={`m-${b.id}`}>
                <circle cx={mx(p.cross)} cy={my(p.made)} r={4.5} fill={b.color} />
                {done && (
                  <circle
                    cx={mx(p.cross)}
                    cy={my(p.made)}
                    r={8}
                    fill="none"
                    stroke={b.color}
                    strokeWidth={1}
                  />
                )}
              </g>
            );
          })}
      </svg>

      <div className="legend">
        {boats.map((b) => (
          <span className="item" key={b.id}>
            <span className="swatch" style={{ background: b.color }} />
            {b.name}
          </span>
        ))}
        <span className="item">
          <span className="swatch" style={{ background: "#56627a" }} />
          wall tack
        </span>
      </div>
    </div>
  );
}
