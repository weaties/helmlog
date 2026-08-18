# HelmLog — Feature Reference

A single-page index of every feature in HelmLog, broken out by audience.

This document is a tour of *what's available*, not a how-to. Each section
points to the detailed guide where one exists. If you are looking for
race-day step-by-step instructions, start with the
[Operations Guide](operators-guide.md). If you are setting up a Pi for the
first time, start with the [README](../README.md) and
[Bootstrap a New Pi](bootstrap-new-pi.md).

> Feature availability assumes a current build (schema v77 or later).
> Most features below have shipped in the last six months; the
> [`RELEASES.md`](../RELEASES.md) file lists per-version highlights.

## Audiences

- **[Crew](#crew)** — people on the boat during racing. Mostly phones and
  tablets on the boat's Tailscale network. Authenticated, role `crew` or
  `admin`. Optimized for one-handed touch use, glanceable layouts, and
  fast taps with wet hands.
- **[Viewer](#viewer)** — anyone looking back at the data after the race.
  Sailors during debrief, coaches with delegated access, fleet members
  via the federation API, anyone with a `viewer` (or higher) role on the
  boat. Read-mostly; the focus is comprehension, comparison, and
  exporting.
- **[Admin](#admin)** — the operator who set up the Pi (typically the
  boat owner or designated geek). Has the `admin` role. Configures
  hardware, manages users, runs identity / co-op operations, watches
  health, deploys updates.

A feature can appear in more than one audience when it is genuinely used
by both. The auth column on each subsection notes the minimum required
role.

---

## Crew

Used during racing. Auth: `crew` or `admin`, except where noted.

### Race control (`/control`)

The race-day cockpit on a phone or tablet. One-tap operations the helm,
tactician, or pit can hit without breaking flow.

- **Start race / End race** — opens a session, tags every instrument
  reading and audio frame for the duration, and auto-closes any prior
  race that was left open. The next start increments the race number
  for the day.
- **Start practice** — same as a race, classified separately so the
  history filter can hide them.
- **Start debrief** — multi-channel debrief recording (#648). When you
  configure separate channels (helm on the USB lavalier, tactician on
  Bluetooth, etc.), each channel records to its own track and the
  transcript shows who-said-what without diarisation guesswork.
- **Schedule races** — pre-fill the day's race schedule so each start
  pulls the configured event name and number.
- **Event name** — auto-fills on Monday (BallardCup) and Wednesday
  (CYC); free-form on other days, persisted across logger restarts.
- **Live duration counter** — ticks every second while a race is open.

### Race start (`/race-start`)

Touch-optimised race timer and start-line tool. Designed for one-handed use
at the helm. Auth: `crew` to control; `viewer` for read-only display.

- **Timer display** — large countdown clock. Colour shifts to amber inside
  the last 60 seconds and green at 0:00 (gun).
- **Start** — opens a new race (silently closes any race left open), starts
  the countdown timer. Sends `start` to B&G instruments when integration
  is ON.
- **Stop Race** — freezes the timer at the current remaining time and ends
  the race in the database. Sends `stop` to B&G instruments when
  integration is ON.
- **Reset** — resets the countdown to the stored duration without stopping
  the timer. Sends `reset` to B&G instruments.
- **Sync** — snaps the running timer to the nearest whole minute. Use when
  you hear the prep gun slightly late. Sends `nearest-minute` to B&G.
- **Set Start Value** — disabled while the timer is running. Tap to replace
  the clock display with a numeric input; enter whole minutes (1–60) and
  confirm. The new duration takes effect on the next Start. Sends `SET` to
  B&G instruments when integration is ON.
- **Rolling Timer** — toggle switch. When ON the timer automatically restarts
  at the stored duration when the countdown reaches 0:00 (practice-start
  loops).
- **Boat-end ping / Pin-end ping** — record the current GPS position as one
  end of the start line. Hold Shift to enter coordinates manually if GPS is
  unavailable.
- **Line metrics panel** — computed from the two pinged end positions plus
  the boat's live GPS, COG/SOG, and TWD:
  - Right (primary): **Distance to line**, **Time to line** — large-format
    display for tactical use.
  - Left (secondary): Line bearing, Line length, Bias (favoured end and
    degrees).
- **Instrument Timer (B&G)** — toggle switch at the bottom of the page.
  When ON, inbound B&G timer events update the HelmLog clock, and Start
  page actions send CAN commands to instruments. When OFF, HelmLog operates
  standalone and B&G events are ignored. State persists through reboots.
  Switches ON automatically on the first inbound B&G event (headless mode).

See [`docs/specs/simradtimerintegration.md`](specs/simradtimerintegration.md)
for the full technical spec including headless mode and the CAN command table.

### Live instruments

- **Gauges panel** — boatspeed, true and apparent wind, heading, COG,
  SOG, depth, plus heel and trim from Signal K attitude (#646).
  Compass rose, BSP recovery bar, heel recovery bar.
- **WebSocket updates** — phone displays update in real time without
  manual refresh.
- **System health banner** — appears on the home page if disk usage
  exceeds 85 % or CPU temperature exceeds 75 °C (polled every 30 s).

### Crew management

- **Assign positions** — Helm, Main, Pit, Bow, Tac, Guest, with
  autocomplete from recent names and two-tier defaults (boat-wide
  defaults plus per-event overrides).
- **Default crew** — set the most common roster once; it pre-fills on
  every new race.
- **Voice consent** — record a per-person consent flag before audio is
  captured (required for diarisation under the data licensing policy).
- **Placeholder crew** — add a crew member who isn't in the user
  database (e.g. a one-time guest).

### Recording control

- **Audio** — automatic when a race or debrief starts; disabled if no
  USB audio device is plugged in. Multi-channel routing per device
  (#648).
- **Video — Insta360 X4** — start/stop spherical or single-lens
  recording from the **Cameras** page; status lights show streaming
  and recording state.
- **Video — local USB cameras** — ArUco markers in the camera frame
  can trigger named controls (start race, end race, etc.) so the
  helm can mark events without touching a phone.

### Notes and bookmarks

- **Text notes** — free-form observations attached to the active
  session, timestamped automatically.
- **Settings notes** — key/value pairs (vang, cunningham, jib lead)
  captured into the tuning history.
- **Photo notes** — phone-camera or photo-library uploads attached
  to the session.
- **Bookmarks** (#477) — drop a flag at the current moment so it
  surfaces in the post-race review.

### Sails and results

- **Per-race sails** — pick the main, jib, and kite from your
  inventory. Defaults pre-select. Selections feed sail-VMG analysis
  and roll up into the session detail page.
- **Race results** — log finish position, finishers' boat names, DNF
  / DNS flags. Linked to imported regatta results when available.

### Exports during the day

Each completed race in **Today's races** has **↓ CSV** and **↓ GPX**
buttons that download just that race's data — useful for sending an
immediate snapshot to a coach or fleet captain.

---

## Viewer

Used post-race. Auth: `viewer` or higher for the local boat;
peer-authenticated co-op access for federated viewers (see
[`guide-coaches.md`](guide-coaches.md) and
[`guide-federation.md`](guide-federation.md)).

### History (`/history`)

Searchable, filterable list of every recorded session.

- **Filters** — event, date range, type (race / practice / debrief),
  crew member, tags.
- **Search** — across session names, notes, and crew.
- **Per-card actions** — open the session detail page, download
  CSV / GPX / JSON / WAV, play the audio inline, link a YouTube
  video.

### Session detail (`/session/{id}`)

The deep-dive page. Everything HelmLog knows about a session, in
one place.

- **Track map** — full GPS track on Leaflet, with maneuver markers,
  start-line / mark-rounding overlays from imported courses,
  zoom presets (S / M / L / XL), and collapsible side panels.
- **Wind overlay** — TWS/TWD heatmap and wind-barb timeline (#557,
  #558).
- **Polar overlay** — actual boatspeed plotted against the boat's
  polar baseline for each true wind angle observed. Async-cached
  rebuild when underlying data changes (#603).
- **Maneuver analysis v2** (#612, #640) — tacks and gybes detected
  from rate-of-turn, classified by direction (P→S / S→P), broken
  into entry / apex / exit phases, with HTW (heading-to-windward),
  median time, spread (fastest vs. slowest), and ladder loss
  metrics. Overlay chart on the track shows phase boundaries.
- **Gauges panel** (#646) — heel, trim, BSP recovery bar, compass
  rose during playback.
- **Replay scrubber** — drag through the session at any speed; all
  panels stay in sync.
- **Video** — embedded YouTube player with deep-links to each
  second; multi-camera overlays during maneuver windows (#641).
- **Crew & sails** — who was on board, what sails were up, and
  when sails were changed.
- **Notes** — text, settings, and photo notes captured during the
  race.
- **Discussion threads** (#478, #592) — anchored to a moment, a
  maneuver window, a bookmark, or a mark rounding. `@mention`
  notifications, resolve / unresolve, edit, delete, anonymize
  speaker.
- **Tuning extraction** — sail-trim and rig-setting mentions are
  surfaced from the transcript for quick review and
  accept/dismiss.
- **Tags** (#587) — apply across sessions, notes, and maneuvers;
  merge tags from the admin page.
- **Bookmarks** (#477) — lightweight pins; useful as discussion
  anchors.
- **Deep-link** (#642) — append `?t=<seconds>` to the session URL
  to land directly on a moment. The Share button generates a
  shareable URL with the current cursor.

### Maneuvers (`/maneuvers`, `/compare`)

Cross-session analysis once enough sessions are recorded.

- **Maneuver browser** (#584) — every tack and gybe across every
  session, with wind-range filtering (e.g. 8–12 kts), direction pill
  (port-to-starboard or starboard-to-port), regatta filter, and
  sail-config filter.
- **Compare page** (#566–#583) — pick any subset of maneuvers and
  view them side-by-side: synced multi-video panels, mute controls,
  offset sliders, compass rose, BSP recovery bar, wind range filter.
- **Export** — maneuvers table to CSV with all metrics.

### Sails (`/sails`)

- **Inventory** — every sail with type, name, point-of-sail
  classification, and notes. Soft-delete (retire) when no longer in
  use.
- **Defaults** — set per-point-of-sail defaults so new races
  pre-select them.
- **Performance stats** — usage hours, sessions, average BSP, and
  sail-VMG numbers per sail.

### Audio and transcripts

- **Inline player** — play audio attached to any session directly in
  the History or session pages.
- **Transcript** — faster-whisper transcription with optional
  pyannote speaker diarisation, color-coded blocks per speaker,
  manual speaker reassignment per segment or by time range, and
  speaker anonymization. Remote-offload over Tailscale to a Mac
  is supported (see [`transcription-offload.md`](transcription-offload.md)).
- **Search** — transcript text is searchable from the History page.

### Exports

Available on every history card and session detail page.

| Format | Best for |
|---|---|
| CSV | Spreadsheets, Sailmon, custom analysis |
| GPX | Navigation apps, course replay tools |
| JSON | Custom scripts, programmatic analysis |
| WAV | Raw audio for external transcription / editing |

CSV columns include the per-second instrument values plus weather
(`WX_TWS`, `WX_TWD`, `AIR_TEMP`, `PRESSURE`), tides (`TIDE_HT`), and
a deep-linked `video_url` for any second covered by a linked video.
See [README → Exports](../README.md#exports) for the full column
reference and [`docs/data-licensing.md`](data-licensing.md) for the
boat-owns-its-data export guarantee.

### Federation — viewing peer data

When the boat is in a co-op (see [`guide-federation.md`](guide-federation.md)):

- **Co-op session list** — fellow co-op boats' shared sessions,
  fetched live via the peer API over Tailscale.
- **Peer track overlay** — load another boat's track onto your own
  session map for head-to-head visualization.
- **Session matching** — when two boats raced the same race,
  HelmLog auto-proposes a match by time and proximity. Accept,
  reject, or rename. Once matched, both boats' tracks render
  together on either side's session page.
- **Coach access** — per-boat, time-limited grants. Coaches see
  instrument data, polar deltas, and benchmarks but never audio,
  notes, crew, or sails (per the data licensing policy).
- **Field allowlist** — only LAT, LON, BSP, HDG, COG, SOG, and the
  wind fields cross the wire to peers. PII (audio, photos, emails,
  diarized transcripts) stays on the originating boat.

### Visualization and analysis plugins

- **Visualization catalog** — pluggable renderers (polar scatter,
  speed-VMG timeseries, track performance map). Users pick a
  default per analysis type.
- **Analysis catalog** — pluggable models (polar baseline, sail
  VMG, plus admin-approved community plugins). A/B compare two
  models on the same session.
- **Per-user preferences** — favorite chart, preferred analysis
  model, color scheme.

### Notifications and discussion

- **Attention page** (`/attention`) — `@mention` inbox from
  discussion threads.
- **Notifications API** — read / dismiss / count.
- **Threaded comments** — anchored to moments, maneuvers, or
  bookmarks; resolve / unresolve workflow.

### External regatta results

- **Imported regattas** — Clubspot and STYC race results pulled in
  by the admin; viewers can see series standings, fleet classes,
  and individual race results.
- **Match local session to external race** — link your locally
  recorded session to the external race so finish positions appear
  alongside instrument data.

### YouTube video linking

- **Manual link** — `helmlog link-video --url … --sync-utc … --sync-offset …`
  to register a one-time sync between an instrument timestamp and a
  position in the video.
- **Automated pipeline** — for boats running the
  [Insta360 video pipeline](video-pipeline.md), uploads are
  auto-stitched, uploaded to YouTube, matched to sessions by
  timestamp, and linked.
- **Auto-association by channel** — `helmlog sync-videos
  --channel-id …` matches recent uploads on a channel against
  recent sessions by timestamp.

### Themes and timezone

- **Six color themes** — Ocean (default), Slate, Sunset, Forest,
  Sunlight (high-contrast outdoor), Night (low-light). All WCAG
  contrast-validated. Per-user override; admin sets the boat
  default.
- **Timezone-aware UI** — set `TIMEZONE` in `.env` and all
  timestamps render in local time. Race grouping and weekday
  auto-naming use the local weekday.

---

## Admin

Used by the operator. Auth: `admin`. Many actions are also available
on the CLI for emergency / scripted use.

### User and role management (`/admin/users`)

- **Roles** — `admin`, `crew`, `viewer`, plus a developer flag for
  extra surfaces.
- **Invite users** — generate a single-use invite link emailed to
  the recipient. Recipient sets a password (or signs in with
  Google / Apple / GitHub OAuth if configured).
- **Password reset** — forgot-password flow with one-hour token.
- **OAuth providers** — Google, Apple, GitHub, configurable via
  `OAUTH_*` environment variables.
- **Session TTL** — 90 days by default (`AUTH_SESSION_TTL_DAYS`).
- **Deactivate users** — soft-delete that preserves history.

### Boats (`/admin/boats`)

- **Boat registry** — sail number, name, class. Linked to race
  results when matching boats are imported from external regattas.
- **Boat identity** — Ed25519 keypair, fingerprint, owner email
  (used for federation requests).

### Cameras (`/admin/cameras`)

- **Insta360 cameras** — register by name and IP address. Live
  status (streaming / recording) and remote start/stop control via
  the Insta360 OSC HTTP API.
- **USB cameras** — managed via the ArUco page (next).

### ArUco (`/admin/aruco`)

Visual control panel for boats with USB cameras pointed at the
cockpit.

- **Camera registration** — name, USB device, exposure / ISO.
- **Calibration** — generate a checkerboard PDF, capture
  calibration images, store calibration profiles per camera, and
  switch profiles dynamically.
- **MJPEG preview** — live preview of any registered camera for
  alignment.
- **Marker bindings** — bind ArUco marker IDs to named controls
  (start race, end race, start debrief, etc.). When the camera
  detects the marker, the bound control fires.

### Controls (`/admin/controls`)

- **Control catalog** — define named race-day operations and group
  them into categories.
- **Trigger sources** — bind each control to an ArUco marker, an
  audio trigger word, or both.
- **Trigger words** — keyword list scanned across transcripts so a
  spoken phrase ("start race") fires the same action as a button
  tap.

### Audio channels (`/admin/audio-channels`) (#648)

- **Device enumeration** — list every available USB audio input.
- **Channel routing** — name each channel (`helm`, `tactician`,
  `pit`) and bind it to a device input.
- **Per-session overrides** — re-assign a channel after the fact
  if a device was unplugged or relabeled mid-session.

### Boat settings and calibration

- **Calibration parameters** — leeway, BSP scale, AWA offset, AWS
  scale, etc. Stored as time-effective rows so older sessions keep
  their original calibration.
- **Tuning extraction runs** — review tuning items extracted from
  transcripts; accept or dismiss each.
- **Tuning compare** — diff calibration sets across dates.

### Polar baseline

- **Build polar** — `helmlog build-polar --min-sessions N` rebuilds
  the polar diagram from historical session data.
- **Async rebuild** (#603) — UI-triggered rebuild runs in the
  background; progress is polled.

### Tags (`/admin/tags`)

- **Create / rename / merge** — tags applied across sessions,
  notes, and maneuvers. Merge to consolidate after typos or
  evolving naming.

### Analysis catalog (`/admin/analysis`)

- **Plugin discovery** — auto-discovered plugins from
  `analysis/plugins/`.
- **Approve / deprecate** — gate which plugins are visible to
  viewers.
- **Set default** — pick the default analysis model per analysis
  type.
- **Propose plugin** — viewers can propose; admin approves or
  rejects.

### Vakaros (`/admin/vakaros`)

- **VKX inbox** — files dropped to the inbox path are watched and
  ingested. Status of each file (queued / ingested / failed) shown.
- **Rematch** — re-run session-matching after metadata changes.

### Race results (`/admin/race-results`)

- **Discover regattas** — query a provider (Clubspot, STYC) for
  upcoming or past regattas.
- **Import** — pull series, races, and results into the local DB.
- **Rematch** — retry matching local sessions to imported races
  after data corrections.

### Network (`/admin/network`)

- **Status** — current SSID, IP, signal strength, link state.
- **Profiles** — manage Wi-Fi networks via NetworkManager
  (`nmcli`). Add, remove, switch.

### System and deployment

- **Settings** (`/admin/settings`) — boat-wide config: default color
  scheme, external-data fetching toggle, metering preferences.
- **Devices** (`/admin/devices`) — issue and rotate scoped API
  keys for headless devices (ESP32 sensors, etc.).
- **Deployment** (`/admin/deployment`) — current Git SHA, branch,
  pipeline status, deploy history. Promote between
  `main → stage → live` (developer flag required).
- **Audit log** (`/admin/audit`) — every user action plus every
  co-op data access, with IP, user-agent, and timestamp.
- **Event rules** (`/admin/events`) — day-of-week event-name
  defaults.

### Federation (`/admin/federation`)

- **Identity init** — generate the boat's Ed25519 keypair and
  boat card.
- **Co-op create** — start a new co-op with this boat as moderator.
- **Invite** — generate an invite bundle for a peer boat.
- **Co-op status** — membership, peers, sharing controls.
- **Sharing controls** — per-session, per-co-op share toggle, with
  optional embargo timestamps for delayed visibility.

### Email (SMTP)

- **Welcome emails** — sent automatically when a user is created
  via CLI or the admin UI.
- **New-device alerts** — notify a user when their account is
  used from an unfamiliar IP / user-agent.
- **Configuration** — set `SMTP_*` variables in `.env`. Without
  them, login links print to the journal and email features are
  no-ops.

### CLI surface (`helmlog …`)

The CLI exists for setup and emergency operation; day-to-day use
goes through the web UI.

| Command | Purpose |
|---|---|
| `helmlog run` | Start the logging loop (the systemd service runs this) |
| `helmlog status` | Show DB row counts and last-seen timestamps |
| `helmlog export --start … --end … --out …` | Export a time range |
| `helmlog list-devices` | List audio input devices |
| `helmlog list-audio` | List recorded audio sessions |
| `helmlog list-cameras` | List configured Insta360 cameras |
| `helmlog list-videos` | List linked YouTube videos |
| `helmlog link-video --url … [--sync-utc … --sync-offset …]` | Manual video link |
| `helmlog link-channel-videos --channel-id …` | Match a YouTube channel to races |
| `helmlog sync-videos [--channel-id …]` | Auto-associate YouTube uploads with sessions |
| `helmlog add-user --email … --role …` | Bootstrap or add a user |
| `helmlog build-polar --min-sessions N` | Rebuild polar baseline |
| `helmlog detect-maneuvers [--session ID \| --all]` | Re-run maneuver detection |
| `helmlog scan-transcript [--session ID \| --all]` | Re-scan transcripts for trigger words |
| `helmlog vakaros-ingest PATH` | Ingest a Vakaros VKX log |
| `helmlog identity init / show` | Manage boat identity |
| `helmlog co-op create / status / invite` | Manage co-op membership |
| `helmlog --help` | Full subcommand list |

### Background services (the operator should know exist)

- **Logger service** (`helmlog.service`) — main loop; depends on
  Signal K, which depends on `can-interface`.
- **Weather loop** — Open-Meteo fetch every hour, position-keyed.
- **Tide loop** — NOAA CO-OPS fetch once a day, two days
  forward, idempotent.
- **System monitor** — psutil → InfluxDB every 60 s.
- **Evergreen deploy loop** — opt-in via `DEPLOY_MODE=evergreen`;
  polls the configured branch every five minutes and deploys
  automatically when commits land.
- **Audio recorder** — starts when a race or debrief begins and the
  configured device is present; falls through silently when not.
- **Transcription worker** — local faster-whisper, or remote-offload
  to a Mac over Tailscale via `TRANSCRIBE_URL`.
- **ArUco poll** — frame-by-frame marker detection on registered
  USB cameras.

### Hardware-isolation map (for orientation)

Hardware access lives in a small, fixed set of modules. Anything
outside this list works on decoded data structures and is testable
without hardware:

- `sk_reader.py` — Signal K WebSocket (primary data source)
- `can_reader.py` — direct CAN (legacy, `DATA_SOURCE=can`)
- `audio.py` / `usb_audio.py` — sounddevice
- `cameras.py` / `insta360.py` — Insta360 OSC HTTP API
- `aruco_detector.py` — OpenCV ArUco
- `network.py` — `nmcli`
- `monitor.py` — psutil

---

## Data licensing in one paragraph

Every feature here is governed by the
[Data Licensing Policy](data-licensing.md). The short version: the boat
owns its data and can always export it; PII (audio, photos, emails,
biometrics, diarised transcripts) has deletion and anonymization
rights; co-op data is view-only over the wire (no bulk export, no
protest-format export, no betting use); biometric features require
per-person consent independent of the boat owner. When in doubt,
read the policy or run `/data-license` against the change.

---

## Where to go from here

| If you are a… | Start here |
|---|---|
| Crew member on race day | [`operators-guide.md`](operators-guide.md) |
| Sailor wondering what's shared in a co-op | [`guide-sailors.md`](guide-sailors.md) |
| Coach with delegated access | [`guide-coaches.md`](guide-coaches.md) |
| Fleet champion pitching adoption | [`guide-champions.md`](guide-champions.md) |
| Boat owner setting up federation | [`guide-federation.md`](guide-federation.md) |
| Operator setting up a new Pi | [`bootstrap-new-pi.md`](bootstrap-new-pi.md) |
| Developer | [`../AGENTS.md`](../AGENTS.md) (canonical conventions), [`../CONTRIBUTING.md`](../CONTRIBUTING.md) |
