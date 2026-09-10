# Video-derived fleet analysis

> Design doc for turning the 360° race videos into fleet-relative facts,
> cached derived data, and shareable evidence for a season coaching report.
> Tracking issue: [#839](https://github.com/weaties/helmlog/issues/839).

---

## 1. Why

HelmLog logs one boat. Every question that starts with "how did we do
compared to the fleet" runs into that wall: we know our speed, angles,
and track, but not where anyone else was. Results give a finish place
and nothing about how it happened.

The stern-mounted Insta360 X4 sees the whole fleet, the committee boat,
and every mark, every race, from the gun to the finish. Roughly 30 of the
2026 CYC Wednesday races and most of the Ballard Cup races have a linked
video. Read at the right instants and joined to the telemetry, that
footage answers questions no other source can:

- How many boats were ahead of us, and to weather of us, at the gun?
- Which end and which row did we start in, and how late were we?
- Where were we at each rounding, and where did places change hands?
- Did the side we picked on the first beat pay, and did the fleet agree?
- How long did sets and douses take?

The deliverable is a season coaching report whose claims are backed by
facts, data, and video, so the crew can agree on two or three things to
work on rather than argue from memory.

## 2. The source material

### 2.1 What a frame contains

The X4 records with FlowState stabilisation and direction lock, then the
pipeline stitches to equirectangular MP4 (`docs/video-pipeline.md`). The
result is **boat-locked**: the bow is always at the horizontal centre of
the frame and the stern at both edges. Horizontal pixel position maps
linearly to relative bearing:

```
rel_bearing = (x / width) * 360 - 180        # -180 .. +180, bow = 0
abs_bearing = (logged_heading + rel_bearing) % 360
```

With the 1 Hz heading log this gives an absolute bearing to anything in
the frame. Verified on race 254: the sun sat at the bearing the ephemeris
predicts, and a logged tack appears in the video within five seconds of
the stored sync point.

| Property | Local stitched file | YouTube (older races) |
|---|---|---|
| Resolution | 7680×3840 | 3840×1920 (format 315/401) |
| Frame rate | 29.97 | 48 |
| Audio | AAC stereo 48 kHz | Yes (same track) |
| Availability | Late Aug 2026 onward in `~/Insta360 Exports` | Every linked race since April |
| Size per race | 12–16 GB | 7–9 GB, but section downloads work |

The horizon band (roughly rows 1600–2200 of the 8K frame) holds
everything of interest: hulls, sails, marks, committee boat, shoreline.
Stabilisation keeps the horizon level, so **heel is not visible in the
frame**; take heel from telemetry if logged.

### 2.2 What is legible

From the pilot frames (8K local file, evening light):

| Thing | Legible at |
|---|---|
| Hull count and relative bearing | Any distance a hull is above the horizon line, ~800 m |
| Committee boat, marks, pin buoy | Whole course |
| Sail numbers, hull name | Within ~3 boat lengths; occasionally 5 |
| Spinnaker colour (boat ID by kite) | Whole leg |
| Crew on rail vs cockpit on other boats | Within ~5 lengths |
| Own boat: trim, telltales, crew position, hoist/douse state | Always |
| Water texture (pressure), wake on buoys (current) | Within ~300 m, light dependent |

At YouTube's 4K, halve the identification distances; counts are
unaffected.

### 2.3 What the audio contains

The camera's stereo track picks up the RC horns clearly. A band-limited
energy detector (250–1200 Hz, 0.2 s window, robust z-score) on race 254
found the 5-4-1-0 sequence with 60 / 180 / 60 s spacing and no false
pattern match. It also captures crew callouts at conversational level,
which the existing `transcribe.py` pipeline can handle.

## 3. Pilot: race 254 (2026-09-09, CYC race 1)

Run on the local 8K file. Everything below was produced in one session
with ffmpeg, a 60-line horn detector, and a 70-line strip generator.

1. **Gun from audio.** Horns at video t = 168, 228, 408, 468 s. The
   468 s horn is the gun, 01:24:55 UTC. The horn at 108 s is the
   preceding class's one-minute signal.
2. **Sync check.** Stored sync says video t = 0 at session start. A tack
   logged at 01:25:35 appears in the video at 01:25:30 ± 3 s. Good enough
   for counting; see §8 for how the horn refines it.
3. **Frames** at gun −60/−30/0/+30/+60/+120 s and at both roundings.
   Each rendered as four 90° horizon strips with absolute-bearing ticks,
   the true-wind bearing marked, and heading / speed / TWD / TWA / TWS
   stamped from the log.
4. **Read.** Port-tack approach along the line, tacked onto starboard
   under the committee boat at the gun, crossed roughly 20 s late at the
   boat end with about ten boats ahead and to leeward. Tacked to port at
   gun +45 s and went right. Fourth or fifth at the windward mark, fifth
   at the leeward mark. Sail numbers 412 and 174 and one hull name were
   legible during the start.

That is the whole method. The rest of this document is about doing it
thirty times consistently, caching the work, and presenting the result.

## 4. What we can learn

Tiered by how directly the video answers the question and how much
reading effort it costs. Every item names the frames it needs so the
sampling plan in §5.3 covers it.

### Tier 1 — position ladder and start quality (the coaching core)

| Fact | Frames | How it is read |
|---|---|---|
| Boats ahead at gun | gun | Count hulls forward of abeam on the racecourse side |
| Boats to weather at gun | gun | Count hulls on the wind side of the bearing line through TWD |
| Line position (boat third / middle / pin third) | gun −30, gun | Bearing and apparent size of committee boat and pin |
| Row (front / second / buried) | gun, gun +30 | Whether hulls sit between us and the line |
| Seconds late or early | gun, gun +15, +30 | When the committee boat or pin passes abeam; cross-check against track and line if pinged |
| Boats ahead at each rounding | rounding −45, 0, +45 | Count hulls between us and the mark, and hulls already past |
| Boats ahead at finish | finish −60, finish | Count hulls across the line or between us and it |
| Fleet end preference | gun −60, gun | Where the mass of the fleet is relative to the two ends |

From these, per race: a **position ladder** (gun → +2 min → W1 → L1 →
W2 → finish) and a **place delta per leg**. Over the season: where
places are gained and lost, and whether that changed after mid-July,
when results dropped from consistent top-six to consistent bottom-four.

### Tier 2 — the first beat and side choice

| Fact | Frames | How it is read |
|---|---|---|
| Side we took | gun +60 … +240, telemetry | First tack time and track |
| Fleet split | gun +120, +240 | Hulls on each side of the bearing line through TWD |
| Boats left vs right that were ahead at W1 | W1 −45 | Which cluster arrives first |
| First cross count | first port/starboard convergence | Boats we cross ahead of vs behind |
| Layline call | W1 −90 … 0, telemetry TWA | Reaching into the mark (overstood) or two extra tacks (understood) |
| Shift visible on boats ahead | any beat frame | Boats upwind of us heading up or down before the log shows it |

The last one deserves a note. Boats a few hundred metres upwind are a
free wind sensor showing the shift 30 to 90 seconds before it reaches
us. Comparing "when the fleet ahead lifted" with "when we tacked" is a
direct measure of whether we were sailing to the shifts or to the
compass.

### Tier 3 — runs, roundings, and crew work

| Fact | Frames | How it is read |
|---|---|---|
| Set time (bear-away to full and drawing) | W −15 … +90 at 5 s | Kite state in the own-boat view |
| Douse time (kite down to jib trimmed) | L −90 … +30 at 5 s | Same |
| Overlap at the zone, inside or outside | rounding −45, −20, 0 | Relative bearing and range to nearest hulls |
| Gybe count vs boats nearby | run, every 60 s | Kite colour lets you track the same boat across frames |
| Where a boat passed us | run, every 60 s | Bearing walks from ahead-of-beam to abaft |

### Tier 4 — boat-on-boat speed and height

Sail numbers are rarely legible, but **spinnaker colour and hull
colour** identify a boat for a whole leg, and a boat near us at the gun
stays near us long enough to track. For a reference boat on the same
tack:

- **Bearing drift rate** over a steady 60 s tells relative VMG.
- **Apparent size change** (mast height in pixels) tells closing or
  opening range.
- Together: gaining and higher, gaining and lower, losing and higher,
  losing and lower. That is the height/speed trade-off crews argue
  about, measured.

This is the noisiest tier. Treat it as a per-leg qualitative flag, not
a number, unless a boat is within five lengths for the whole window.

### Tier 5 — our own trim and handling

The own-boat half of every frame is always in focus. A frame every 30 s
on steady legs, binned by TWS and TWA, supports:

- Mainsail twist and traveller position by wind band.
- Jib lead and sheet tension by wind band.
- Crew weight placement and how early it moves before a tack or gybe.
- Telltale state at moments when polar percentage drops below a
  threshold for 20 s or more, which often shows the cause: pinching, a
  boat on our air, or a trim change.

This tier is worth doing on a subset (say the best and worst three
races by result) rather than the season, because reading subtle trim
differences across hundreds of frames is slow and the conclusions are
softer.

### Tier 6 — audio and decision timing

- Horn detection for gun, recalls, and shortened course. Already
  validated.
- Transcribed callouts around the start and each rounding, timestamped.
  "Two minutes" called at gun −140 s is a fact about the crew's clock.
  Whether "tack" was called before or after the fleet ahead lifted is a
  fact about decision latency.
- Silence and overlap patterns during roundings, as a crude signal of
  who is talking and whether calls are being acknowledged.

### Tier 7 — environment and course

- **Pressure ahead**: dark water in the upwind strip, matched against
  the TWS that arrived 30 to 60 s later. Speculative but cheap once
  frames exist.
- **Current at marks**: wake direction on the mark and moored boats,
  as a second opinion on the track-derived set and drift.
- **Course geometry from video**: two or three bearings to each line
  end from different pre-start positions triangulate the line to
  roughly 10–20 m. That backfills line bias and rough time-to-line for
  the 26 Wednesday races without Vakaros pings. Not OCS-grade.
- **Line bias as the fleet saw it**, from fleet end preference above,
  versus the bias we called.

## 5. Data model and caching

The expensive step is reading frames, whether by a person or a model.
Everything else is seconds of ffmpeg. The design goal is that **a new
question can usually be answered from cached observations, and when it
cannot, the frames are already on disk.**

### 5.1 Layers

```
L0  source video        local 8K file, or YouTube section download
L1  audio events        horn timestamps per video           (tiny JSON)
L2  frames              full-res JPEG at sampled instants   (~3.5 MB each)
L3  strips / views      annotated horizon strips, reprojected perspective views
L4  observations        structured reads per (race, instant), versioned
L5  derived facts       per-race ladders, season aggregates (recomputed from L4)
L6  clips               MP4 segments cut for scenario reels
```

L0–L3 are keyed by `(video_id, video_t)`. L4–L6 are keyed by
`(race_id, instant)`. The join between them is the race's sync point.

### 5.2 Storage

Offline analysis lives outside the app database, in a sidecar so that no
Critical-tier `storage.py` migration is needed until the facts are ready
to surface in the UI (§9, Phase 4):

```
data/video-analysis/
  ledger.sqlite               # L1, L4, L5 and the index for L0/L2/L3/L6
  sources/<video_id>/         # L0 section downloads (never the full YouTube file)
  frames/<video_id>/<t>.jpg   # L2, t = video seconds to 0.1 s
  strips/<video_id>/<t>_<kind>.jpg
  clips/<race_id>/<scenario>_<n>.mp4
```

The sidecar is `.gitignore`d with the rest of `data/`. The ledger is the
thing to back up; frames and clips are reproducible from L0 in minutes.

Ledger tables:

```sql
audio_events (video_id, video_t, kind, z, dur_s, detector_version)
frames       (video_id, video_t, path, width, height, source, created_at)
observations (id, race_id, instant, utc, video_id, video_t,
              schema_version, reader, reader_version, prompt_version,
              confidence, payload_json, created_at, superseded_by)
facts        (race_id, key, value_json, computed_from, created_at)
```

`observations` are append-only. A re-read with a newer prompt or model
inserts a new row and sets `superseded_by` on the old one. The report
always uses the latest non-superseded row, and any claim can be traced
to the reader and prompt that produced it.

### 5.3 Sampling plan

Fixed per race so later questions are answerable without going back to
the video:

| Window | Instants |
|---|---|
| Pre-start | gun −300, −240, −180, −120, −60, −30, −15 |
| Start | gun, +15, +30, +60, +120, +240 |
| Every leg | every 60 s |
| Each rounding | −90, −45, −20, 0, +20, +45, +90 |
| Set and douse | W −15 … +90 and L −90 … +30, every 5 s (own-boat half only, downscaled) |
| Finish | −120, −60, 0 |

About 60 full frames plus 40 small own-boat crops per race. Thirty races
is roughly 6 GB of frames. Instants are named, not numbered, so
`gun+30` in race 254 and `gun+30` in race 122 line up in every table.

### 5.4 Observation schema (v1)

One record per instant. Fields the reader fills in; nulls allowed.

```json
{
  "race_id": 254, "instant": "gun", "utc": "2026-09-10T01:24:55Z",
  "video_id": "jygj-NbqFJE", "video_t": 467.6,
  "own": {"hdg": 337, "sog": 4.5, "twd": 32, "twa": 55, "tws": 8.6,
          "tack": "stbd", "kite": "down"},
  "line": {"committee_boat": {"rel_brg": 110, "range": "close"},
           "pin": {"rel_brg": -85, "range": "far"}},
  "marks": [{"name": "W1", "rel_brg": 15, "range": "far"}],
  "boats": [
    {"rel_brg": -70, "range": "close", "tack": "stbd", "kite": "down",
     "id": "412", "id_kind": "sail", "conf": 0.9},
    {"rel_brg": -50, "range": "mid", "tack": "stbd", "id": "Paladin",
     "id_kind": "hull_name", "conf": 0.8}
  ],
  "counts": {"ahead": 2, "astern": 9, "to_weather": 0, "to_leeward": 11,
             "between_us_and_line": 0},
  "notes": "Tacked onto stbd under the committee boat; fleet in a line to leeward",
  "confidence": 0.8
}
```

`range` is deliberately categorical (`close` < 3 lengths, `mid` < 8,
`far`). Bearings are relative and converted to absolute at query time
from the logged heading, so a heading correction never invalidates an
observation.

### 5.5 What the cache buys

With L2 and L4 populated for the season, these are queries, not video
work:

- "Every race where we were second row at the gun" → observations where
  `counts.between_us_and_line > 0`.
- "Show me every leeward rounding with an outside overlap" → rounding
  observations with a `close` boat at rel_brg −60 … −120.
- "Did we ever start at the pin?" → line position facts.
- "Which boat do we most often round W1 next to?" → boat IDs at W1 ±45.
- "Rebuild the strips with a 5° yaw correction" → L2 plus telemetry,
  no re-download.

Questions that need a new instant (say gun −45) need L0 and a few
seconds of ffmpeg per race, then a new read. Section downloads are kept
for that reason.

## 6. Tooling

New scripts under `scripts/analysis/video/`, following the existing
offline-tool conventions in `scripts/analysis/README.md`. Dependencies:
`ffmpeg`, `yt-dlp`, `numpy`, `Pillow`. All already present on the dev
Mac; none needed on the Pi.

| Script | Does |
|---|---|
| `fetch.py` | Resolve a race's video, sync, and source (local file or YouTube). Section-download only the windows the sampling plan needs. Record in the ledger. |
| `horns.py` | Band-limited horn detector → `audio_events`. Pattern-match 5-4-1-0 and report a gun candidate with confidence. |
| `instants.py` | Compute the named instants for a race from gun, maneuvers, and finish. Flag races whose rounding count does not match the course. |
| `frames.py` | Extract L2 frames at instants; idempotent against the ledger. |
| `strips.py` | Annotated horizon strips (as in the pilot) and `v360`-reprojected perspective views pointing at a bearing, for reels and close reads. |
| `observe.py` | Assemble the reading packet for an instant: strips, own-boat crop, telemetry stamp, prior observation if any. Store the read. |
| `facts.py` | Recompute L5 from L4: ladders, deltas, side record, set/douse times. Emit CSV and the tables the report uses. |
| `reel.py` | Build a scenario reel (§7.3) from a query over L4. |

`ffmpeg -vf v360=e:flat:yaw=<rel_brg>:pitch=0:h_fov=90:v_fov=60` turns the
equirectangular frame into a normal-looking camera pointed at any
bearing. That is what reels use, so a viewer sees "the committee boat
end as we approached it" rather than a warped panorama.

### 6.1 Reading frames

The pilot read frames with a model in the loop. That is the plan for
the season pass as well, with three guardrails:

- **Fixed prompt, versioned.** The prompt names the instant, gives the
  telemetry stamp, defines the count rules (§4, Tier 1), and asks for
  the v1 schema. Prompt text is stored with the observation.
- **Human spot-check.** Ten percent of instants, weighted toward guns
  and roundings, get a second read by a person. Disagreement above one
  boat in any count triggers a re-read of that race.
- **Confidence is recorded**, and the report drops counts below 0.6
  rather than averaging them in.

## 7. Sharing results

The report has to work for people who were on the boat and remember it
differently. Every claim therefore carries three things: the fact, the
data behind it, and a way to look at the video.

### 7.1 Claim cards

The unit of the report. Short enough to read in thirty seconds:

```
Late at the boat end
We started in the boat-end third in 14 of 22 races and crossed more than
15 s late in 9 of those 14. In the pin third we were late twice in 8.

  race   end    late_s  ahead_at_gun  ahead_at_W1  finish
  254    boat     20         10            4          ?
  208    boat     25         12            9         10
  ...

Evidence: gun+30 strips for the nine late races (thumbnails), each
linking to the YouTube deep link (?t=) and to the moment on the session.
```

A card states one thing, shows the rows that support it, and links out.
The season report is twenty to thirty cards grouped by theme, with a
one-page summary of the three the crew should act on.

### 7.2 Moments and deep links

Each Tier 1 fact becomes a moment on the session (`moments` with a
`start` or `rounding` anchor) with the strip attached
(`moment_attachments`, kind `image`). The existing video panel already
deep-links a moment to `?t=` on YouTube. That means a crew member
reading the debrief can click from "ten boats ahead at the gun" to the
frame and to the video at that second, without the report having to
embed anything.

### 7.3 Scenario reels

A scenario is a named query over observations plus a camera direction
and a data overlay. The reel is every instance in the season, cut
together, with a caption card between clips and a data strip along the
bottom.

```
scenario: late_boat_end
  select:   instant=gun, line.position=boat_third, facts.late_s > 15
  window:   gun-45 .. gun+30
  look_at:  committee_boat          # v360 yaw follows the bearing per frame
  overlay:  countdown, sog, twa, boats_ahead
  caption:  "{race.name} — crossed {late_s}s late, {ahead} boats ahead"
```

Reels worth building first, because each maps to a coaching
conversation:

| Reel | Query | Camera |
|---|---|---|
| Late at the boat end | as above | committee boat |
| Second row | `between_us_and_line > 0` at gun | bow, wide |
| Wrong side | first-beat side ≠ side of the W1 leaders | to weather |
| Slow set | set time > 75th percentile | own boat, foredeck |
| Slow douse | douse time > 75th percentile | own boat, foredeck |
| Outside at the leeward mark | `close` boat inside at L −20 | the mark |
| Rolled on the run | reference boat bearing walks from abaft to ahead | that boat |
| Shift missed | fleet ahead lifted ≥ 45 s before our tack | to weather |
| The good ones | front row, on time, top three at W1 | bow, wide |

The last reel matters as much as the others. A coaching report that is
all failure gets ignored.

Commentary is generated from the observation and the telemetry
("crossed 20 s late, 4.5 kt, ten boats ahead to leeward") and then edited
by a person before the reel is shared. Reels are MP4 plus a markdown
page that lists every clip with its YouTube deep link, so anyone who
wants the original context can get it.

### 7.4 Season aggregates

A handful of tables and charts, all computed from L5:

- Position ladder per race, and the season median ladder.
- Place delta per leg, box plot across the season.
- Start position (end × row) against finish place.
- Side-choice record: went left / right, fleet majority, W1 leaders'
  side, our W1 place.
- Set and douse time trend.
- The mid-July break: every metric above, before and after 2026-07-13.

## 8. Method validation and error budget

| Quantity | Error | Source | Mitigation |
|---|---|---|---|
| Bearing | ±3° | Camera yaw offset from centreline, stabiliser drift | Calibrate yaw once per mount against the sun or a pinged mark; log per video |
| Sync | ±5 s (stored), ±1 s (refined) | Session-start sync is coarse | When a Vakaros gun and a detected horn both exist, their pair is an exact sync point; store it per race |
| Gun (no Vakaros) | ±1 s | Horn detector | Require the 5-4-1-0 pattern, not a single blast; flag single-blast candidates |
| Counts | ±1 in the open, ±2 in a cluster | Overlapping hulls | Two frames 15 s apart for every gun count; report the range |
| Late seconds | ±5 s | Which end passed abeam when | Cross-check with track and line where pinged (13 races) |
| Identification | Often none | Resolution | Accept `id: null`; use kite colour on runs |
| Roundings | Mislabeled pre-start turns | Maneuver detector | Only roundings after the gun; first is W1; flag count mismatches |

Validation steps before the season pass is trusted:

1. Horn gun vs Vakaros gun on all 26 races that have both. Target: 100 %
   agreement within 2 s.
2. Video-derived line position vs the 13 pinged lines. Target: the
   third is right in 12 of 13.
3. Model read vs human read on 20 instants. Target: counts within one
   boat on 90 %.

## 9. Phases

1. **Tooling.** The scripts in §6, the ledger, the sampling plan, the
   v1 schema. Re-run the pilot through them so race 254 is the first
   ledger entry.
2. **Season observation pass.** Every 2026 Wednesday race with a linked
   video. Depends on the July-onward Vakaros import for gun events;
   horns are the fallback. Human spot-checks per §6.1.
3. **Season report.** Claim cards, aggregates, the first five reels,
   and a one-page summary. Delivered as a `docs/archive/` entry plus the
   reels in `data/`.
4. **Surface in HelmLog.** Observations become moments with attached
   strips; a `video_observations` table if the sidecar has earned it;
   the debrief page shows the position ladder and the gun strip. This
   is the point at which `storage.py` changes and the Critical-tier
   process applies.

## 10. Licensing and privacy

`docs/data-licensing.md` treats video as boat-private PII, with crew
holding rights over their likeness. This work adds two things that
policy should be read against:

- **Other boats' crews are in the frames.** Counting hulls and reading
  sail numbers is no different from reading results, which are public.
  Reels that show identifiable people on other boats are a different
  matter. Reels stay with our crew and coach; frames and clips live in
  `data/`, not in the repo or on YouTube.
- **Observations name competitors** by sail number. That is results-level
  information and can live in the ledger. It should not flow to a co-op
  peer under the current policy without a specific decision.

Nothing here changes what is shared with peers today.

## 11. Open questions

- **Sail-number legibility at 4K.** The pilot was at 8K. If 4K makes
  identification rare, Tier 4 collapses to kite colour only. Test on an
  April race before committing to Tier 4.
- **Whether to reproject before reading.** A perspective view at the
  bearing of interest may read better than the strip. Cost is one
  ffmpeg pass per view.
- **Where the yaw calibration lives.** Per video, since the mount moves
  between race days.
- **Second camera.** Some races have a bow camera. It doubles the
  identification range for boats ahead. Not needed for Tier 1.
- **How far to take audio.** Horns are done. Transcribing thirty starts
  is a few hours of Whisper time and a separate review effort.

## 12. Related

- `docs/video-pipeline.md` — how the videos get made and linked.
- `docs/grafana-video-links.md` — existing deep-link behaviour.
- `scripts/analysis/README.md` — the offline analysis scripts this
  work joins, in particular `start_quality.py` and `ocs_detect.py`.
- `src/helmlog/video.py` — the sync-point model.
- `src/helmlog/maneuver_detector.py` — rounding detection.
- `src/helmlog/anchors.py`, `moments` — where facts land in the UI.
- Ideation log IDX-009 — self-hosted video, which would make clips
  first-class in the app.
