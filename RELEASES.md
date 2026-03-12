# Release Notes

## Sprint 2 complete — Performance Analysis & Synthesizer (2026-03-12)

Sprint 2 (March 10–24) is feature-complete. Three stretch/secondary items were
punted to Sprint 3: #226 (federation integration tests — Docker + Pi smoke),
#186 (YouTube channel video linker script), and #166 (deep-link transcript
segments to YouTube timestamps).

### Performance analysis

- **Maneuver detection** (#232) — automatic tack and gybe detection from 1 Hz
  heading data, surfaced on the session detail page
- **Polar performance visualization** (#233) — polar diagram overlay on the
  session detail page showing boat performance against the J/105 target polar

### Synthesizer improvements

- **Spatially varying wind model** (#248) — wind direction and pressure
  gradients across the course area instead of a single uniform wind field
- **Tack on headers with realistic randomization** (#247) — synthesized boats
  now tack when lifted, with heading noise and timing jitter for realistic
  tracks
- **Wind model sharing between co-op members** (#246) — co-op boats in a
  synthesized session share the same wind field and start time so their tracks
  are physically consistent
- **Fix: leg-derived marks for wind field visualization** (#264) — wind field
  overlay now uses the actual mark positions from leg geometry instead of the
  original placed marks

### Deploy & infrastructure

- **Promotion history and branch comparison** (#258) — deploy admin page shows
  a commit-level diff between the current branch and the promotion target
- **Fix: create loki/promtail groups before chown** (#261) — setup.sh no longer
  fails on a fresh Pi when the loki/promtail system groups don't exist yet

---

## Promoted to live and stage from main, 2026-03-10

### Synthesize race sessions with interactive Leaflet map (#245, #252)

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

### Federation phase 1 — identity, co-op data model, session sharing (#224)

Boat-to-boat federation with cryptographic identity:

- **Ed25519 keypair generation** — `helmlog identity init` creates a boat card
  with public key and fingerprint
- **Co-op data model** — create, join, and manage cooperative groups of boats
- **Session sharing** — share/unshare sessions with co-op members, with embargo
  enforcement and audit logging
- **Peer API** — inter-boat HTTP endpoints with request signing and verification
- 32 integration tests covering co-op lifecycle, auth, embargo, and data licensing

### Deployment management and promotion workflow (#222, #223)

Structured release process with stage/live branches:

- **Admin deploy page** — view current version, trigger updates, and monitor
  deploy status from the web UI
- **Evergreen mode** — opt-in automatic deployment on push to the tracked branch
- **Promotion workflow** — `main` → `stage` → `live` branch promotion with
  safety checks

### Data policy compliance — privacy, auth, and deletion controls (#194–#211, #215)

Comprehensive data licensing policy enforcement:

- **PII deletion** — audio, photos, transcripts, and diarized content can be
  deleted per data policy requirements
- **Field allowlists** — co-op API endpoints enforce strict field filtering
- **Audit logging** — all data access and sharing events are logged
- **Private session isolation** — unshared sessions are invisible to co-op peers

### Configurable Pi health monitor interval (#249, #250)

- Default collection interval changed from 60 s to 2 s for smoother dashboards
- Non-blocking CPU measurement via `psutil.cpu_percent(interval=None)`
- `MONITOR_INTERVAL_S` env var and admin settings page for runtime tuning

### Mobile navigation overhaul (#230, #231)

- **Hamburger menu** replaces tab navigation for better mobile usability

### Infrastructure and developer experience

- **GitHub Actions CI** — tests, lint, and type checking on every PR (#219)
- **Docker dev container** — Claude Code development environment (#229)
- **Community contribution infrastructure** — CONTRIBUTING.md, issue templates,
  PR template (#174, #218)
- **Fan speed** added to Pi Health Grafana dashboard with shared crosshairs (#225)
- **Hostname in footer** — version info now includes the Pi hostname (#175, #221)
- **reset-pi.sh** — restore a Pi to pre-setup state for reimaging (#242)
- **Git ownership fix** — prevent `.git/` conflicts between helmlog service and
  deploy user (#239, #240)
- **Bootstrap add-user fix** — run as correct user to avoid read-only DB (#244)
- **Documentation** — approachability guides, fleet quickstart, operator updates,
  federation protocol gaps (#216, #217, #220)
- **CLAUDE.md** — added `uv sync` dependency guidance (#251)

---

## 2026-03-08

### Embedded YouTube player with track sync (#183, #185)

Session replay now includes synchronized video:

- **Embedded YouTube player** on session detail page and history page cards
- **Bidirectional track sync** — click a point on the track map and the video
  jumps to that moment; video playback updates the track marker position
- Deep-link support via `?t=<seconds>` for sharing specific race moments

### Session detail page with track map (#178, #180)

Dedicated session view at `/session/{id}`:

- **Interactive track map** — GPS track rendered with speed-based color coding
- **Video deep-links** — clickable timestamps jump to the corresponding video
- All session metadata (crew, results, notes, transcripts, sails, exports)
  consolidated into a single page replacing the history accordion cards

### Simplified home page (#170, #171)

Home page redesigned for race-day focus:

- **Idle state** — start buttons only (race, practice, debrief)
- **Active state** — current race card with live instruments and controls
- Red stop button with two-tap safety guard and countdown timer
- Camera start/stop runs fire-and-forget so race API responds instantly
- Extracted inline HTML/CSS/JS from `web.py` into Jinja2 templates and
  static files (`base.html`, `home.html`, `history.html`, `base.css`,
  `shared.js`, `home.js`, `history.js`)

### Gaia GPS backfill (#101, #176)

Import historical race data from Gaia GPS exports:

- **Track download** — fetch GPX tracks from Gaia GPS API
- **Race classification** — auto-detect race sessions from track patterns
- **SQLite import** — backfill position, heading, and speed data
- **InfluxDB migration** — push historical data to Grafana dashboards

### Insta360 X4 video pipeline (#155, #161)

End-to-end automated video workflow:

- **Camera control** — start/stop recording via OSC HTTP API, tied to race
  start/stop events (#98, #147)
- **Camera admin UI** — add/configure cameras, WiFi credentials, status
  monitoring from the web interface (#147)
- **Stitch & upload** — Docker-based stitcher with ffmpeg fallback; discovers
  both `.insv` (360°) and `.mp4` (single-lens) recordings
- **Auto-link** — uploaded videos automatically associated with race sessions
  by time overlap
- WiFi SSID/password fields on camera config for direct camera network access

### Remote transcription offload (#121, #146)

Offload Whisper transcription from the Pi to a faster machine:

- **HTTP worker** (`scripts/transcribe_worker.py`) runs on a Mac or other
  machine with more CPU
- **Admin settings page** — SQLite-backed settings overrides configurable
  from `/admin/settings` in the web UI
- Transparent fallback to local Pi transcription if remote worker is
  unreachable

### Nginx reverse proxy (#137, #167)

Single-port access to all services on the Pi:

- **Path-based routing** — `/` (Helm Log), `/grafana/` (Grafana), `/sk/`
  (Signal K admin), `/signalk/` (Signal K API)
- Eliminates the need to remember multiple port numbers
- Signal K admin UI moved to `/sk/` to avoid conflict with `/admin/`

### Configurable event naming rules (#154, #159)

Day-of-week event names are now admin-configurable:

- Admin UI at `/admin/events` for creating and managing event rules
- Custom event names (set via web UI) take precedence over weekday defaults
- Error message shown when starting a race without a daily event set (#153)

### Self-service login (#148, #152)

Existing users can request a new magic link from the login page without
needing an admin to generate an invite token.

### Configurable Pi host references (#151)

Removed hardcoded `corvopi` / `weaties` references from scripts and docs.
All Pi-specific values now come from environment variables or are
auto-detected.

### Infrastructure fixes

- **Loki + Promtail** for centralized log management (#139, #142), with
  loopback-only config fix (#162)
- **Setup.sh fixes** — bcrypt module, data/ permissions, uv Python
  traversal, tmux, locale (#140, #141, #144)
- **Signal K** — fix CORS origins crash on fresh install (#143)
- **Grafana** — bind to 0.0.0.0 for Tailscale access, auto-populate
  InfluxDB vars in `.env`, anonymous access with None role for login page
- **Race Track query optimization** — downsample before union, 65x faster
- **Database schema documentation** at `docs/database-schema.md` (#156, #157)

### Shared navigation, version footer, timezone support (#129, #130)

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

### Audit log, tags, triggers, headshots, admin nav (#93, #94, #99, #100, #123)

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

### Speaker diarisation on Pi 5 (#120)

Pyannote speaker diarisation now works on the Pi 5 (aarch64):

- Enabled `pyannote-audio` pipeline for speaker labeling (`SPEAKER_00`,
  `SPEAKER_01`, …) on transcriptions when `HF_TOKEN` is configured
- Bypassed `torchcodec` `AudioDecoder` which fails on aarch64 — uses
  `soundfile` fallback instead
- Set `HOME` env var in systemd unit so `faster-whisper` can cache models
  under the `helmlog` service account

### Security hardening (#117, #118)

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

### Race event naming fix (#122)

Custom event names (saved via the web UI) now take precedence over the
weekday defaults (BallardCup on Monday, CYC on Wednesday). Previously,
a saved custom event was ignored on Monday/Wednesday.

### Security audit documentation

Added security audit report and penetration test statement of work
(`docs/`) documenting the 2026-03-01 security review.

---

## 2026-03-01

### Audio transcription (#42, PR #63)

Completed recordings can now be transcribed to text directly from the History
page. Transcription runs on the Pi via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no cloud service
required.

- **📝 Transcript** button on every audio-enabled race card in History
- Jobs run in the background; status polling shows a spinner until complete
- Transcripts are stored in SQLite (`transcripts` table, schema v15)
- Model is configurable via `WHISPER_MODEL` env var (default: `base`)
- Speaker diarisation deferred — pyannote.audio is too heavy for Pi CPU

### System health monitoring (#39, PR #62)

The logger now watches the Pi's vitals automatically.

- `monitor_loop` background task collects CPU/memory/disk/temperature via
  `psutil` every 60 s and writes a `system_health` measurement to InfluxDB
- Home page polls `/api/system-health` every 30 s; a red warning banner
  appears when disk > 85 % or CPU temp > 75 °C
- `GET /api/system-health` endpoint available for external monitoring

### Audio playback and download (#21, PR #61)

WAV recordings can now be played or downloaded directly from the web UI
without needing to SSH into the Pi.

- Inline `<audio>` player on every History race card with an associated recording
- `GET /api/audio/{id}/stream` — browser-range-request compatible streaming
- `GET /api/audio/{id}/download` — downloads the WAV with `Content-Disposition: attachment`

### Photo caching (#44, PR #61)

Photo notes no longer reload on every page refresh, which was noticeably slow
over the boat's Wi-Fi hotspot.

- `serve_note_photo` now returns `ETag` + `Cache-Control: public, max-age=31536000, immutable`
- `304 Not Modified` responses on repeat loads (effectively free after first load)
- `loading="lazy"` on all photo `<img>` tags

---

## 2026-02-27

### Sail tracking (#57, PR #60)

- Sail inventory management on the Boats page (add / delete sails by type and name)
- Per-race sail selection on History race cards (main, jib, kite)
- Sail choices stored in `race_sails` table (schema v14)

### Guest crew position, WAV player on home page, debrief audio (#38, #49, #31, PR #59)

- Crew member positions include a **Guest** option
- The home page shows a live audio player when a recording is in progress
- Debrief sessions can be added with a named crew list

### YouTube video linking with Grafana deep links (#22, PR #51 / #58)

- Link any YouTube video to instrument data via a UTC/offset sync point
- Every CSV export row gets a `video_url` deep-link (`?t=<seconds>`)
- Grafana annotation popups link directly to the matching video timestamp
- History page **Add Video** form with optional sync calibration

### Grafana annotations endpoint (#52, PR #54)

- `POST /api/grafana/annotations` — creates Grafana annotations from race/practice events
- Enables click-through from Grafana time-series panels to race timestamps

### Git version in web UI footer (#55, PR #56)

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
