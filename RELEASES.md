# Release Notes

## Unreleased (main, 2026-03-02)

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
