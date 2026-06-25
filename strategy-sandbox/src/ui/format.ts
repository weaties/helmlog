import { TrackPoint } from "../sim/beat";

export function fmtTime(sec: number): string {
  if (!isFinite(sec)) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Linear-interpolate a boat's position along its track at time t (seconds). */
export function posAtTime(track: TrackPoint[], t: number): TrackPoint {
  if (track.length === 0) return { t: 0, made: 0, cross: 0 };
  const first = track[0]!;
  const last = track[track.length - 1]!;
  if (t <= first.t) return first;
  if (t >= last.t) return last;
  // Track samples are evenly spaced in time; binary search is overkill but safe.
  let lo = 0;
  let hi = track.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (track[mid]!.t <= t) lo = mid;
    else hi = mid;
  }
  const a = track[lo]!;
  const b = track[hi]!;
  const span = b.t - a.t || 1;
  const f = (t - a.t) / span;
  return {
    t,
    made: a.made + (b.made - a.made) * f,
    cross: a.cross + (b.cross - a.cross) * f,
  };
}
