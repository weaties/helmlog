# Release Notes

## GPS-Disciplined Timestamps, Safer Backups & Moments Markdown (2026-07-02)

The headline this cycle is time integrity: the Pi runs Signal K locally, so
a boot before NTP/GPS sync used to stamp a whole recording — and its race and
audio boundaries — at the wrong UTC. The logger now disciplines every
timestamp to GPS time. Alongside that, a silent backup-corruption bug is
fixed, Moments comments render real Markdown, per-race polars stop being
skewed by prestart maneuvering, and the B&G start-timer port clears its
hardware-validation gate.

### GPS clock discipline & session integrity

- **Recording timestamps disciplined to GPS time** — new `clock_sync.py`
  derives the host↔GPS offset from `navigation.datetime` (PGN 126992/129033),
  median-smoothed against outliers, and stamps records on GPS time. It never
  blocks recording: with no GPS fix it falls back to host time and flags the
  recording `unverified`. Each live recording now carries a clock provenance
  flag (`synced` / `corrected` / `unverified` / `ref_lost`).
  ([#794](https://github.com/weaties/helmlog/pull/794))
- **Race and audio boundaries share the telemetry clock** — web-route
  timestamps were left on the raw host clock, which split an unsynced-boot
  recording (race 203 landed ~44 min behind its own data). The GPS offset is
  now mirrored onto the storage singleton so race windows and audio sessions
  stamp on the same time base as the instrument stream.
  ([#797](https://github.com/weaties/helmlog/pull/797))
- **Races close at their last data point, not wall-clock `now`** — a race
  left open when power died was later stamped with `now`, producing bogus
  multi-hour spans (a ~14.8 h race that pegged a core warming its cache on
  boot). The end is now anchored to the last real data point when a gap
  precedes the close. ([#786](https://github.com/weaties/helmlog/pull/786))
- **Lock-resilient maneuver detection** — the maneuvers panel and re-detect
  wrote to SQLite on read paths and lost races with the live logger
  (`database is locked` → 500s). Read paths no longer write back when the
  value is already stored, and transient locks are logged instead of bubbling
  up. ([#776](https://github.com/weaties/helmlog/pull/776))

### Race start-timer & polar analysis

- **B&G/Simrad start-timer port — hardware validation (Phase 0)** — a
  read-only PGN sniffer plus an admin `/admin/pgn-audit` page confirm from a
  phone on the water whether the boat's Triton² instruments emit the race
  timer PGNs (130845 Set, 130850 Start/Stop) before any of the port is built.
  On-water validation passed. ([#790](https://github.com/weaties/helmlog/pull/790))
- **Polars only see racing data** — the per-race polar diagram and baseline
  were computed over the whole session, so prestart maneuvering and
  post-finish sailing biased both. A shared `race_window()` now trims to the
  gun (start-timer/Vakaros anchored) through the detected finish.
  ([#812](https://github.com/weaties/helmlog/pull/812))

### Moments & replay

- **Markdown in Moments comments** — comment bodies rendered as escaped flat
  text, collapsing paragraphs, lists, and emphasis. They now render
  GitHub-Flavored Markdown (vendored markdown-it → DOMPurify, strict
  allowlist), with a creator-only edit affordance on every comment.
  ([#809](https://github.com/weaties/helmlog/pull/809))
- **Replay boat marker hides outside the GPS track** — the scrubber extends
  ~20 min into the prestart, but the GPS track starts at the gun, so the boat
  used to sit parked on the "Start" dot for that window. The marker is now
  hidden until replay time re-enters the track's span.
  ([#806](https://github.com/weaties/helmlog/pull/806))

### Backups, infrastructure & tooling

- **Backups no longer ship corrupt DB snapshots** — `backup.sh` staged the
  `sqlite3 .backup` on `/tmp`, a 4 GB tmpfs; once the DB grew past ~4 GB the
  backup ran out of space and left an all-zeros file that got rsync'd over the
  good copy (only `test -f` was checked). Snapshots now stage on disk-backed
  `/var/tmp` and are honoured only when `.backup` exits 0 **and** passes
  `PRAGMA quick_check`. ([#808](https://github.com/weaties/helmlog/pull/808))
- **shiftsimulator coexists on corvopi-live** — nginx proxies `/sim/` to the
  shiftsimulator service on `127.0.0.1:8765`, mirroring the `/grafana/` and
  `/signalk/` routes; additive proxy only.
  ([#802](https://github.com/weaties/helmlog/pull/802))
- **`AGENTS.md` is now the canonical agent guide** — the drifted CLAUDE.md and
  AGENTS.md are reconciled into one tool-agnostic `AGENTS.md`, with CLAUDE.md
  reduced to an `@AGENTS.md` import plus Claude-Code-only mechanics.
  ([#804](https://github.com/weaties/helmlog/pull/804))
- **Skill anti-rationalization tables & judgment rules** — the `tdd`,
  `pr-checklist`, and `data-license` skills pair each step-skipping excuse
  with a written rebuttal, and CLAUDE.md gains explicit judgment rules.
  ([#778](https://github.com/weaties/helmlog/pull/778))
- **CI:** bump `actions/checkout` from 6 to 7.
  ([#800](https://github.com/weaties/helmlog/pull/800))

## Per-Race Crew, Body Weight, and Gear Weight (2026-05-13)

Crew weight is now a first-class part of every race rather than a
boat-wide default that never moves. Setting the crew for a race flips
the lineup from "assumed default" to "verified", a new gear preset
(dry/wet/foulies) fills in clothing weight for the session, and the
analysis API now reports verified-vs-assumed and total crew weight so
debrief findings can finally tell apart "we sailed underweight" from
"we sailed badly."

- **Verified-vs-assumed flag on every race** — new `races.crew_assumed`
  column defaults to 1 (the boat default is unverified for this race)
  and flips to 0 the moment a crew or admin saves a per-race crew via
  `POST /api/races/{race_id}/crew`. Existing historical races stay
  flagged as `assumed` until a backfill confirms them.
- **Gear preset per race** — `races.gear_preset` records one of
  `dry`/`wet`/`foulies` (free-form NULL allowed). Confirming a crew
  with a preset fills in any per-entry `gear_weight` that wasn't set
  explicitly, so totals stay consistent without the user having to
  re-enter clothing weight for every position.
- **Per-user gear default** — new `users.gear_default_lbs` so a sailor
  who always shows up in heavy foulies (or never does) doesn't get
  surprised by the preset on every race. The analysis API falls
  through to this when a per-race row omits gear weight.
- **Admin backfill endpoint** —
  `POST /api/admin/crew-backfill` accepts a list of
  `{race_id, crew, gear_preset}` and applies them in one batch, with
  one audit-trail row per race for protest readiness (data-licensing
  §11).
- **Anonymisation now clears weight data** —
  `anonymize_sailor` and the user soft-delete path both NULL out
  `users.weight_lbs`, `users.gear_default_lbs`, and any per-race body
  and gear weights referencing the user (data-licensing §1).
- **Analysis API surfaces it all** —
  `/api/sessions/{id}/summary` now includes a `crew` block with the
  resolved lineup, the assumed flag, the gear preset, and totals for
  body, gear, and crew weight. `/api/races/{id}/crew` returns the
  same fields alongside the existing crew list. Closes #761.

## Moments Unified, Sharper Debriefs, Safer Backups (2026-04-24)

Bookmarks, comment threads, and notes are now one thing — "Moments" —
with a unified panel on the session page. Debrief audio matches race
audio channel-for-channel. The box no longer freezes for two minutes
the first time you open a completed session. And restoring from a
backup now tells you immediately if photos are missing.

### Moments — one panel, one tag system
- **Bookmarks, notes, and threads collapse into Moments**
  ([#663](https://github.com/weaties/helmlog/pull/663),
   [#672](https://github.com/weaties/helmlog/pull/672),
   [#673](https://github.com/weaties/helmlog/pull/673)) — the old
  Bookmarks / Notes / Discussion cards are replaced by a single Moments
  panel that holds every race observation, anchored to the right place
  on the timeline. Existing data migrates forward automatically
- **Moment UX v2** ([#678](https://github.com/weaties/helmlog/pull/678))
  — subject-first edit flow, a counterparty chip for tagging other
  boats in a protest/mark-room note, and an attachment gallery that
  treats photos and videos the same way
- **Tag provenance + starter vocabulary**
  ([#674](https://github.com/weaties/helmlog/pull/674)) — every tag
  records who created it, and fresh installs come with a starter set
  of sailing-specific tags instead of a blank list
- **Phone/ESP32-CAM photo ingest**
  ([#661](https://github.com/weaties/helmlog/pull/661)) — ESP32-CAM
  devices can push JPEGs straight into the Moments panel of the active
  race session via a simple HTTP endpoint

### Home page — racing view by default
- **Stripped-down Home during a race**
  ([#665](https://github.com/weaties/helmlog/pull/665)) — while racing,
  Home is just the live track, instruments, setup summary, and notes.
  Everything non-essential fades away so the helm can find what they
  need at a glance

### Debrief audio
- **Multi-channel parity with race audio**
  ([#649](https://github.com/weaties/helmlog/pull/649)) — when debrief
  recording starts, it now honors the same per-channel naming/labels
  the race recorder was using. If the USB device set changed between
  race end and debrief start, the operator sees a warning instead of
  silently dropping tracks

### Performance
- **Polar grading no longer freezes the Pi**
  ([#680](https://github.com/weaties/helmlog/pull/680)) — the first
  view of a completed session used to block every API on the box for
  2–3 minutes while it graded ~400 segments. Grading now runs in a
  worker thread with the full polar baseline pre-loaded, and is warmed
  up in the background the moment the race ends, so the debrief page
  is ready by the time the crew opens it
- **Hot-path SQLite indexes**
  ([#671](https://github.com/weaties/helmlog/pull/671)) — adds the
  missing `audio_sessions.race_id`, `winds(reference, ts)`, and
  latest-instrument reverse indexes that the race list and live
  instrument panel hit on every refresh
- **Stale summary cache fixed after results import**
  ([#668](https://github.com/weaties/helmlog/pull/668)) — importing a
  regatta results file now invalidates the cached session summary so
  finish positions show up immediately instead of on the next restart

### Backups & restore
- **Atomic SQLite snapshots and integrity validator**
  ([#681](https://github.com/weaties/helmlog/pull/681)) — `backup.sh`
  uses `sqlite3 .backup` to stage a consistent DB snapshot before
  transferring, replacing the WAL-checkpoint approach that could race
  with active writes. Every snapshot (and every restore) is now
  cross-checked: if `moment_attachments`, `audio_sessions`, or
  `users.avatar_path` rows point at files that aren't actually in the
  snapshot, the backup report says so and `restore.sh` warns before
  wiping the target. Catches the "25 photo rows, 0 photo files" class
  of silent data loss

### Docs
- **Tightened CLAUDE.md** ([#669](https://github.com/weaties/helmlog/pull/669))
  — AI agent instructions reduced to 177 lines and docs sprawl tamed

## Smarter Maneuver Analysis, Bookmarks & Tags, Heel/Trim, Faster Pages (2026-04-21)

Better debrief tools: deeper maneuver analysis, bookmarks and tags to
organize moments across your season, heel and trim now recorded and
shown, a faster history page, and a home page that opens right on your
latest race.

### Smarter maneuver analysis ([#640](https://github.com/weaties/helmlog/pull/640))
- **Turn vs. recovery timing** — each tack and gybe is now split into
  two phases: how fast the helm got the boat through head-to-wind, and
  how fast speed came back afterward. Slow turn vs. slow recovery are
  different problems and now show up separately in tooltips and the
  maneuver browser
- **Fairer baseline** — entry speed is now measured from the steady part
  of the approach, not dragged down by a helm bleeding speed in the
  last few seconds before ready-about, so distance-lost numbers are
  more trustworthy
- **No more fake good/bad labels** — when every maneuver in a session is
  tightly clustered, they're all labeled "consistent" instead of
  inventing a best-to-worst split out of noise. Rank chips also show a
  percentile so you still see ordering
- **Overlay chart** — new button on the session page and maneuvers
  browser that plots selected tacks/gybes on a shared timeline anchored
  to head-to-wind, so you can see at a glance which one turned fastest
  or recovered cleanest
- **Historical sessions update automatically** — every past session gets
  re-analyzed in the background on the next start, so the new numbers
  appear everywhere without any manual rebuild

### Bookmarks, anchored comments & tags ([#588](https://github.com/weaties/helmlog/issues/588))
- **Bookmarks** ([#589](https://github.com/weaties/helmlog/pull/589))
  — mark any moment in a session for later reference
- **Anchored comment threads** ([#592](https://github.com/weaties/helmlog/pull/592))
  — pin discussions to a specific maneuver, bookmark, or moment; the
  thread highlights as you scrub past it during replay
- **Tags across everything** ([#595](https://github.com/weaties/helmlog/pull/595))
  — one tag system for sessions, maneuvers, threads, and bookmarks,
  with filter chips on lists and an admin page for renaming and merging

### Heel & trim
- **Recorded from the boat** ([#624](https://github.com/weaties/helmlog/pull/624))
  — helmlog now captures heel and trim alongside everything else
- **Shown on the session page** ([#646](https://github.com/weaties/helmlog/pull/646))
  — new Attitude card in the gauges panel

### Home page & session navigation
- **Home page opens on your latest race** ([#636](https://github.com/weaties/helmlog/pull/636))
  — live view while racing, most recent race otherwise. Start/stop and
  crew controls moved to a dedicated `/control` page so a spectator
  can't accidentally end a race
- **Video overlays during maneuvers** ([#641](https://github.com/weaties/helmlog/pull/641))
  — optional compass-rose and mini-track overlays fade in over the
  session video only during tacks/gybes, keeping the long upwind and
  downwind stretches clean
- **Share a link to a specific moment** ([#643](https://github.com/weaties/helmlog/pull/643))
  — new Share button copies a URL that opens the session paused at the
  current playback time, just like YouTube's "share at current time"
- **Synthesized sessions now show on history** ([#591](https://github.com/weaties/helmlog/pull/591))
  — sessions created from imported results are no longer hidden

### Faster pages
- **Session history, summary, tracks and wind fields load from cache**
  ([#594](https://github.com/weaties/helmlog/issues/594)) — repeat views
  of the same race skip the heavy recomputation and come back almost
  instantly. Browser-side revalidation lets the browser reuse its copy
  too
- **Weather and tide lookups are cached** — the logger no longer hits
  Open-Meteo and NOAA for the same forecast twice
- **History is pre-warmed** — when a race ends, its summary is built in
  the background so the first time you open it in history it's already
  ready

### Video upload reliability
- **No more half-rendered clips on YouTube** ([#634](https://github.com/weaties/helmlog/pull/634))
  — the uploader now waits until the Insta360 export is fully finished
  and verifies YouTube accepted it before moving the source file off
  the exports disk, fixing a case where partially-rendered videos were
  being uploaded and the originals archived before you could re-export

### Setup for new boats
- **New-owner bootstrap guide** ([#627](https://github.com/weaties/helmlog/pull/627))
  — step-by-step guide for setting up helmlog on a new Pi for a new
  boat
- **Fresh installs no longer inherit the original owner's identity**
  ([#629](https://github.com/weaties/helmlog/pull/629)) — scripts now
  prompt for required settings instead of silently using defaults from
  the upstream project

### Fixes
- **Importing multi-class regatta results no longer fails**
  ([#606](https://github.com/weaties/helmlog/pull/606)) — second
  regattas sharing a class (e.g. J/105) with an already-imported one
  now import cleanly

## Maneuver Video Compare + Cross-Session Browser (2026-04-16)

Side-by-side synced video comparison for detected maneuvers, followed by a
full cross-session browser that lets you pool tacks, gybes, roundings, and
synthesized starts across many sessions and filter by regatta, session type,
wind range, and mark side — then compare the selected clips together.

### Maneuver video compare page
- **Synced multi-video playback** ([#566](https://github.com/weaties/helmlog/pull/566))
  — open a compare grid from the session page, pick maneuvers, and play their
  YouTube clips side-by-side with a shared play/pause, speed, and pre-roll
- **Per-video and global offset controls** ([#569](https://github.com/weaties/helmlog/pull/569))
  — nudge any cell ±0.5s to align, plus a global offset that shifts all cells
  together; restyled Compare button on the session page
- **Per-video SVG track overlay** ([#571](https://github.com/weaties/helmlog/pull/571))
  — each cell shows the maneuver's local track rotated into a wind-up frame
  with a ladder reference line, colored by rank
- **Compass-rose instrument gauge overlay** ([#573](https://github.com/weaties/helmlog/pull/573))
  — top-left overlay with HDG, BSP, SOG, TWS/AWS readouts and TWD/AWA/COG
  arrows that update in sync with the video
- **BSP recovery bar gauge** ([#575](https://github.com/weaties/helmlog/pull/575))
  — vertical bar showing live speed as a percentage of entry BSP, colored by
  recovery state, with a dashed min-BSP marker
- **P→S / S→P direction filter pills on maneuvers panel** ([#577](https://github.com/weaties/helmlog/pull/577))
  — filter tacks and gybes by turn direction from the session-page maneuvers
  card
- **Mute-all button, muted by default** ([#579](https://github.com/weaties/helmlog/pull/579))
  — multiple simultaneous audio tracks are never useful; cells start muted
  with a single toggle
- **Collapsible filter panel** ([#581](https://github.com/weaties/helmlog/pull/581))
  — type / direction / rank / post-start pills hide behind a toggle so the
  grid keeps maximum room for video
- **Watch on YouTube link per cell** ([#583](https://github.com/weaties/helmlog/pull/583))
  — deep-link each maneuver to its moment on the original full-race video

### Cross-session maneuver browser ([#584](https://github.com/weaties/helmlog/issues/584), [#585](https://github.com/weaties/helmlog/pull/585))
- **Top-level `/maneuvers` page** — pools tacks, gybes, roundings, and starts
  across many sessions so a debrief can pull a statistically meaningful set
  of samples at similar wind speeds from across a season, not just one race
- **Generalized compare URL** — `/compare?ids=<session_id>:<maneuver_id>,...`
  accepts pairs from different sessions; the legacy
  `/session/{id}/compare?ids=1,2,3` URL keeps working for back-compat
- **Filter pills** — session type (race / practice), type (tack, gybe,
  rounding, weather, leeward, start), direction, race phase (post-start
  only), and wind-range pills that multi-select to union non-adjacent bands
  (e.g. 6–8 + 12–15)
- **Weather vs. leeward roundings** — each rounding is classified by exit
  TWA (downwind exit = weather mark, upwind exit = leeward) and displayed
  inline as "rounding (W)" / "rounding (L)", with dedicated pill filters
- **Synthesized start "maneuvers"** — each session with a recorded Vakaros
  race_start event gets a synthetic start entry at the gun time, pickable
  and comparable like any other maneuver
- **Start-cell overlay** — compare cells of type `start` show a live
  countdown (T-0:30 pre-gun → GUN at zero → T+0:15 post-gun) and a
  perpendicular distance-to-line readout, computed per-tick from the
  session's start line and track data
- **Zero-maneuver and imported-results sessions hidden** — the picker only
  shows live-sailed sessions that actually have maneuvers, skipping the
  ghost races that multi-class result imports create

### Fixes
- **409 instead of 500 on duplicate regatta adds** ([#563](https://github.com/weaties/helmlog/pull/563))
  — the admin Results page now surfaces a clean conflict rather than an
  internal server error when re-adding a regatta

## Sprint 8 — Race Replay, Multi-Channel Audio, Results Import & Session Page Overhaul (2026-04-15)

Full race-replay UI with polar-graded track and event stepping, multi-channel
audio recording with per-position mic isolation and parallel sibling-card
capture, Clubspot/STYC race-results import with session linking, polar
analysis overhauls, debrief audio on the session page, a rebuilt history row
summary, and a session page with wind overlay, resizable track map, and
collapsible gauge layers.

### Race replay ([#464](https://github.com/weaties/helmlog/issues/464), [#516](https://github.com/weaties/helmlog/pull/516), [#521](https://github.com/weaties/helmlog/pull/521))
- **Playback core** ([#465](https://github.com/weaties/helmlog/issues/465),
  [#468](https://github.com/weaties/helmlog/issues/468)) — play/pause/speed
  scrubber with pause-on-seek, gauge cards with sparklines, and a unified
  clock that fans seeks out from YouTube and multi-channel audio scrubbers
  back to the map cursor and replay controls
- **Polar-graded track** ([#469](https://github.com/weaties/helmlog/issues/469),
  [#470](https://github.com/weaties/helmlog/issues/470)) — per-segment grading
  pipeline drives the polyline color by polar %; SK track averaged per second
  with source address captured per fix, decimated to match Vakaros vertex
  density, and smoothed for display
- **Boat cursor** — rotating boat icon interpolated between GPS fixes with
  smoothed rotation, a follow-boat toggle, and crisper HDG/COG indicators
- **Maneuver markers & event stepping** ([#475](https://github.com/weaties/helmlog/issues/475),
  [#466](https://github.com/weaties/helmlog/issues/466)) — clickable markers
  on track, keyboard-driven event navigation, and reclassification of
  large-turn tacks/gybes as roundings
- **Laylines & course overlay** ([#473](https://github.com/weaties/helmlog/issues/473))
  — layline kind split by mark, anchored to roundings, wind-frame math fixed,
  type-aware scaling with leeward cap, start-line laylines with 120s grace
  window, and Vakaros race-gun honored as effective start
- **Derived current overlay** ([#523](https://github.com/weaties/helmlog/issues/523),
  [#538](https://github.com/weaties/helmlog/pull/538)) — phase 1: derive and
  render current vectors on the replay map
- **Cross-surface playback** — media players made independent, audio target
  re-applied on play/loadedmetadata, YouTube clamped to clock state so
  pause-on-seek sticks, throttled YT seekTo

### Multi-channel audio & sibling capture ([#462](https://github.com/weaties/helmlog/issues/462), [#509](https://github.com/weaties/helmlog/issues/509))
- **Schema & consent** ([#493](https://github.com/weaties/helmlog/issues/493))
  — v63 channel_map + transcript_segments + voice biometric consent audit
- **Hardware discovery & capture** ([#494](https://github.com/weaties/helmlog/issues/494))
  — pyudev USB device detection and multi-channel recording in `audio.py`
- **Per-channel transcription** ([#495](https://github.com/weaties/helmlog/issues/495))
  — per-channel ASR with unified time-sorted segment merge
- **Channel mapping UI** ([#496](https://github.com/weaties/helmlog/issues/496),
  [#497](https://github.com/weaties/helmlog/issues/497)) — admin channel-map
  page, voice consent acknowledgement flow, and per-session overrides from
  the home page
- **Mixed playback + isolation** ([#498](https://github.com/weaties/helmlog/issues/498))
  — Web Audio mixed playback with segment-click channel isolation
- **Export & deletion** ([#499](https://github.com/weaties/helmlog/issues/499))
  — multi-channel WAV export preserves all channels; atomic per-channel
  deletion satisfies data-licensing right-to-erasure
- **Sibling-card parallel capture** ([#509](https://github.com/weaties/helmlog/issues/509))
  — `AudioRecorderGroup` opens one mono USB receiver per sibling with a
  shared `capture_group_id`; transcription fans out across siblings and
  merges into one time-sorted transcript; session page plays every sibling
  via multi-source Web Audio
- **Per-receiver native players** ([#525](https://github.com/weaties/helmlog/issues/525),
  [#540](https://github.com/weaties/helmlog/pull/540)) — stacked native
  `<audio controls>` below the mixed player, one per receiver, with per-row
  download; starting one pauses the mix and any other playing sibling
- **Capture hotfixes** — PortAudio re-init on Linux detection, pyudev sound
  subsystem filtered to `card*`, `AudioRecorderGroup` accepted alongside
  `AudioRecorder` on the web layer, and graceful race-start audio failure

### Race results import ([#459](https://github.com/weaties/helmlog/issues/459), [#460](https://github.com/weaties/helmlog/pull/460))
- **Clubspot provider & importer** — schema v59 with provider protocol, idempotent Clubspot importer, and bundled test fixtures
- **STYC HTML provider** — STYC results import with place dedup and a `series.htm` fallback ([#526](https://github.com/weaties/helmlog/issues/526))
- **Admin Race Results page** — browse imported regattas, trigger imports, link imported races to local sessions
- **Auto-discover** ([#524](https://github.com/weaties/helmlog/pull/524),
  [#526](https://github.com/weaties/helmlog/issues/526)) — Clubspot regatta
  name + classes and STYC series/races discovered from a single URL
- **Session linking** ([#520](https://github.com/weaties/helmlog/issues/520))
  — auto-link imported races to local sessions on import, pair by order,
  match by stored date column, `/rematch` endpoint + admin button, session
  page picker to link results manually
- **History & session page polish** — imported races hidden from session
  history listing, session page shows linked fleet results, source links on
  the results page, class ID validation

### Polar analysis ([#534](https://github.com/weaties/helmlog/issues/534), [#536](https://github.com/weaties/helmlog/issues/536))
- **Full-circle polar diagram** — port/stbd split with panel split by
  point-of-sail and tack; upwind and downwind curves stay disconnected
- **Panel filters** — position, tack, TWS range, delta, and race phase
- **Click-to-highlight** — polar diagram dot click highlights matching replay
  segments via enriched grade mapping carrying bsp + target
- **Baseline race window** ([#536](https://github.com/weaties/helmlog/issues/536),
  [#537](https://github.com/weaties/helmlog/pull/537)) — exclude pre-start
  from polar baseline

### Maneuvers UI ([#531](https://github.com/weaties/helmlog/pull/531))
- **Filter pills** — stackable type/tack/rank/post-start filters on the
  maneuvers card, overlay respects them, and the enriched payload is cached
  per session
- **Tracks & markers** — smoother maneuver tracks with speed-recovery
  markers, tack direction labels corrected, track ticks, and optional
  Vakaros overlay with SK/Vakaros source toggles and row→overlay highlight

### Backup & reporting ([#518](https://github.com/weaties/helmlog/issues/518), [#519](https://github.com/weaties/helmlog/pull/519))
- **SSH preflight, safety gate, and email reports** — backup script now
  pre-checks the SSH target, refuses to clobber, and mails a summary on
  completion

### CI & test hygiene
- **Nightly review + bisect** ([#514](https://github.com/weaties/helmlog/pull/514),
  [#515](https://github.com/weaties/helmlog/pull/515)) — replace per-PR
  Claude review with a nightly-on-main job that bisects regressions, plus
  follow-up storage/CI fixes
- **write_audio_session identity regression guard** ([#517](https://github.com/weaties/helmlog/pull/517))

### Bug fixes
- **Start-line bias sign** ([#529](https://github.com/weaties/helmlog/pull/529))
  — pin→boat bias sign inversion fixed
- **Imported races break /api/state** ([#532](https://github.com/weaties/helmlog/issues/532),
  [#533](https://github.com/weaties/helmlog/pull/533)) — naive/missing
  start_utc on imported rows no longer 500s the home endpoint; v68/v69
  migrations backfill existing rows
- **Ghost "open" imported races** ([#541](https://github.com/weaties/helmlog/issues/541),
  [#542](https://github.com/weaties/helmlog/pull/542)) — importer now writes
  `end_utc = start_utc` at insert time so imported rows never get picked up
  by `get_current_race`; the dead `start_utc LIKE '%T%'` filter removed
- **Backup rsync partial-transfer** ([#544](https://github.com/weaties/helmlog/issues/544),
  [#545](https://github.com/weaties/helmlog/pull/545)) — treat rsync rc=23/24
  as warning instead of failure in backup reporting
- **Clubspot multi-race linking** ([#551](https://github.com/weaties/helmlog/pull/551))
  — fix session linking for multi-race days; admin rematch can force-relink
  existing rows

### Debrief audio on session page ([#546](https://github.com/weaties/helmlog/issues/546), [#547](https://github.com/weaties/helmlog/pull/547))
- **Surface debrief audio + transcript** — attached debrief audio and its
  transcript now render on the session page alongside the race audio
- **Click-to-seek** — clicking a debrief transcript segment seeks the debrief
  audio player
- **Consolidated layout** — race transcript lives under the race audio player,
  debrief renders as a collapsible subsection below it
- **History cleanup** — attached debriefs hidden from the default history
  list so they don't double-count as sessions

### History row overhaul ([#549](https://github.com/weaties/helmlog/pull/549))
- **Race-first filter** — default history filter is Race; debrief filter
  removed (debriefs now live attached to their race)
- **Per-row race summary** — each row shows thumbnail, wind, and top-3 fleet
  result (preferring imported `race_results`, always surfacing the own boat)
- **Simpler rows** — inline actions, exports, audio player, and pills
  stripped from list rows; initial load triggers on page render

### Session page: wind overlay, resizable map, collapsible gauges
- **Wind card with TWD** ([#553](https://github.com/weaties/helmlog/pull/553))
  — TWD added to wind card with true/apparent stacked by column
- **Wind overlay on track** ([#555](https://github.com/weaties/helmlog/pull/555))
  — TWD/TWS met wind barbs drawn along the track with zoom-adaptive cadence
  and hover tooltips
- **Current overlay polish** ([#557](https://github.com/weaties/helmlog/pull/557))
  — zoom-adaptive cadence and lighter strokes for the derived current overlay
- **Resizable track map** ([#559](https://github.com/weaties/helmlog/pull/559))
  — S/M/L/XL size presets for the session track map
- **Collapsible layer/gauge rows** ([#560](https://github.com/weaties/helmlog/pull/560))
  — layer and gauge rows collapse, a current gauge was added, and gauges
  moved above the map

### Discussion threads
- **Shareable deep links** ([#561](https://github.com/weaties/helmlog/issues/561))
  — query-param anchors (`?thread=<id>&comment=<id>`) that survive Slack and
  other unfurls, copy-link buttons on threads and comments, flash-highlight on
  arrival, and auth redirects that preserve the query string so a signed-out
  recipient lands on the login wall and returns to the exact thread after
  signing in; scroll pins the thread header to the top unless a linked reply
  would fall below the fold, in which case the reply is centered

### Dependencies
- **ruff** → `>=0.15.10`, **python-dotenv** → `>=1.2.2`,
  **pytest-cov** → `>=7.1.0`, **influxdb-client** → `>=1.50.0`,
  **pyannote-audio** → `>=4.0.4`, **actions/github-script** → v9

## Sprint 7 — Vakaros Ingest, Maneuver Analysis & Diarized Playback (2026-04-11)

Vakaros VKX watched-folder ingest with rich start-line overlays, a new
maneuver-analysis package, diarized transcript playback with crew identification,
multi-camera Insta360 pipeline, and a host of session-management fixes.

### Vakaros VKX ingest ([#458](https://github.com/weaties/helmlog/issues/458), [#482](https://github.com/weaties/helmlog/pull/482))
- **VKX parser** — pure-Python decoder for the public Vakaros VKX binary format
  (positions, race-timer events, line pings, wind rows)
- **Storage schema v59** — five new tables, content-hash dedupe, FK cascade to
  matched races, and a `vakaros-ingest` CLI subcommand
- **Watched inbox + admin page** — `/admin/vakaros` lists ingested sessions,
  matches them to overlapping races (≥50% time overlap), and supports re-match
- **Session-page overlays** — track selector, start-line geometry with hover
  reveal, flag/boat icons for line pings with relative-time tooltips, wind-tick
  visualization, polar % and wind-relative line bias at race start, boat speed
  and distance-to-line readouts, and Vakaros race start surfaced in the
  maneuvers panel
- **Cross-surface playback fixes** — pause media on seek, transcript follow
  toggle, true-wind-only nearest-lookup, and per-race line-ping trimming

### Maneuver analysis ([#457](https://github.com/weaties/helmlog/pull/457))
- **Maneuver analysis package** — entry/exit metrics, distance loss, ranking,
  and YouTube deep-links for tacks and gybes
- **Track diagrams** — per-maneuver charts with TWS overlay, ghost upwind/
  perpendicular projections, actual boat position at the ghost timestamp, and
  explicit Ladder ideal/Δ labels
- **Overlay polish** — sticky tooltips, wind-up overlay, overlay checkbox
  selection, elapsed-time column, and `TIMEZONE` resolved from DB settings

### Diarized playback & session sync ([#443](https://github.com/weaties/helmlog/issues/443), [#446](https://github.com/weaties/helmlog/issues/446))
- **Diarized transcript playback** — speaker-attributed transcript with crew
  identification and per-voice learning, retranscribe button for undiarized
  segments, and a new retranscribe endpoint
- **Unified playback clock** — map, video, audio, and transcript share one
  clock; transcript highlight is driven directly from `audio.timeupdate`
- **360 video panning** — YouTube embed configured to allow 360° panning, with
  a Watch-on-YouTube fallback link
- **Remote transcribe robustness** — extended timeout, MPS for pyannote, always
  request diarization when offloading, speaker picker reads from response
  envelope
- **Session page reorder** — video under track, audio above transcript

### Multi-camera Insta360 pipeline ([#445](https://github.com/weaties/helmlog/issues/445), [#452](https://github.com/weaties/helmlog/pull/452), [#455](https://github.com/weaties/helmlog/pull/455))
- **Multi-camera pipeline** — import/upload/backup lifecycle, dual-fisheye
  `.mp4` → `.insv` rename during import-matched, 360° detection fixes, and
  local-timezone titles
- **SD-mount auto-import** — launchd agent runs `import-matched` on Insta360
  SD card mount; `fswatch -E` fix so regex quantifiers actually match
- **Watch-on-YouTube link** follows the current video and playhead; static
  assets cache-busted by git SHA

### Session rename + URL slugs ([#449](https://github.com/weaties/helmlog/issues/449))
- **Human-readable URLs** — session rename with stable session id alongside
  the slug, debrief and missing-slug URL resolution, and a v58 backfill

### Backup & restore
- **Backup improvements** ([#461](https://github.com/weaties/helmlog/pull/461))
  — first-run handling, `.env` capture, and broader Signal K coverage
- **Restore script** ([#463](https://github.com/weaties/helmlog/pull/463)) —
  `restore.sh` pulls a snapshot back onto a target Pi; backup now captures
  Grafana provisioning and Influx token with rotation handling

### Bug fixes
- **Session deletion FKs** ([#434](https://github.com/weaties/helmlog/pull/434),
  [#436](https://github.com/weaties/helmlog/pull/436)) — delete `camera_sessions`,
  `sensor_readings`, and `extraction_runs` before parent rows to avoid FK errors
- **Session start timestamp** ([#437](https://github.com/weaties/helmlog/pull/437))
  — captured just before DB insert
- **SK auth password file** ([#438](https://github.com/weaties/helmlog/pull/438))
  — works when the helmlog service runs with a different `HOME`
- **Grafana AWA units** ([#439](https://github.com/weaties/helmlog/pull/439)) —
  Apparent Wind split into separate AWS and AWA panels; AWA shows degrees
- **Photo upload limit** ([#441](https://github.com/weaties/helmlog/pull/441))
  — raise nginx body-size limit above the 1 MB default
- **Crew selector** ([#435](https://github.com/weaties/helmlog/pull/435)) —
  show invited-but-not-accepted users
- **mypy clean** ([#483](https://github.com/weaties/helmlog/pull/483)) — clear
  remaining mypy errors across `src/`

## Sprint 6 — Rudder Angle & Signal K Security (2026-04-05)

Rudder angle instrument support and Signal K authentication hardening.

### Rudder angle ([#420](https://github.com/weaties/helmlog/pull/420))
- **Signal K ingest** — subscribe to `steering.rudderAngle`, decode radians to
  degrees via new `RudderRecord` dataclass (PGN 127245)
- **Storage** — new `rudder_angles` table (migration v52) with configurable
  storage rate (`RUDDER_STORAGE_HZ`) exposed on admin settings page
- **Instrument panel** — live RDR tile showing rudder angle in degrees

### Signal K auth & security
- **Auth token support** ([#418](https://github.com/weaties/helmlog/pull/418)) —
  three-tier waterfall: `SK_TOKEN` env var → `SK_USERNAME`/`SK_PASSWORD` login →
  `~/.signalk-admin-pass.txt` fallback. Token applied to WebSocket URI and HTTP
  headers. Backward compatible when no auth is configured
- **secretKey generation** ([#417](https://github.com/weaties/helmlog/pull/417)) —
  `setup.sh` now generates a 32-byte random `secretKey` in Signal K's
  `security.json`, fixing device token issuance and SensESP device approval

## Sprint 5 — Web Architecture, Networking & Bandwidth, Calibration (2026-03-24)

Major web architecture refactor, metered bandwidth management, admin networking
tools, and continued analysis and visualization work.

### Web architecture overhaul
- **Route module split** ([#405](https://github.com/weaties/helmlog/pull/405)) — decomposed monolithic `web.py` into 24
  domain-specific route modules for maintainability
- **WebSocket live push** ([#405](https://github.com/weaties/helmlog/pull/405)) — real-time instrument data streaming to the
  browser via WebSocket
- **WAL mode + connection split** ([#405](https://github.com/weaties/helmlog/pull/405)) — SQLite write-ahead logging with
  separate read/write connections for improved concurrency

### Metered bandwidth management ([#403](https://github.com/weaties/helmlog/issues/403))
- **Metered-connection mode** — detects cellular/marina networks and enforces
  bandwidth budgets with configurable daily limits
- **InfluxDB bandwidth attribution** — per-path network tagging so dashboard
  panels show exactly where bandwidth is spent
- **Grafana dashboard** — pre-built panels for bandwidth monitoring and alerting

### Admin networking & remote access
- **Network management page** ([#256](https://github.com/weaties/helmlog/issues/256)) — WLAN profile switching, interface status,
  and bandwidth monitoring from the admin UI
- **Tailscale, Cloudflare & DNS** ([#256](https://github.com/weaties/helmlog/issues/256)) — Tailscale status, Cloudflare routing,
  and DNS configuration on the network page
- **Cloudflare Tunnel wizard** ([#378](https://github.com/weaties/helmlog/pull/378)) — interactive setup for remote access
  without port forwarding
- **Config persistence** — InfluxDB and camera config saved to
  `~/.helmlog/config.env` to survive deploys
- **User deletion** ([#381](https://github.com/weaties/helmlog/pull/381)) — Delete button on the admin users page

### Calibration & boat settings
- **Instrument calibration category** ([#337](https://github.com/weaties/helmlog/issues/337)) — new `instrument_calibration`
  settings category for compass offset, speed factor, etc.
- **Negative value input** ([#407](https://github.com/weaties/helmlog/pull/407)) — ± toggle button for mobile numeric inputs
  (e.g., compass deviation)
- **Session settings fix** ([#385](https://github.com/weaties/helmlog/issues/385)) — boat setup values now correctly reflected
  during and after active sessions

### Analysis & visualization
- **Analysis framework Phase 2** ([#285](https://github.com/weaties/helmlog/issues/285)) — co-op plugin promotion, A/B
  comparison workflow, and version staleness tracking
- **Analysis catalog UI** ([#412](https://github.com/weaties/helmlog/issues/412)) — admin page for plugin management, co-op
  catalog with state badges and moderator actions, session-page staleness
  indicator with re-run button, and A/B comparison side-by-side view
- **Visualization framework Phase 1** ([#286](https://github.com/weaties/helmlog/issues/286)) — pluggable chart rendering
  for session data with extensible plugin API
- **Session deletion** ([#409](https://github.com/weaties/helmlog/issues/409)) — delete sessions from the UI with an
  active-session safety guard

### Auth & profile
- **Change password** ([#340](https://github.com/weaties/helmlog/issues/340)) — `PATCH /api/me/password` endpoint with full
  validation chain (credential check, current password verification, length,
  confirmation match) and audit logging. Profile page conditionally renders
  the form only for password-credential users

### Signal K fixes
- **Self-vessel UUID resolution** ([#388](https://github.com/weaties/helmlog/pull/388)) — Signal K deltas no longer rejected
  due to mismatched vessel identity
- **Admin API access** ([#384](https://github.com/weaties/helmlog/issues/384)) — use `type=admin` in Signal K security.json for
  proper admin-level access

### Theming & UX
- **CSS variable migration** — remaining hardcoded hex colors replaced with CSS
  custom properties across all templates, JS, and Python
- **In-app issue reporting** ([#369](https://github.com/weaties/helmlog/issues/369)) — bug report and feature request links
  directly from the app

### Documentation & developer experience
- **Crew race-day guide** — quick reference for crew operating HelmLog during
  races (start/stop sessions, mark events, troubleshooting)
- **AI contributor guardrails** ([#392](https://github.com/weaties/helmlog/pull/392)) — improved onboarding docs and CI checks
  for AI-assisted contributions
- **Semantic layer** ([#390](https://github.com/weaties/helmlog/pull/390)) — machine-readable domain knowledge module with
  decision tables, state diagrams, and EARS specs
- **Documentation refresh** ([#172](https://github.com/weaties/helmlog/issues/172), [#236](https://github.com/weaties/helmlog/pull/236)) — roadmap, README, CONTRIBUTING.md,
  and guides updated to match current codebase

### Infrastructure
- **CI dependency bumps** — actions/checkout v4→v6, actions/github-script v7→v8
- **WAL gitignore** — SQLite WAL files excluded to prevent dirty-version false
  positives on Pi deploys
- **Idempotent migrations** — `ALTER TABLE ADD COLUMN` made safe to re-run

## Sprint 4 — Analysis, Session Matching, Tuning Extraction & Skill Tooling (2026-03-18)

Sprint 4 adds a pluggable analysis framework, co-op session matching,
transcript-based tuning extraction, customizable color schemes, and
expanded developer skill tooling.

### Pluggable analysis framework ([#283](https://github.com/weaties/helmlog/issues/283), [#309](https://github.com/weaties/helmlog/issues/309), [#321](https://github.com/weaties/helmlog/issues/321))

Extensible plugin system for post-session analysis:

- **Plugin protocol** — ABC-based plugin API with importlib discovery, SQLite
  cache with data-hash invalidation (schema v42), and preference inheritance
  (platform → co-op → boat → user)
- **Polar baseline plugin** — wraps existing polar analysis as the first
  built-in plugin
- **Sail VMG comparison** ([#309](https://github.com/weaties/helmlog/issues/309)) — pure upwind/downwind VMG functions across 5
  wind bands, cross-session `/api/sails/performance` endpoint, and performance
  table on the sails page

### Co-op session matching ([#281](https://github.com/weaties/helmlog/issues/281), [#324](https://github.com/weaties/helmlog/issues/324))

Proximity-based pairing of co-op sessions across boats:

- **Automatic scan** — configurable time window (15 min) and geographic radius
  (2 NM) to detect overlapping sessions from co-op peers
- **Match lifecycle** — Unmatched → Candidate → Matched → Named, with quorum
  confirmation and shared name synthesis
- **Federation support** — 5 new peer API endpoints for scan, proposal, confirm,
  reject, and name push — all Ed25519-signed
- **Scalability** — parallel fan-out for 20-boat co-ops, proposal dedup,
  centroid caching (schema v45)

### Boat tuning extraction from transcripts ([#276](https://github.com/weaties/helmlog/issues/276), [#325](https://github.com/weaties/helmlog/issues/325))

Regex-based extraction of tuning parameters from audio transcripts:

- **RegexExtractor** — parses natural language ("backstay 12", "vang 8.5") from
  transcript segments into structured tuning values
- **Extraction run lifecycle** — Created → Running → ReviewPending/Empty →
  FullyReviewed, with accept/dismiss review workflow
- **Auto-create settings** — accepted items automatically create `boat_settings`
  timeline entries linked to the extraction run
- **Review UI** — collapsible settings history with transcript play buttons,
  newest-first timeline, and superseded-default indicators
- **Privacy** — all extraction data is boat-private, never shared with co-op
- Schema v44 adds `extraction_runs` and `extraction_items` tables; 7 API endpoints

### Customizable color schemes ([#347](https://github.com/weaties/helmlog/issues/347), [#358](https://github.com/weaties/helmlog/issues/358))

Sunlight-optimized theming system:

- **6 presets** with WCAG contrast validation
- **Admin default + user override** — boat-wide default on admin settings,
  personal override on profile page
- **Full compliance** — ~150+ hardcoded hex colors replaced with CSS custom
  properties across all templates, JS, CSS, and Python

### Threaded comments Phase 2 — notifications ([#284](https://github.com/weaties/helmlog/issues/284), [#321](https://github.com/weaties/helmlog/issues/321))

- **@mention autocomplete** — dropdown in comment textareas with arrow key
  navigation, multi-word name support
- **4 notification types** — mention, new thread, reply, resolved — with
  pluggable channels (platform + email)
- **Attention dashboard** — `/attention` page with nav badge for unread
  notifications
- Schema v43 for notification storage

### Sail management overhaul ([#306](https://github.com/weaties/helmlog/issues/306), [#307](https://github.com/weaties/helmlog/issues/307), [#308](https://github.com/weaties/helmlog/issues/308), [#318](https://github.com/weaties/helmlog/issues/318))

- **Point-of-sail field** — upwind/downwind/both classification per sail
  (schema v39)
- **Default sail selection** — `sail_defaults` table (schema v40) with
  pre-selection on session pages
- **Sail management page** — `/sails` with inventory, accumulated tack/gybe
  counts, per-session history with wind summaries

### Auto-start recording ([#345](https://github.com/weaties/helmlog/issues/345), [#346](https://github.com/weaties/helmlog/issues/346))

- Schedule a future race start time via the home page UI
- Background task fires `start_race()` at the scheduled time with 1 s polling
- Manual start atomically cancels any pending schedule; missed starts are
  detected and cleared

### Developer experience & infrastructure

- **Risk tiers** ([#320](https://github.com/weaties/helmlog/issues/320)) — 4-tier classification (Critical/High/Standard/Low)
  with tier-aware `/pr-checklist` and `/spec` skill for structured specs
- **Skill evaluation framework** ([#349](https://github.com/weaties/helmlog/issues/349), [#357](https://github.com/weaties/helmlog/issues/357)) — test cases for measuring skill
  quality and detecting regressions
- **/skill-compare** ([#354](https://github.com/weaties/helmlog/issues/354), [#367](https://github.com/weaties/helmlog/issues/367)) — blind A/B comparison of two skill versions
  with correctness, completeness, conciseness, and actionability scoring
- **Skill trigger optimization** ([#353](https://github.com/weaties/helmlog/issues/353), [#364](https://github.com/weaties/helmlog/issues/364)) — audited descriptions for all 13
  skills with explicit trigger/anti-trigger guidance and 33-case test suite
- **/architecture skill** ([#352](https://github.com/weaties/helmlog/issues/352), [#363](https://github.com/weaties/helmlog/issues/363)) — codebase comprehension with module map,
  data flow, and complexity hotspots
- **/diagnose skill** ([#351](https://github.com/weaties/helmlog/issues/351), [#360](https://github.com/weaties/helmlog/issues/360)) — systematic Pi troubleshooting runbook
- **/domain skill** ([#350](https://github.com/weaties/helmlog/issues/350), [#359](https://github.com/weaties/helmlog/issues/359)) — Signal K paths, NMEA 2000 PGNs, and sailing
  instrument reference
- **Pi test harness** ([#334](https://github.com/weaties/helmlog/issues/334), [#335](https://github.com/weaties/helmlog/issues/335), [#336](https://github.com/weaties/helmlog/issues/336)) — Mac-orchestrated cross-Pi federation
  testing over Tailscale with UI smoke tests
- **Claude Code Review** ([#330](https://github.com/weaties/helmlog/issues/330)) — GitHub Actions workflow for automated PR review
- **CI updates** — actions/checkout v6, astral-sh/setup-uv v7
- **Pi fixes** — sudoers exact-match fix ([#361](https://github.com/weaties/helmlog/issues/361)), data dir ownership ([#362](https://github.com/weaties/helmlog/issues/362)),
  Grafana provisioning permissions ([#361](https://github.com/weaties/helmlog/issues/361))

---

## Sprint 3 — Auth, Boat Settings, Comments & Developer Tooling (2026-03-14)

Sprint 3 adds multi-method authentication, boat tuning capture, and
collaborative race discussion.

### Authentication overhaul — invitation + password + OAuth ([#268](https://github.com/weaties/helmlog/issues/268), [#272](https://github.com/weaties/helmlog/issues/272), [#279](https://github.com/weaties/helmlog/issues/279), [#280](https://github.com/weaties/helmlog/issues/280))

Replace the single-use magic-link flow with a proper auth system:

- **Invitation workflow** — admins send email invitations; users register with
  a password on first visit
- **Password authentication** — bcrypt-hashed credentials with forgot/reset flow
- **OAuth login** — Google, Apple, and GitHub identity providers via Authlib
- **Session middleware** — secure cookie-based sessions replace per-request
  token lookup
- **Developer role** ([#271](https://github.com/weaties/helmlog/issues/271)) — orthogonal `is_developer` flag gates access to
  the synthesizer and non-standard branch selection in the deploy UI

### Boat tuning settings ([#274](https://github.com/weaties/helmlog/issues/274), [#275](https://github.com/weaties/helmlog/issues/275), [#297](https://github.com/weaties/helmlog/issues/297), [#298](https://github.com/weaties/helmlog/issues/298))

Structured capture and playback of boat tuning parameters:

- **Time-series data model** — `boat_settings` table (schema v35) stores
  parameter values with timestamps, supporting both boat-level defaults and
  race-specific overrides
- **Manual input UI** — phone-friendly accordion card on the home page with
  category groups ordered by change frequency, auto-save on field change
- **Session playback panel** — read-only settings panel on the session detail
  page resolves values by timestamp; overridden defaults shown with
  strikethrough annotation; updates when clicking the track or scrubbing video
- **Synthesizer integration** — synthesized sessions now generate realistic
  J/105 tuning data (rig tensions, sail controls, wind-responsive adjustments)

### Threaded comments Phase 1 ([#304](https://github.com/weaties/helmlog/issues/304))

Collaborative race discussion anchored to sessions:

- **Comment threads** — create threads on a session with optional timestamp or
  mark reference (weather_mark_1, leeward_mark_2, etc.)
- **Threaded replies** — nested comments with per-user read/unread tracking and
  unread badges
- **Resolve/unresolve** — mark threads as resolved with a summary; resolved
  threads show as hollow green rings on the track map
- **Track map integration** — right-click the track to start a discussion at
  that timestamp; colored dots show open (purple), unread (blue), and resolved
  (green) threads with tooltip previews
- **Crew redaction** — comment content is redacted in co-op API responses per
  data licensing policy

### Developer experience & infrastructure

- **Promote gate** ([#302](https://github.com/weaties/helmlog/issues/302), [#303](https://github.com/weaties/helmlog/issues/303)) — `promote.yml` GitHub Actions workflow gates
  `main → stage` promotion on a new RELEASES.md heading; ideation-only commits
  are exempt
- **/release-notes skill** — drafts a RELEASES.md entry from commits since
  the last stage promotion
- **/ideate skill** ([#289](https://github.com/weaties/helmlog/issues/289)) — capture half-baked ideas into `docs/ideation-log.md`
  with structured metadata
- **Bootstrap fixes** ([#291](https://github.com/weaties/helmlog/issues/291), [#292](https://github.com/weaties/helmlog/issues/292), [#294](https://github.com/weaties/helmlog/issues/294)) — admin login URL printed correctly,
  `sudo` added to data dir removal in `reset-pi.sh`

---

## Sprint 2 complete — Performance Analysis & Synthesizer (2026-03-12)

Sprint 2 (March 10–24) is feature-complete. 

### Performance analysis

- **Maneuver detection** ([#232](https://github.com/weaties/helmlog/issues/232)) — automatic tack and gybe detection from 1 Hz
  heading data, surfaced on the session detail page
- **Polar performance visualization** ([#233](https://github.com/weaties/helmlog/issues/233)) — polar diagram overlay on the
  session detail page showing boat performance against the J/105 target polar

### Synthesizer improvements

- **Spatially varying wind model** ([#248](https://github.com/weaties/helmlog/issues/248)) — wind direction and pressure
  gradients across the course area instead of a single uniform wind field
- **Tack on headers with realistic randomization** ([#247](https://github.com/weaties/helmlog/issues/247)) — synthesized boats
  now tack when lifted, with heading noise and timing jitter for realistic
  tracks
- **Wind model sharing between co-op members** ([#246](https://github.com/weaties/helmlog/issues/246)) — co-op boats in a
  synthesized session share the same wind field and start time so their tracks
  are physically consistent
- **Fix: leg-derived marks for wind field visualization** ([#264](https://github.com/weaties/helmlog/issues/264)) — wind field
  overlay now uses the actual mark positions from leg geometry instead of the
  original placed marks

### Deploy & infrastructure

- **Promotion history and branch comparison** ([#258](https://github.com/weaties/helmlog/issues/258)) — deploy admin page shows
  a commit-level diff between the current branch and the promotion target
- **Fix: create loki/promtail groups before chown** ([#261](https://github.com/weaties/helmlog/issues/261)) — setup.sh no longer
  fails on a fresh Pi when the loki/promtail system groups don't exist yet

---

## Promoted to live and stage from main, 2026-03-10

### Synthesize race sessions with interactive Leaflet map ([#245](https://github.com/weaties/helmlog/issues/245), [#252](https://github.com/weaties/helmlog/issues/252))

Generate synthetic J/105 sailing sessions for testing and demo purposes:

- **Interactive course builder** — Leaflet map with CYC mark pins; click to
  place the race committee boat, auto-compute windward/leeward marks
- **Simulation engine** — J/105 polar interpolation, wind shifts, VMG-aware
  tack selection, and 1 Hz instrument data generation
- **Land avoidance** — real OSM coastline data for Puget Sound; computed marks
  that fall on land are automatically pulled to navigable water
- **Segment-based land crossing detection** — intermediate point sampling
  catches peninsula/island clips even when both endpoints are in water
- Synthesized session type with amber badge in UI, filterable on history page

### Federation phase 1 — identity, co-op data model, session sharing ([#224](https://github.com/weaties/helmlog/issues/224))

Boat-to-boat federation with cryptographic identity:

- **Ed25519 keypair generation** — `helmlog identity init` creates a boat card
  with public key and fingerprint
- **Co-op data model** — create, join, and manage cooperative groups of boats
- **Session sharing** — share/unshare sessions with co-op members, with embargo
  enforcement and audit logging
- **Peer API** — inter-boat HTTP endpoints with request signing and verification
- 32 integration tests covering co-op lifecycle, auth, embargo, and data licensing

### Deployment management and promotion workflow ([#222](https://github.com/weaties/helmlog/issues/222), [#223](https://github.com/weaties/helmlog/issues/223))

Structured release process with stage/live branches:

- **Admin deploy page** — view current version, trigger updates, and monitor
  deploy status from the web UI
- **Evergreen mode** — opt-in automatic deployment on push to the tracked branch
- **Promotion workflow** — `main` → `stage` → `live` branch promotion with
  safety checks

### Data policy compliance — privacy, auth, and deletion controls ([#194](https://github.com/weaties/helmlog/issues/194)–[#211](https://github.com/weaties/helmlog/issues/211), [#215](https://github.com/weaties/helmlog/issues/215))

Comprehensive data licensing policy enforcement:

- **PII deletion** — audio, photos, transcripts, and diarized content can be
  deleted per data policy requirements
- **Field allowlists** — co-op API endpoints enforce strict field filtering
- **Audit logging** — all data access and sharing events are logged
- **Private session isolation** — unshared sessions are invisible to co-op peers

### Configurable Pi health monitor interval ([#249](https://github.com/weaties/helmlog/issues/249), [#250](https://github.com/weaties/helmlog/issues/250))

- Default collection interval changed from 60 s to 2 s for smoother dashboards
- Non-blocking CPU measurement via `psutil.cpu_percent(interval=None)`
- `MONITOR_INTERVAL_S` env var and admin settings page for runtime tuning

### Mobile navigation overhaul ([#230](https://github.com/weaties/helmlog/issues/230), [#231](https://github.com/weaties/helmlog/issues/231))

- **Hamburger menu** replaces tab navigation for better mobile usability

### Infrastructure and developer experience

- **GitHub Actions CI** — tests, lint, and type checking on every PR ([#219](https://github.com/weaties/helmlog/issues/219))
- **Docker dev container** — Claude Code development environment ([#229](https://github.com/weaties/helmlog/issues/229))
- **Community contribution infrastructure** — CONTRIBUTING.md, issue templates,
  PR template ([#174](https://github.com/weaties/helmlog/issues/174), [#218](https://github.com/weaties/helmlog/issues/218))
- **Fan speed** added to Pi Health Grafana dashboard with shared crosshairs ([#225](https://github.com/weaties/helmlog/issues/225))
- **Hostname in footer** — version info now includes the Pi hostname ([#175](https://github.com/weaties/helmlog/issues/175), [#221](https://github.com/weaties/helmlog/issues/221))
- **reset-pi.sh** — restore a Pi to pre-setup state for reimaging ([#242](https://github.com/weaties/helmlog/issues/242))
- **Git ownership fix** — prevent `.git/` conflicts between helmlog service and
  deploy user ([#239](https://github.com/weaties/helmlog/issues/239), [#240](https://github.com/weaties/helmlog/issues/240))
- **Bootstrap add-user fix** — run as correct user to avoid read-only DB ([#244](https://github.com/weaties/helmlog/issues/244))
- **Documentation** — approachability guides, fleet quickstart, operator updates,
  federation protocol gaps ([#216](https://github.com/weaties/helmlog/issues/216), [#217](https://github.com/weaties/helmlog/issues/217), [#220](https://github.com/weaties/helmlog/issues/220))
- **CLAUDE.md** — added `uv sync` dependency guidance ([#251](https://github.com/weaties/helmlog/issues/251))

---

## 2026-03-08

### Embedded YouTube player with track sync ([#183](https://github.com/weaties/helmlog/issues/183), [#185](https://github.com/weaties/helmlog/issues/185))

Session replay now includes synchronized video:

- **Embedded YouTube player** on session detail page and history page cards
- **Bidirectional track sync** — click a point on the track map and the video
  jumps to that moment; video playback updates the track marker position
- Deep-link support via `?t=<seconds>` for sharing specific race moments

### Session detail page with track map ([#178](https://github.com/weaties/helmlog/issues/178), [#180](https://github.com/weaties/helmlog/issues/180))

Dedicated session view at `/session/{id}`:

- **Interactive track map** — GPS track rendered with speed-based color coding
- **Video deep-links** — clickable timestamps jump to the corresponding video
- All session metadata (crew, results, notes, transcripts, sails, exports)
  consolidated into a single page replacing the history accordion cards

### Simplified home page ([#170](https://github.com/weaties/helmlog/issues/170), [#171](https://github.com/weaties/helmlog/issues/171))

Home page redesigned for race-day focus:

- **Idle state** — start buttons only (race, practice, debrief)
- **Active state** — current race card with live instruments and controls
- Red stop button with two-tap safety guard and countdown timer
- Camera start/stop runs fire-and-forget so race API responds instantly
- Extracted inline HTML/CSS/JS from `web.py` into Jinja2 templates and
  static files (`base.html`, `home.html`, `history.html`, `base.css`,
  `shared.js`, `home.js`, `history.js`)

### Gaia GPS backfill ([#101](https://github.com/weaties/helmlog/issues/101), [#176](https://github.com/weaties/helmlog/issues/176))

Import historical race data from Gaia GPS exports:

- **Track download** — fetch GPX tracks from Gaia GPS API
- **Race classification** — auto-detect race sessions from track patterns
- **SQLite import** — backfill position, heading, and speed data
- **InfluxDB migration** — push historical data to Grafana dashboards

### Insta360 X4 video pipeline ([#155](https://github.com/weaties/helmlog/issues/155), [#161](https://github.com/weaties/helmlog/issues/161))

End-to-end automated video workflow:

- **Camera control** — start/stop recording via OSC HTTP API, tied to race
  start/stop events ([#98](https://github.com/weaties/helmlog/issues/98), [#147](https://github.com/weaties/helmlog/issues/147))
- **Camera admin UI** — add/configure cameras, WiFi credentials, status
  monitoring from the web interface ([#147](https://github.com/weaties/helmlog/issues/147))
- **Stitch & upload** — Docker-based stitcher with ffmpeg fallback; discovers
  both `.insv` (360°) and `.mp4` (single-lens) recordings
- **Auto-link** — uploaded videos automatically associated with race sessions
  by time overlap
- WiFi SSID/password fields on camera config for direct camera network access

### Remote transcription offload ([#121](https://github.com/weaties/helmlog/issues/121), [#146](https://github.com/weaties/helmlog/issues/146))

Offload Whisper transcription from the Pi to a faster machine:

- **HTTP worker** (`scripts/transcribe_worker.py`) runs on a Mac or other
  machine with more CPU
- **Admin settings page** — SQLite-backed settings overrides configurable
  from `/admin/settings` in the web UI
- Transparent fallback to local Pi transcription if remote worker is
  unreachable

### Nginx reverse proxy ([#137](https://github.com/weaties/helmlog/issues/137), [#167](https://github.com/weaties/helmlog/issues/167))

Single-port access to all services on the Pi:

- **Path-based routing** — `/` (Helm Log), `/grafana/` (Grafana), `/sk/`
  (Signal K admin), `/signalk/` (Signal K API)
- Eliminates the need to remember multiple port numbers
- Signal K admin UI moved to `/sk/` to avoid conflict with `/admin/`

### Configurable event naming rules ([#154](https://github.com/weaties/helmlog/issues/154), [#159](https://github.com/weaties/helmlog/issues/159))

Day-of-week event names are now admin-configurable:

- Admin UI at `/admin/events` for creating and managing event rules
- Custom event names (set via web UI) take precedence over weekday defaults
- Error message shown when starting a race without a daily event set ([#153](https://github.com/weaties/helmlog/issues/153))

### Self-service login ([#148](https://github.com/weaties/helmlog/issues/148), [#152](https://github.com/weaties/helmlog/issues/152))

Existing users can request a new magic link from the login page without
needing an admin to generate an invite token.

### Configurable Pi host references ([#151](https://github.com/weaties/helmlog/issues/151))

Removed hardcoded `corvopi` / `weaties` references from scripts and docs.
All Pi-specific values now come from environment variables or are
auto-detected.

### Infrastructure fixes

- **Loki + Promtail** for centralized log management ([#139](https://github.com/weaties/helmlog/issues/139), [#142](https://github.com/weaties/helmlog/issues/142)), with
  loopback-only config fix ([#162](https://github.com/weaties/helmlog/issues/162))
- **Setup.sh fixes** — bcrypt module, data/ permissions, uv Python
  traversal, tmux, locale ([#140](https://github.com/weaties/helmlog/issues/140), [#141](https://github.com/weaties/helmlog/issues/141), [#144](https://github.com/weaties/helmlog/issues/144))
- **Signal K** — fix CORS origins crash on fresh install ([#143](https://github.com/weaties/helmlog/issues/143))
- **Grafana** — bind to 0.0.0.0 for Tailscale access, auto-populate
  InfluxDB vars in `.env`, anonymous access with None role for login page
- **Race Track query optimization** — downsample before union, 65x faster
- **Database schema documentation** at `docs/database-schema.md` ([#156](https://github.com/weaties/helmlog/issues/156), [#157](https://github.com/weaties/helmlog/issues/157))

### Shared navigation, version footer, timezone support ([#129](https://github.com/weaties/helmlog/issues/129), [#130](https://github.com/weaties/helmlog/issues/130))

Consistent navigation and timezone-aware timestamps across all pages:

- **Shared nav bar** — Home, History, Boats, Users (admin), Audit (admin), and
  Profile links appear on every page; admin-only links auto-hide for non-admins
- **Version footer** — every page shows the git branch, short SHA, and
  dirty/clean status (includes uncommitted changes and unpushed commits)
- **Configurable timezone** — set `TIMEZONE=America/Los_Angeles` (or any IANA
  name) in `.env` to display all timestamps in local time instead of UTC; affects
  race date grouping, weekday event naming, and all displayed timestamps on home,
  history, audit, and admin pages
- 12 new tests (425 total passing)

### Audit log, tags, triggers, headshots, admin nav ([#93](https://github.com/weaties/helmlog/issues/93), [#94](https://github.com/weaties/helmlog/issues/94), [#99](https://github.com/weaties/helmlog/issues/99), [#100](https://github.com/weaties/helmlog/issues/100), [#123](https://github.com/weaties/helmlog/issues/123))

Major feature batch adding traceability, organization, and user personalization:

- **Audit logging** — all state-changing web routes log user, IP, and action to
  an `audit_log` table; admin audit page at `/admin/audit` + JSON API at `/api/audit`
- **Tags** — full CRUD API for tagging sessions and notes; `tags`, `session_tags`,
  `note_tags` tables (schema v19)
- **Keyword triggers** — auto-create notes from transcript keywords; new
  `triggers.py` module + `scan-transcript` CLI subcommand for retroactive scanning
- **Profile headshots** — self-service avatar upload on profile page (Pillow
  resize to 256×256 JPEG); SVG initials fallback for users without avatars
- **Admin Users nav link** — `/admin/users` is now discoverable from the main
  nav bar (visible to admins only via `/api/me` role check)
- 25 new tests (413 total passing)

### Speaker diarisation on Pi 5 ([#120](https://github.com/weaties/helmlog/issues/120))

Pyannote speaker diarisation now works on the Pi 5 (aarch64):

- Enabled `pyannote-audio` pipeline for speaker labeling (`SPEAKER_00`,
  `SPEAKER_01`, …) on transcriptions when `HF_TOKEN` is configured
- Bypassed `torchcodec` `AudioDecoder` which fails on aarch64 — uses
  `soundfile` fallback instead
- Set `HOME` env var in systemd unit so `faster-whisper` can cache models
  under the `helmlog` service account

### Security hardening ([#117](https://github.com/weaties/helmlog/issues/117), [#118](https://github.com/weaties/helmlog/issues/118))

Comprehensive security hardening of the Pi deployment, baked into `setup.sh`
so all future SD card builds inherit the same posture:

- Dedicated `helmlog` service account (nologin, UID ≈ 997)
- Scoped `NOPASSWD` sudo replacing the blanket Pi OS default
- SSH hardening (X11Forwarding disabled, permissions tightened)
- InfluxDB bound to loopback only; Grafana loopback + login required
- Automatic security updates via `unattended-upgrades`
- Unused services masked (cups, avahi-daemon, bluetooth)
- Signal K bcrypt admin password auto-generated at setup
- SSH safety guard added after lockout incident — `setup.sh` now validates
  that at least one SSH access method remains before tightening config

### Race event naming fix ([#122](https://github.com/weaties/helmlog/issues/122))

Custom event names (saved via the web UI) now take precedence over the
weekday defaults (BallardCup on Monday, CYC on Wednesday). Previously,
a saved custom event was ignored on Monday/Wednesday.

### Security audit documentation

Added security audit report and penetration test statement of work
(`docs/`) documenting the 2026-03-01 security review.

---

## 2026-03-01

### Audio transcription ([#42](https://github.com/weaties/helmlog/issues/42), PR [#63](https://github.com/weaties/helmlog/issues/63))

Completed recordings can now be transcribed to text directly from the History
page. Transcription runs on the Pi via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no cloud service
required.

- **📝 Transcript** button on every audio-enabled race card in History
- Jobs run in the background; status polling shows a spinner until complete
- Transcripts are stored in SQLite (`transcripts` table, schema v15)
- Model is configurable via `WHISPER_MODEL` env var (default: `base`)
- Speaker diarisation deferred — pyannote.audio is too heavy for Pi CPU

### System health monitoring ([#39](https://github.com/weaties/helmlog/issues/39), PR [#62](https://github.com/weaties/helmlog/issues/62))

The logger now watches the Pi's vitals automatically.

- `monitor_loop` background task collects CPU/memory/disk/temperature via
  `psutil` every 60 s and writes a `system_health` measurement to InfluxDB
- Home page polls `/api/system-health` every 30 s; a red warning banner
  appears when disk > 85 % or CPU temp > 75 °C
- `GET /api/system-health` endpoint available for external monitoring

### Audio playback and download ([#21](https://github.com/weaties/helmlog/issues/21), PR [#61](https://github.com/weaties/helmlog/issues/61))

WAV recordings can now be played or downloaded directly from the web UI
without needing to SSH into the Pi.

- Inline `<audio>` player on every History race card with an associated recording
- `GET /api/audio/{id}/stream` — browser-range-request compatible streaming
- `GET /api/audio/{id}/download` — downloads the WAV with `Content-Disposition: attachment`

### Photo caching ([#44](https://github.com/weaties/helmlog/issues/44), PR [#61](https://github.com/weaties/helmlog/issues/61))

Photo notes no longer reload on every page refresh, which was noticeably slow
over the boat's Wi-Fi hotspot.

- `serve_note_photo` now returns `ETag` + `Cache-Control: public, max-age=31536000, immutable`
- `304 Not Modified` responses on repeat loads (effectively free after first load)
- `loading="lazy"` on all photo `<img>` tags

---

## 2026-02-27

### Sail tracking ([#57](https://github.com/weaties/helmlog/issues/57), PR [#60](https://github.com/weaties/helmlog/issues/60))

- Sail inventory management on the Boats page (add / delete sails by type and name)
- Per-race sail selection on History race cards (main, jib, kite)
- Sail choices stored in `race_sails` table (schema v14)

### Guest crew position, WAV player on home page, debrief audio ([#38](https://github.com/weaties/helmlog/issues/38), [#49](https://github.com/weaties/helmlog/issues/49), [#31](https://github.com/weaties/helmlog/issues/31), PR [#59](https://github.com/weaties/helmlog/issues/59))

- Crew member positions include a **Guest** option
- The home page shows a live audio player when a recording is in progress
- Debrief sessions can be added with a named crew list

### YouTube video linking with Grafana deep links ([#22](https://github.com/weaties/helmlog/issues/22), PR [#51](https://github.com/weaties/helmlog/issues/51) / [#58](https://github.com/weaties/helmlog/issues/58))

- Link any YouTube video to instrument data via a UTC/offset sync point
- Every CSV export row gets a `video_url` deep-link (`?t=<seconds>`)
- Grafana annotation popups link directly to the matching video timestamp
- History page **Add Video** form with optional sync calibration

### Grafana annotations endpoint ([#52](https://github.com/weaties/helmlog/issues/52), PR [#54](https://github.com/weaties/helmlog/issues/54))

- `POST /api/grafana/annotations` — creates Grafana annotations from race/practice events
- Enables click-through from Grafana time-series panels to race timestamps

### Git version in web UI footer ([#55](https://github.com/weaties/helmlog/issues/55), PR [#56](https://github.com/weaties/helmlog/issues/56))

- Branch name and short commit SHA shown in the footer of every web page
- Makes it easy to confirm which version is running on the Pi

### Grafana InfluxDB datasource and dashboards (#earlier)

- Boatspeed, wind, heading, depth, position — all provisioned at setup time
- `can-interface.service` → `signalk.service` → `helmlog.service` dependency chain

### External data: weather and tides

- Open-Meteo hourly weather (wind, air temp, pressure) fetched once per hour
- NOAA CO-OPS hourly tide predictions fetched once per day
- Both written to SQLite and included as extra columns in CSV exports

### Audio recording

- Automatic WAV recording from USB Audio Class devices (Gordik 2T1R tested)
- One file per session in `data/audio/`, named by UTC start timestamp
- Graceful degradation — no device means instrument logging continues unaffected
