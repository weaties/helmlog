# Race-data analysis scripts

Standalone Python scripts that read the HelmLog SQLite (`data/logger.db`)
and produce post-race performance analysis. None of these are wired into
the live web app — they're offline tools for end-of-season debriefs.

The original write-up that drove these (Spring 2026 review for Corvo 105)
lives outside the repo at `/Users/dweatbrook/Backups/helmlog/_analysis/`.
The scripts themselves are kept here so they don't get lost.

## Configuration

All scripts share two env vars (with sensible defaults):

| Env var | Default | What |
|---|---|---|
| `HELMLOG_DB` | `data/logger.db` | Path to the SQLite database |
| `HELMLOG_OUT_DIR` | `.` | Where PNG outputs land (used by `polar_plot.py` and `start_quality.py`) |

You can also pass the DB path as `argv[1]` for any script.

## Scripts

### `full_analysis.py` — VMG / shifts / current

Per-race breakdown:

- Downwind / upwind polar (mean BSP × |cos(TWA)|) per TWS bin
- Shift-tack hit rate (using TWD before/after each tack)
- Current set/drift from steady-leg detection (race + practice)
- Per-race scorecard (% of seconds at ≥90% best-VMG)

Uses Vakaros `race_start` events as the effective gun where available;
falls back to `start_utc + 5 min`. Excludes practice sessions and
RTTS-style long-distance races from the VMG/shift sections.

```
HELMLOG_DB=path/to/logger.db uv run python scripts/analysis/full_analysis.py
```

### `polar_plot.py` — TWA + AWA polar diagrams + heatmap

Generates three PNGs:

- `polar.png` — observed mean BSP per TWA, faceted by TWS bin (TWA on
  the angular axis). Upwind and downwind rendered as separate segments
  with a 90° beam-reach gap (we don't race there).
- `polar_awa.png` — same data, AWA on the angular axis (what the
  apparent-wind gauge would actually read).
- `polar_heatmap.png` — grid of mean BSP per (TWS, TWA).

Requires `matplotlib` and `numpy` (in the project venv).

```
HELMLOG_OUT_DIR=out/ uv run python scripts/analysis/polar_plot.py
```

### `awa_table.py` — TWA → AWA conversion table

Helper for human-readable TWA/AWA cross-reference. Computes
`AWA = atan2(TWS·sin(TWA), TWS·cos(TWA) + BSP)` for every (TWS, TWA)
cell in the dataset, plus a "common targets" lookup (light-up,
heavy-down, etc.).

### `current_and_shifts.py` — earlier current + shift work

Largely superseded by `full_analysis.py` but has slightly different
shift-window logic. Kept for reference.

### `ocs_detect.py` — OCS race detection

For each race with a Vakaros `race_start` event AND `vakaros_line_positions`
endpoints, classify as:

- **clean** — boat on pre-start side at gun
- **OCS — RETURNED** — boat on race side at gun, then crossed back to
  pre-start side
- **OCS? (no return)** — boat on race side at gun, no documented return
  (likely false positive from pre-start positioning noise)

The "pre-start side" is determined by majority side of the boat 60–10 sec
before the gun.

### `start_quality.py` — start matrix + trend chart + favored-end + tack

Computes per race:

- **dist_at_gun** — signed perpendicular distance from line at gun
  (positive = over the line)
- **SOG at gun** — boat speed (GPS-based, calibration-immune)
- **TTL** — time from gun to first sustained race-side crossing (or
  re-crossing, for OCS races)
- **Favored end** — line bias from TWD vs line bearing; reports which
  end was favored, the bias in degrees, and whether the boat went there
- **Tack at gun** — starboard / port from the boat-relative TWA
- **Pre-start trajectory** — position-along-line at gun-3min through gun

Outputs `start_quality_trend.png`. The hardcoded `ocs_ids = {35, 55}`
fallback is for backups taken before the `ocs` tags were added on
corvopi-live; remove it when reading from a fresh backup or from the live
DB.

```
HELMLOG_OUT_DIR=out/ uv run python scripts/analysis/start_quality.py
```

## Design notes

- Scripts are intentionally standalone — no shared library — because
  they evolved iteratively during a single debrief session. If we run
  these monthly, factor out the common bits (`load_aligned`, gun
  resolution, `latlon_to_xy`, line geometry).
- DB reads are read-only; safe to run against the live `helmlog run`
  process via WAL.
- `tests/` doesn't cover these scripts. They're treated as one-shot
  analysis tools, not production code.
- All TWA/AWA conventions: TWA folded to `[0, 180]` (absolute), with
  separate "tack" labels for port/starboard. Wind reference IDs:
  `0 = boat-referenced TWA`, `4 = north-referenced TWD`, `2 = apparent`.

## `video/` — video-derived fleet analysis (#839)

A package rather than a single script: `scripts/analysis/video/` reads the
360° race videos at named instants (gun, roundings, finish), caches frames
and structured observations in a sidecar SQLite ledger, and derives
fleet-relative facts for the season coaching report. Design:
[`docs/video-analysis.md`](../../docs/video-analysis.md). Needs `ffmpeg`
and `yt-dlp` on the Mac; nothing runs on the Pi. Tests live in
`tests/video_analysis/` (run without ffmpeg or network).

```bash
uv run python -m scripts.analysis.video --help
uv run python -m scripts.analysis.video fetch    --race 254        # local 8K export or 4K YouTube download
uv run python -m scripts.analysis.video horns    --race 254        # horn detector → gun + refined sync
uv run python -m scripts.analysis.video instants --race 254        # named instants from the sampling plan
uv run python -m scripts.analysis.video frames   --race 254        # frames + annotated horizon strips
uv run python -m scripts.analysis.video observe  --race 254        # Claude API reads → observations
uv run python -m scripts.analysis.video facts    --season --csv season.csv
uv run python -m scripts.analysis.video report   --season          # tables + charts
uv run python -m scripts.analysis.video reel     --scenario late_boat_end --season
```

Extra env vars on top of `HELMLOG_DB`:

| Env var | Default | What |
|---|---|---|
| `VIDEO_ANALYSIS_DIR` | `data/video-analysis` | Sidecar root: `ledger.sqlite`, `sources/`, `frames/`, `strips/`, `clips/`, `reels/`, `report/` |
| `HELMLOG_META_DB` | `HELMLOG_DB` | Races / videos / Vakaros / maneuvers / results. Lets a fresh table dump from the Pi pair with an older telemetry snapshot |
| `INSTA360_EXPORTS` | `~/Insta360 Exports` | Where the stitched 8K files live; used when the video title matches |
| `ANTHROPIC_API_KEY` | — | For `observe --reader claude-api` |

The ledger is the thing to back up; everything else is reproducible from the
videos. `data/video-analysis/` is gitignored.
