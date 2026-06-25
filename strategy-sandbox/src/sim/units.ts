// Unit conversions. The simulation works internally in SI (metres, seconds,
// radians); knots / nautical miles / degrees exist only at the config edge and
// in reported results.

export const KT_TO_MS = 0.514444; // 1 knot in metres/second
export const NM_TO_M = 1852; // 1 nautical mile in metres

export const kt2ms = (kt: number): number => kt * KT_TO_MS;
export const ms2kt = (ms: number): number => ms / KT_TO_MS;
export const nm2m = (nm: number): number => nm * NM_TO_M;
export const m2nm = (m: number): number => m / NM_TO_M;

export const deg2rad = (deg: number): number => (deg * Math.PI) / 180;
export const rad2deg = (rad: number): number => (rad * 180) / Math.PI;
