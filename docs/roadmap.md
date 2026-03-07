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

---

## Completed

### Core logging
- [x] NMEA 2000 PGN decoders for 7 standard PGNs (127250, 128259, 128267, 129025, 129026, 130306, 130310)
- [x] Signal K WebSocket reader (`sk_reader.py`) — primary data source
- [x] Legacy direct-CAN path (`can_reader.py`) available via `DATA_SOURCE=can`
- [x] SQLite storage with async writes and integer-versioned migrations (schema v16)
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

### Infrastructure
- [x] Raspberry Pi setup script (`scripts/setup.sh`) — idempotent, installs full stack
- [x] Deploy script (`scripts/deploy.sh`) — pull + sync deps + restart service
- [x] `can-interface.service` → `signalk.service` → `helmlog.service` dependency chain
- [x] Grafana provisioning (datasource + dashboards) via `scripts/provision-grafana.sh`
- [x] CAN HAT hardware setup & loopback testing on Pi (all 7 PGNs verified in loopback)
- [x] Full test suite (330+ tests — all modules covered)
- [x] ruff + mypy clean
