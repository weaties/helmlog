# Roadmap & TODO

Checked items are complete.

---

## Important — needed for reliable on-the-water operation

- [ ] **CAN HAT verified with live B&G traffic** — confirm each of the 7 supported PGNs decodes
      correctly against real instrument output. Use `candump` + the decoder and spot-check
      values against the chart plotter display.

- [ ] **B&G proprietary PGNs** — capture live CAN traffic and reverse-engineer any B&G-specific
      PGN payloads that carry data not covered by the standard PGNs.
      Document in `docs/pgn-notes.md`.

---

## Open — planned features

- [ ] **Boatspeed vs historical baseline** (#40) — query SQLite for `(TWS, TWA, BSP)` tuples,
      bucket by wind condition, surface a "are we fast or slow?" delta on the race page and in
      CSV exports. CLI: `helmlog build-polar`.

- [ ] **Public web access / auth** (#25) — magic-link invite tokens, role-based access
      (`admin` / `crew` / `viewer`), session cookies in SQLite, HTTPS deployment guide
      (Caddy / Cloudflare Tunnel / Tailscale Funnel).

- [x] **Grafana race track panel** (#18) — Geomap panel with speed-coloured GPS track,
      wind tooltip, and YouTube deep-link per track point.

- [ ] **External SSD** (#19) — mount at `/mnt/ssd`, relocate SQLite + audio + InfluxDB data,
      nightly `systemd` backup timer (`scripts/backup.sh`), graceful SD-card fallback.

- [ ] **Transcript export** — download transcript as plain text or PDF from the History UI
      (currently transcripts are stored in SQLite but not exportable from the web UI).

- [ ] **WEB_PIN access control** — env var is reserved; not yet implemented.

- [ ] **FastPacket reassembly** — support multi-frame NMEA 2000 messages
      (e.g. PGN 129029 GNSS Position Data) if needed for direct-CAN path.

- [ ] **Integration test replay** — replay a recorded `candump .log` file through the full
      stack (reader → decoder → storage → export) to catch regressions with real data.

### Data co-op platform

Items below track the data licensing policy technical requirements
(`docs/data-licensing.md` Section 12) and federation design phases
(`docs/federation-design.md` Section 13). Phases 1 and 2 of the federation
design are complete; Phases 3–5 remain.

#### Done (Phases 1 & 2)

- [x] **Boat identity model** — Ed25519 keypair generation, boat cards, fingerprints;
      CLI: `helmlog identity init/show`; stored in `~/.helmlog/identity/`.

- [x] **Co-op data model** — `co_op_memberships`, `session_sharing`, `co_op_peers`,
      `co_op_audit`, `request_nonces`, and `boat_identity` tables (schema v28);
      signed charter and membership records; revocation support.

- [x] **Session sharing** — per-session co-op share/unshare via session detail page
      (`POST /api/sessions/{id}/share`); storage, API, and data model complete.
      ⚠️ Setting an embargo timestamp is supported by the API (`embargo_until` field)
      but is not yet exposed in the webapp UI — embargo can only be set via the API
      directly (tracked as a follow-on UI task).

- [x] **Peer API** (`peer_api.py`) — `/peer/identity`, `/peer/sessions`,
      `/peer/sessions/{id}/track`, `/peer/sessions/{id}/results`; data field
      allowlist enforced per data licensing policy.

- [x] **Request authentication** (`peer_auth.py`) — Ed25519 signing middleware,
      nonce replay protection, clock-skew tolerance, rate-limit detection.

- [x] **Peer client** (`peer_client.py`) — async HTTP client for querying peers;
      used by the co-op view in `web.py`.

- [x] **Audit logging** — `co_op_audit` table records every peer data access
      (action, resource, points returned, bytes transferred); volume-based
      rate-limit detection with auto-freeze.

- [x] **Embargo enforcement** — API blocks track data for sessions under active
      embargo; tested end-to-end in `tests/integration/test_embargo_e2e.py`.

- [x] **Data licensing field allowlist** — peer API strips PII fields before
      serving to co-op members; tested in `tests/integration/test_data_license_e2e.py`.

- [x] **Integration test suite** — 32 in-process tests (Layer 1) across
      `test_federation_e2e.py`, `test_auth_e2e.py`, `test_embargo_e2e.py`,
      `test_data_license_e2e.py`; Docker compose suite (Layer 3) via
      `tests/integration/docker-compose.yml`.

- [x] **Delete / anonymization** — session hard delete (`DELETE /api/sessions/{id}`);
      user account anonymization (email replaced, name/avatar cleared, auth sessions
      purged via `DELETE /api/users/{id}`); diarized transcript speaker anonymization
      with per-speaker redaction map. ⚠️ The "30-day soft-delete grace period with
      recoverable suppression" described in earlier designs is not yet implemented —
      all deletes are currently immediate and irreversible.

- [x] **AIS exclusion** — 23 AIS PGNs and SK paths blocked at ingestion;
      non-member vessel tracking data never stored.

- [x] **No bulk export enforcement** — peer API is view-only (no bulk export
      endpoints); rate limiting + volume-based auto-freeze deter scraping;
      own-boat data export unrestricted.

- [x] **Audio PII deletion** — whole-recording deletion on crew PII request
      (`DELETE /api/audio/{id}`). ⚠️ API endpoint is complete but no UI button
      is currently exposed in the webapp — deletion must be done via the API directly.
      Per-segment editing is a future capability.

- [x] **Processing offload audit trail** — `transcribe.start` action recorded in
      the audit table when a transcription job is triggered; application log records
      which URL audio was sent to and how many chars/segments were returned. Warning
      logged when `TRANSCRIBE_URL` uses plain HTTP (non-localhost) to alert about
      unencrypted PII in transit.

- [x] **GPS precision control** — `?gps_precision=N` query parameter on export
      endpoints (`/api/sessions/{id}/export`) reduces coordinate precision to N
      decimal places (e.g. 2 dp ≈ 1.1 km resolution). ⚠️ No webapp UI; export
      must be triggered via the API to use this. The peer API does not
      currently enforce reduced precision on track data served to co-op members.

- [x] **Auth hardening** — auth required on all data-reading GET endpoints;
      crew+ auth required for audio download/stream/transcript.

#### Phase 3 — Fleet benchmarking (not started)

- [ ] **Maneuver detection** (`maneuver_detect.py`) — detect tacks, gybes, mark roundings,
      starts, and acceleration events from instrument data (heading rate, BSP delta, GPS
      track geometry). Store as typed events in `maneuver_events` table. Auto-calibrate
      thresholds from co-op data.

- [ ] **Condition binning** — bucket sessions and maneuvers by environmental conditions
      (TWS bands, wave state) so benchmarks compare apples-to-apples. Configurable bins
      at the co-op level (charter field).

- [ ] **Fleet benchmark engine** (`benchmarks.py`) — compute anonymous aggregate statistics
      (median, 10th/25th/75th/90th percentiles) per maneuver type per condition bin across
      all contributing co-op boats. Enforce minimum 4-boat threshold per bin; return
      "insufficient data" below threshold. Exclude embargoed sessions until embargo lifts.

- [ ] **Benchmark API endpoints** — `GET /peer/benchmarks/maneuvers` and
      `GET /peer/benchmarks/polar`; per-boat serving only (no cross-boat breakdowns);
      rate-limited and audit-logged.

- [ ] **Percentile Heatmap dashboard** — single-screen visualization: maneuver ×
      fleet 10th%/median/90th%/your result/your percentile. Color-coded green/yellow/red.
      The primary co-op value proposition.

- [ ] **Maneuver detail drilldown** — click any heatmap row to see historical trend
      (percentile over time), per-session breakdown, condition scatter. Own-boat only.

- [ ] **Benchmark embargo sync** — exclude embargoed session data from benchmark
      computation until embargo lifts.

#### Phase 4 — Governance & voting (not started)

- [ ] **Proposal creation and signing** — admin creates a signed proposal record;
      distributed to all co-op members via peer push or pull.

- [ ] **Vote collection** — each boat's Pi signs a vote (approve/reject); votes are
      idempotent and verifiable by any peer; stored in `co_op_votes` table.

- [ ] **Resolution records** — once threshold met (2/3 or unanimous per proposal type),
      admin signs a resolution and distributes to all peers; all Pis update local
      agreement state.

- [ ] **Pre-join disclosure endpoint** — surface all active co-op agreements (commercial,
      ML, current model, cross-co-op) to prospective members before admission.

- [ ] **Membership eligibility enforcement** — active racing requirement; commercial
      actors must use coach access, not co-op membership.

#### Phase 5 — Current models & advanced features (not started)

- [ ] **Per-event co-op exclusivity enforcement** — when a boat belongs to multiple co-ops,
      require assignment of each session to exactly one co-op at share time; UI enforces
      the choice before the session is shared.

- [ ] **Observed current model pipeline** — BSP/heading vs SOG/COG vector derivation,
      geographic scoping per sailing area, unanimous consent gating, per-area opt-out.

- [ ] **Cross-co-op isolation** — prevent query or aggregation across co-ops unless both
      co-ops vote (2/3 supermajority each) to allow it.

- [ ] **Coach access controls** — time-limited, view-only access grants (signed
      `coach_access` records); no-aggregation enforcement; mandatory deletion on expiry.
      Per-boat opt-in only; co-op admin does not control coach access.

- [ ] **Data aging tiers** — current season: full detail; previous season: reduced
      precision; older: summary only. Charter-configurable per co-op.

- [ ] **Peer caching (opt-in)** — source boat opts in to cacheability; receiving boat
      opts in to local storage; 30-day TTL; tombstone polling for cache invalidation;
      cache encrypted at rest.

- [ ] **Benchmark historical trends** — per-boat percentile trend over time;
      per-session breakdown and condition scatter. Own-boat data only.

---

## Completed

### Core logging
- [x] NMEA 2000 PGN decoders for 7 standard PGNs (127250, 128259, 128267, 129025, 129026, 130306, 130310)
- [x] Signal K WebSocket reader (`sk_reader.py`) — primary data source
- [x] Legacy direct-CAN path (`can_reader.py`) available via `DATA_SOURCE=can`
- [x] SQLite storage with async writes and integer-versioned migrations (schema v28)
- [x] Batch SQLite writes — flush every 1 s / 200 records
- [x] Timestamp indexes (schema migration v2)
- [x] Non-blocking recv via `asyncio.to_thread`
- [x] Graceful SIGTERM/SIGINT shutdown with buffer flush

### Export
- [x] CSV export joining all tables by second
- [x] GPX export (one `<trkpt>` per second with GPS position)
- [x] JSON export (typed nulls for missing data)
- [x] `video_url` column with YouTube deep-links in CSV export
- [x] Weather and tide columns (`WX_TWS`, `WX_TWD`, `AIR_TEMP`, `PRESSURE`, `TIDE_HT`)
- [x] GPS precision control — configurable decimal places; reduced precision (2 dp) for external API calls

### External data
- [x] Open-Meteo weather background task (hourly fetch by GPS position)
- [x] NOAA CO-OPS tide predictions (daily fetch, nearest station to position)

### Video
- [x] YouTube video metadata fetching via `yt-dlp`
- [x] `link-video` CLI with sync-point offset
- [x] Video deep-links in History page (📹 Videos panel)

### Web interface
- [x] FastAPI web app on port 3002 (`web.py`)
- [x] Race marker — Start / End race with event naming and auto day-of-week defaults
- [x] Practice and debrief session types
- [x] Live instrument data panel (BSP, TWS, TWA, HDG, COG, SOG, AWS, AWA, TWD)
- [x] History page — search, filter by type, date range, pagination
- [x] Race results (boat registry, place / DNF / DNS per race)
- [x] Race notes — text, key/value settings, photo upload (with ETag caching)
- [x] Crew tracking per race (6 positions with recent-sailor quick-tap chips)
- [x] Sail inventory (Boats page) and per-race sail selection
- [x] Grafana deep-link buttons scoped to race time window
- [x] System health warning banner (disk > 85 %, CPU temp > 75 °C)
- [x] Inline audio player and WAV download (`↓ WAV`)
- [x] Audio stream and download endpoints with range request support
- [x] Dedicated session detail page at `/session/{id}` (#180)
- [x] Simplified home page — idle: start buttons only; active: current race card (#170)
- [x] Hamburger menu navigation for mobile (#230)

### Audio
- [x] WAV recording per session via `sounddevice` (Gordik 2T1R / any UAC device)
- [x] `list-devices` and `list-audio` CLI subcommands
- [x] Audio session tied to race/practice/debrief session in SQLite

### Transcription
- [x] faster-whisper transcription running on Pi CPU (no cloud)
- [x] Speaker diarisation via pyannote.audio (opt-in with `HF_TOKEN`)
- [x] `segments_json` column in `transcripts` table (schema v16)
- [x] Colour-coded speaker blocks in History page transcript panel

### System health
- [x] psutil background task → `system_health` InfluxDB measurement every 60 s
- [x] `/api/system-health` endpoint
- [x] Home page warning banner for disk and temperature thresholds
- [x] Fan speed on Pi Health dashboard

### Infrastructure
- [x] Raspberry Pi setup script (`scripts/setup.sh`) — idempotent, installs full stack
- [x] Deploy script (`scripts/deploy.sh`) — pull + sync deps + restart service
- [x] `can-interface.service` → `signalk.service` → `helmlog.service` dependency chain
- [x] Grafana provisioning (datasource + dashboards) via `scripts/provision-grafana.sh`
- [x] CAN HAT hardware setup & loopback testing on Pi (all 7 PGNs verified in loopback)
- [x] Full test suite (700+ tests — all modules covered)
- [x] ruff + mypy clean
- [x] GitHub Actions CI workflow (tests, lint, type checking) (#219)
- [x] Docker-based Claude Code dev container (#229)
- [x] Deployment management admin page, evergreen mode (#222)

### Licensing & governance
- [x] AGPLv3 software license (`LICENSE`)
- [x] Data licensing policy (`docs/data-licensing.md`) — 13-section policy covering data
      ownership, co-op sharing model, anonymous fleet benchmarking, governance, crew access,
      retention/deletion, cross-co-op boundaries, non-member protections, AI/ML rights,
      commercial use, tide/current data, and technical requirements (21 revisions)
- [x] Co-op charter template (`docs/co-op-charter-template.md`) — fillable template for
      individual co-ops to define mission, membership, governance, active agreements, and
      fleet-specific rules
- [x] Community contribution infrastructure — CONTRIBUTING.md, issue/PR templates,
      GitHub Actions CI for forks, Code of Conduct (#174)

### Federation — Phases 1 & 2
- [x] Ed25519 boat identity (keypair generation, boat cards, fingerprints); stored in
      `~/.helmlog/identity/`; CLI: `helmlog identity init/show`
- [x] Co-op charter and membership records with cryptographic signatures;
      CLI: `helmlog co-op create/status/invite`
- [x] Session sharing with embargo support — per-session co-op assignment,
      embargo timestamps, event-name scoping
- [x] Schema v28 — 6 new federation tables: `boat_identity`, `co_op_memberships`,
      `session_sharing`, `co_op_peers`, `co_op_audit`, `request_nonces`
- [x] Peer API (`peer_api.py`) — `/peer/identity`, `/peer/sessions`,
      `/peer/sessions/{id}/track`, `/peer/sessions/{id}/results`
- [x] Request authentication (`peer_auth.py`) — Ed25519 signing middleware,
      nonce replay protection, clock-skew tolerance
- [x] Peer client (`peer_client.py`) — async HTTP client for querying peers
- [x] Audit logging — `co_op_audit` table with volume-based rate-limit detection
- [x] Data licensing field allowlist enforced on all peer API endpoints
- [x] Integration test suite — 32 in-process tests (Layer 1):
      `test_federation_e2e.py`, `test_auth_e2e.py`, `test_embargo_e2e.py`,
      `test_data_license_e2e.py`
- [x] Docker compose integration test environment — two-container fleet simulation
      (Layer 3): `tests/integration/docker-compose.yml`

### Data policy compliance (#194–#211)
- [x] AIS data filtering — 23 AIS PGNs and SK paths blocked at ingestion
- [x] Self-vessel validation in SK reader (non-member vessel data excluded)
- [x] Soft delete (suppression) and hard delete (permanent purge)
- [x] "Boat X" anonymization with reversible mapping (30-day grace period)
- [x] Audio PII deletion (whole-recording deletion on crew request)
- [x] Processing offload audit trail (what was sent, where, when)
- [x] TLS warning for non-Tailscale transcription URLs
- [x] Auth required on all data-reading GET endpoints
- [x] Crew+ auth required for audio download/stream/transcript
- [x] Default video privacy to "private"
- [x] WiFi passwords masked in camera API responses
