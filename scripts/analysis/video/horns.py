"""Horn detection from the video audio track, and race sync refinement.

Band-limited (250–1200 Hz) energy with a robust z-score finds loud, sustained
sounds; the 5-4-1-0 sequence (T-300, T-240, T-60, T) identifies the gun among
them. Crew voices near the camera trip the detector too, so a gun needs
either pattern support (≥ 2 of the three warning horns) or, when a Vakaros
``race_start`` gives an expected time, a loud blast within ±20 s of it. The
(horn video_t ↔ Vakaros UTC) pair is then the race's exact sync point.

Validated on races 253–255 (Sept 2026): stored session-start syncs were
5–6 s slow; the pattern was complete only on 254.

    uv run python -m scripts.analysis.video horns --race 254
"""

from __future__ import annotations

import argparse
import subprocess
import wave
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np

from scripts.analysis.video import common
from scripts.analysis.video.sources import resolve_source

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

DETECTOR_VERSION = "band-z-v2"
DEFAULT_THRESHOLD = 8.0
BAND_HZ = (250.0, 1200.0)
WINDOW_S = 0.2
HOP_S = 0.1
MIN_BLAST_S = 0.5
MIN_HORN_Z = 20.0  # quieter events are voices, winch clicks, water
PATTERN_OFFSETS_S = (300.0, 240.0, 60.0)  # 5-minute, 4-minute, 1-minute before the gun
PATTERN_TOL_S = 3.0
EXPECTED_WINDOW_S = 20.0  # a loud blast this close to the Vakaros gun is the gun
MAX_SYNC_DELTA_S = 120.0  # beyond this the horn cannot belong to this start
LISTEN_S = 15 * 60


@dataclass(frozen=True)
class AudioEvent:
    t: float
    dur_s: float
    z: float


@dataclass(frozen=True)
class GunCandidate:
    t: float
    hits: int  # how many of the 5m/4m/1m horns were found
    z: float


@dataclass(frozen=True)
class SyncResult:
    method: str
    gun_utc: common.datetime | None
    gun_video_t: float | None
    delta_s: float
    hits: int = 0


class SyncError(RuntimeError):
    """Horn and expected gun disagree by more than MAX_SYNC_DELTA_S."""


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def band_energy(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-hop horn-band energy: band fraction × loudness, a shape that rejects wind noise."""
    win = int(sr * WINDOW_S)
    hop = int(sr * HOP_S)
    freqs = np.fft.rfftfreq(win, 1 / sr)
    band = (freqs > BAND_HZ[0]) & (freqs < BAND_HZ[1])
    window = np.hanning(win)
    n = max(0, (len(x) - win) // hop)
    t = np.arange(n) * HOP_S
    e = np.empty(n)
    for i in range(n):
        seg = x[i * hop : i * hop + win] * window
        sp = np.abs(np.fft.rfft(seg)) ** 2
        tot = sp.sum() + 1e-9
        e[i] = sp[band].sum() / tot * np.sqrt(tot)
    return t, e


def detect_events(x: np.ndarray, sr: int, threshold: float = DEFAULT_THRESHOLD) -> list[AudioEvent]:
    """Sustained (≥ MIN_BLAST_S) excursions of the band energy above a robust z-score."""
    t, e = band_energy(x, sr)
    if len(e) == 0:
        return []
    med = np.median(e)
    mad = np.median(np.abs(e - med)) + 1e-9
    z = (e - med) / mad
    events: list[AudioEvent] = []
    i = 0
    while i < len(z):
        if z[i] > threshold:
            j = i
            while j < len(z) and z[j] > threshold:
                j += 1
            dur = (j - i) * HOP_S
            if dur >= MIN_BLAST_S:
                events.append(AudioEvent(float(t[i]), float(dur), float(z[i:j].max())))
            i = j
        else:
            i += 1
    return events


def gun_candidates(
    events: list[AudioEvent], tol_s: float = PATTERN_TOL_S, min_z: float = MIN_HORN_Z
) -> list[GunCandidate]:
    """Every loud event with its 5m/4m/1m pattern support, best supported first."""
    loud = [e for e in events if e.z >= min_z]
    starts = np.array([e.t for e in loud])
    out: list[GunCandidate] = []
    for e in loud:
        hits = sum(bool(np.any(np.abs(starts - (e.t - off)) < tol_s)) for off in PATTERN_OFFSETS_S)
        out.append(GunCandidate(e.t, hits, e.z))
    out.sort(key=lambda c: (-c.hits, -c.z))
    return out


def best_gun(cands: list[GunCandidate], expected_t: float | None = None) -> GunCandidate | None:
    """Pick the gun.

    Without an expected time only a candidate with ≥ 2 warning horns qualifies.
    With one (Vakaros), a patterned candidate within ±MAX_SYNC_DELTA_S wins,
    else the best-supported loudest blast within ±EXPECTED_WINDOW_S.
    """
    if expected_t is None:
        patterned = [c for c in cands if c.hits >= 2]
        return patterned[0] if patterned else None
    near = [c for c in cands if abs(c.t - expected_t) <= MAX_SYNC_DELTA_S]
    patterned = [c for c in near if c.hits >= 2]
    if patterned:
        return max(patterned, key=lambda c: (c.hits, -abs(c.t - expected_t)))
    close = [c for c in near if abs(c.t - expected_t) <= EXPECTED_WINDOW_S]
    if close:
        return max(close, key=lambda c: (c.hits, c.z))
    return None


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------


def extract_audio(
    source: Path, out_wav: Path, start_s: float, dur_s: float, sr: int = 16000
) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{max(0.0, start_s):.2f}", "-t", f"{dur_s:.1f}",
            "-i", str(source), "-vn", "-ac", "1", "-ar", str(sr), "-f", "wav", str(out_wav),
        ],
        check=True,
    )  # fmt: skip


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return x, sr


# ---------------------------------------------------------------------------
# Sync reconciliation
# ---------------------------------------------------------------------------


def reconcile_sync(
    ledger: sqlite3.Connection, race: common.Race, gun: GunCandidate | None
) -> SyncResult:
    """Write the race's effective sync + gun to the ledger and return it.

    horn+vakaros: horn video_t paired with the Vakaros gun UTC (exact sync).
    horn:         no Vakaros gun; the gun UTC is the horn read through the stored sync.
    stored:       no horn found; keep the stored sync and the Vakaros gun if any.
    """
    if race.video is None:
        raise SyncError(f"race {race.id} has no linked video")
    v = race.video
    if gun is None:
        result = SyncResult("stored", race.vakaros_gun, None, 0.0)
        sync_utc, sync_off = v.sync_utc, v.sync_offset_s
    elif race.vakaros_gun is not None:
        delta = v.video_t(race.vakaros_gun) - gun.t
        if abs(delta) > MAX_SYNC_DELTA_S:
            raise SyncError(
                f"race {race.id}: horn at t={gun.t:.1f} is {delta:+.0f}s from the Vakaros gun"
            )
        result = SyncResult("horn+vakaros", race.vakaros_gun, gun.t, delta, gun.hits)
        sync_utc, sync_off = race.vakaros_gun, gun.t
    else:
        gun_utc = v.utc_at(gun.t)
        expected = race.start_utc + timedelta(minutes=8)
        if abs((gun_utc - expected).total_seconds()) > 15 * 60:
            raise SyncError(
                f"race {race.id}: horn at t={gun.t:.1f} is implausibly far from the start"
            )
        result = SyncResult("horn", gun_utc, gun.t, 0.0, gun.hits)
        sync_utc, sync_off = v.sync_utc, v.sync_offset_s
    ledger.execute(
        "INSERT OR REPLACE INTO sync (race_id, video_id, method, gun_utc, gun_video_t,"
        " sync_utc, sync_offset_s, delta_s, horn_hits, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            race.id, v.video_id, result.method,
            result.gun_utc.isoformat() if result.gun_utc else None, result.gun_video_t,
            sync_utc.isoformat(), sync_off, result.delta_s, result.hits, common.utcnow_iso(),
        ),
    )  # fmt: skip
    ledger.commit()
    return result


def _store_events(
    ledger: sqlite3.Connection, video_id: str, events: list[AudioEvent], t0: float
) -> None:
    ledger.execute(
        "DELETE FROM audio_events WHERE video_id = ? AND video_t BETWEEN ? AND ?",
        (video_id, t0, t0 + LISTEN_S),
    )
    ledger.executemany(
        "INSERT INTO audio_events (video_id, video_t, kind, z, dur_s, detector_version)"
        " VALUES (?,?,?,?,?,?)",
        [(video_id, t0 + e.t, "horn", e.z, e.dur_s, DETECTOR_VERSION) for e in events],
    )
    ledger.commit()


def run_race(
    ledger: sqlite3.Connection, meta: sqlite3.Connection, race_id: int, threshold: float
) -> SyncResult:
    race = common.load_race(meta, race_id)
    if race.video is None:
        raise SyncError(f"race {race_id} has no linked video")
    src = resolve_source(ledger, race.video)
    # Listen from the stored session start: the sequence begins at gun-5 min.
    t0 = max(0.0, race.video.video_t(race.start_utc) - 60.0)
    wav = common.va_dir() / "audio" / f"{race.video.video_id}_{int(t0)}.wav"
    if not wav.exists():
        extract_audio(src.path, wav, t0, LISTEN_S)
    x, sr = read_wav(wav)
    events = detect_events(x, sr, threshold)
    _store_events(ledger, race.video.video_id, events, t0)
    expected = race.video.video_t(race.vakaros_gun) - t0 if race.vakaros_gun else None
    gun = best_gun(gun_candidates(events), expected)
    if gun is not None:
        gun = GunCandidate(t0 + gun.t, gun.hits, gun.z)
        print(f"  gun horn t={gun.t:.1f}s z={gun.z:.0f} ({gun.hits}/3 warning horns)")
    else:
        print(f"  no gun horn among {len(events)} events")
    result = reconcile_sync(ledger, race, gun)
    print(f"race {race.id}: sync={result.method} gun={result.gun_utc} delta={result.delta_s:+.1f}s")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[], help="race id (repeatable)")
    ap.add_argument("--season", action="store_true", help="every CYC Wednesday race with video")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    ids = list(args.race)
    if args.season:
        ids += [r.id for r in common.list_cyc_wednesday_races(meta) if r.id not in ids]
    failures = 0
    for rid in ids:
        try:
            run_race(ledger, meta, rid, args.threshold)
        except (SyncError, subprocess.CalledProcessError, FileNotFoundError) as exc:
            failures += 1
            print(f"race {rid}: FAILED — {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
