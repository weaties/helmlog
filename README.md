# J105 Logger

NMEA 2000 data logger for J105 sailboat racing performance analysis. Runs on a
Raspberry Pi with a CAN bus HAT connected to the B&G instrument network. Signal K
Server decodes the NMEA 2000 bus and feeds both InfluxDB → Grafana (real-time
dashboards) and j105-logger (SQLite → CSV/GPX/JSON for regatta analysis tools).

Two Signal K plugins are required:
- **signalk-to-influxdb2** — forwards all SK data to InfluxDB for Grafana dashboards
- **signalk-derived-data** — computes true wind (TWS/TWA/TWD) from apparent wind + boatspeed + heading; without this, true wind fields will be empty in the logger and exports

---

## Architecture

```
CAN Bus (can0)
    │
    ▼
Signal K Server          ← owns can0, decodes NMEA 2000 via canboatjs
    ├──► InfluxDB 2.7.11 ← via signalk-to-influxdb2 plugin
    │        └──► Grafana  ← real-time dashboards, port 3001
    └──► WebSocket ws://localhost:3000/signalk/v1/stream
              │
              ▼
         j105-logger (sk_reader.py)
              │
              ▼
         SQLite (storage.py)
              │
              ▼
         export.py  →  CSV / GPX / JSON  →  Sailmon, regatta tools
```

Service dependency chain:
```
can-interface.service  →  signalk.service  →  j105-logger.service
influxd.service        (independent, starts at boot)
grafana-server.service (independent, starts at boot)
```

---

## Table of Contents

1. [Environments & DNS](#environments--dns)
2. [Daily use](#daily-use)
3. [Web interfaces](#web-interfaces)
4. [Race marking](#race-marking)
5. [Sail tracking](#sail-tracking)
6. [Linking YouTube videos](#linking-youtube-videos)
7. [External data — weather and tides](#external-data--weather-and-tides)
8. [Recording audio commentary](#recording-audio-commentary)
9. [Audio transcription](#audio-transcription)
10. [System health monitoring](#system-health-monitoring)
11. [Mac development](#mac-development)
12. [Fresh SD card setup](#fresh-sd-card-setup)
13. [Updating / deploying](#updating--deploying)
14. [Configuration](#configuration)
15. [Troubleshooting](#troubleshooting)

---

## Environments & DNS

The project uses the **`saillog.io`** domain (registered on Cloudflare) with a
`{hostname}.{environment}.saillog.io` naming pattern. The hostname is the boat
name, so this scales to multiple boats in the future.

| Environment | URL | Host | Branch | Deploys |
|---|---|---|---|---|
| **Live** | `corvo.live.saillog.io` | Pi 4 8GB (`corvopi`) | `main` | Auto on promotion push |
| **Stage** | `corvo.stage.saillog.io` | Pi 5 8GB (test Pi) | `stage` | Auto on `stage` branch push |
| **Test** | `corvo.test.saillog.io` | Pi 5 8GB (test Pi) | `main` | Auto on merge to `main` |
| **PR Preview** | `corvo.test-pr{N}.saillog.io` | Pi 5 8GB (test Pi) | PR branch | Auto on PR open/update |
| **Dev** | `localhost:3002` | Developer's Mac | feature branch | Manual |

### Promotion flow

```
Developer Mac (dev)
  └─ PR opened ──────────→ corvo.test-pr{N}.saillog.io  (ephemeral, auto-deployed)
  └─ PR merged to main ──→ corvo.test.saillog.io        (auto-deployed)
  └─ promote stage ───────→ corvo.stage.saillog.io       (fast-forward main → stage, auto-deployed)
  └─ promote live ────────→ corvo.live.saillog.io        (fast-forward stage → live, auto-deployed)
```

Promotions are fast-forward merges only — no merge commits, no surprises.
Every promotion creates a timestamped git tag (`live/2026-03-02T14.30.00Z` or
`stage/2026-03-02T14.30.00Z`) forming a complete deployment audit trail.

```bash
# Promote main → stage
j105-logger promote stage

# Promote stage → live
j105-logger promote live

# Roll back live to the previous deployment
j105-logger promote live --rollback

# View deployment history
git tag -l 'live/*' --sort=-creatordate
```

### Ingress

Public access uses **Cloudflare Tunnel** with **Cloudflare Access** (email OTP)
protecting all environments. Tailscale is retained for private SSH access to
both Pis. See [issue #125](https://github.com/weaties/j105-logger/issues/125)
for the full implementation plan.

---

## Daily use

> These commands assume the Pi is already set up. SSH in via Tailscale
> (`ssh weaties@corvopi`) and run from the project directory.

### Check what's in the database

```bash
j105-logger status
```

```
Table                    Rows  Last seen
-------------------------------------------------------
headings                 1823  2025-08-10T14:32:05.123+00:00
speeds                   1821  2025-08-10T14:32:05.089+00:00
winds                    1820  2025-08-10T14:32:04.997+00:00
...
```

### Start logging (manual / foreground)

```bash
j105-logger run
```

Press `Ctrl-C` to stop. All buffered data is flushed before exit.

### Export to CSV, GPX, or JSON

```bash
j105-logger export \
  --start "2025-08-10T13:00:00" \
  --end   "2025-08-10T15:30:00" \
  --out   data/race1.csv
```

The format is inferred from the file extension:

| Extension | Format | Best for |
|---|---|---|
| `.csv` | Comma-separated values | Spreadsheets, Sailmon, custom analysis |
| `.gpx` | GPX 1.1 XML track | Navigation apps, course replay tools |
| `.json` | Structured JSON | Custom scripts, programmatic analysis |

```bash
# GPX — only seconds with GPS position produce a <trkpt>
j105-logger export --start "2025-08-10T13:00:00" --end "2025-08-10T15:30:00" \
  --out data/race1.gpx

# JSON — numeric values are typed (null instead of empty string for missing data)
j105-logger export --start "2025-08-10T13:00:00" --end "2025-08-10T15:30:00" \
  --out data/race1.json
```

Timestamps are UTC ISO 8601. The output CSV has one row per second with columns:

| Column | Description |
|---|---|
| `timestamp` | UTC ISO 8601 |
| `HDG` | Heading (degrees true) |
| `BSP` | Boatspeed through water (knots) |
| `DEPTH` | Water depth (metres) |
| `LAT` / `LON` | GPS position (decimal degrees) |
| `COG` / `SOG` | Course and speed over ground |
| `TWS` / `TWA` | True wind speed (kts) and angle (°) |
| `AWA` / `AWS` | Apparent wind angle (°) and speed (kts) |
| `WTEMP` | Water temperature (°C) |
| `video_url` | YouTube deep-link for that second (empty if no video linked) |
| `WX_TWS` / `WX_TWD` | Synoptic wind speed (kts) and direction (°) from Open-Meteo |
| `AIR_TEMP` | Air temperature (°C) from Open-Meteo |
| `PRESSURE` | Surface pressure (hPa) from Open-Meteo |
| `TIDE_HT` | Tide height above MLLW (metres) from NOAA CO-OPS |

Weather and tide columns are hourly resolution — all seconds within the same
hour share the same value. They are empty if the Pi had no internet or GPS
lock when the session was logged.

### Manage the background service

The logger runs automatically as a systemd service when the Pi boots on the boat.

```bash
# Check status
sudo systemctl status j105-logger

# View live logs
sudo journalctl -fu j105-logger

# Stop / start / restart
sudo systemctl stop    j105-logger
sudo systemctl start   j105-logger
sudo systemctl restart j105-logger
```

The service depends on `signalk.service`, which in turn depends on
`can-interface.service`. All three start automatically at boot.

### CAN bus health check

```bash
# Should show frames streaming in — if blank, check the physical connection
candump can0

# Interface details (state should be ERROR-ACTIVE when bus is healthy)
ip -details link show can0
```

---

## Web interfaces

All interfaces are available locally over Tailscale and — after `setup.sh` configures
Tailscale Funnel — publicly via `https://corvopi.<tailnet>.ts.net`:

| Interface | Local URL | Public URL (Funnel) | Purpose |
|---|---|---|---|
| j105-logger | `http://corvopi:3002` | `https://corvopi.<tailnet>.ts.net/` | Race marker, history, exports |
| Grafana | `http://corvopi:3001` | `https://corvopi.<tailnet>.ts.net/grafana/` | Real-time sailing dashboards |
| Signal K | `http://corvopi:3000` | `https://corvopi.<tailnet>.ts.net/signalk/` | NMEA 2000 data explorer, plugin management |
| InfluxDB | `http://corvopi:8086` | — (not exposed) | Time-series data explorer, query UI |

`setup.sh` configures all three public routes automatically when Tailscale is connected.
The public URL is written to `PUBLIC_URL` in `.env` so the webapp generates correct
Grafana deep-links.

Grafana default credentials: `admin` / `changeme123` — **change after first login**.
InfluxDB is bound to loopback only (127.0.0.1:8086) — access it via SSH tunnel or from the Pi directly.

---

## Race marking

The race-marker web page at `http://corvopi:3002` gives any crew device on
Tailscale a one-tap way to mark the start and end of each race. Race names tie
together instrument data, audio, and video for that window so exports can be
scoped to a specific race rather than a hand-entered time range.

### Opening the page

On any phone or tablet joined to your Tailscale network:
open `http://corvopi:3002` in a browser. Bookmark it for quick access at the
start line.

### Race naming

Race names follow the format `YYYYMMDD-{Event}-{N}` where N is the race number
for that UTC day (starting at 1).

| Day | Auto event |
|---|---|
| Monday | `BallardCup` |
| Wednesday | `CYC` |
| Any other | You are prompted to type an event name; it is saved and persists across logger restarts |

On Monday and Wednesday the event name is set automatically. On other days an
event name input appears above the race controls — type the event name and tap
**Save** before starting your first race.

### Starting and ending races

- **START RACE N** — opens a new race, auto-closes the previous one if it was
  still in progress, and begins the duration counter.
- **END RACE N** — closes the current race. The next Start will use N+1.

The page polls for updates every 10 seconds and ticks the duration counter
every second.

### Downloading race exports from the phone

Completed races in the "Today's races" list show **↓ CSV** and **↓ GPX**
buttons. Tapping either downloads that race's data directly to the phone.

### Security and access

The web app uses **magic-link authentication**. Before anyone can log in, an admin
must create user accounts from the Pi's command line:

```bash
# Create an admin account (first time — run from the Pi)
j105-logger add-user --email you@example.com --name "Your Name" --role admin

# Create crew accounts (viewer role — can mark races, can't manage users)
j105-logger add-user --email crew@example.com --name "Crew Member" --role viewer
```

Roles: `admin` (full access + user management), `crew` (race ops), `viewer` (read-only).

Once users exist, the admin can generate invite links from the **Admin** page
(`/admin`) so crew can log in on their own devices without needing SSH access.

To bypass auth entirely on a trusted LAN (e.g. local development), set
`AUTH_DISABLED=true` in `.env` and restart the service. **Never set this when
Tailscale Funnel is active** — the web app would be publicly accessible without login.

---

## Sail tracking

The **Boats** page (`http://corvopi:3002` → Boats tab) maintains a sail inventory
for the boat. Each sail has a type, name, and optional notes.

### Managing the sail inventory

Open the Boats page and use the **Add Sail** form to record each sail you own
(main, jib, spinnaker, etc.). Sails appear in a list and can be deleted when
retired.

### Recording sails per race

On the **History** page, each completed race card has a **Sails** panel. Select
the main and jib (and kite if used) from dropdown menus populated from your sail
inventory. Selections are saved immediately and appear in the race summary.

---

## Linking YouTube videos

If you record a race on video and upload it to YouTube, you can link it to
your instrument data. Once linked, every row in the exported CSV gets a
`video_url` column with a deep-link (`?t=<seconds>`) that jumps straight to
that moment in the video.

### How to find your sync point

You need one moment where you know both the **UTC time from the instrument
log** and **where that moment appears in the video** (seconds from the start).

A good sync point is the starting gun — it's visible on video and you can
find it in the log by looking for a sudden change in boatspeed or heading.

### Option A — you know when you pressed Record

If you noted the time when you started the camera, use `--start`:

```bash
j105-logger link-video \
  --url "https://youtu.be/YOUR_VIDEO_ID" \
  --start "2025-08-10T13:45:00"
```

This tells the system the video playback position at `T=0s` corresponds to
UTC `13:45:00`.

### Option B — sync on a known event (recommended)

This is more accurate. Pick any identifiable moment — the starting gun works
well — and note:

1. **Where it is in the video** — scrub to the moment in YouTube and read
   the time off the progress bar (e.g. `5:30` = 330 seconds)
2. **What UTC time it was** — look at your exported CSV for that event, or
   check `j105-logger status` to see timestamps and cross-reference with the
   log

```bash
j105-logger link-video \
  --url "https://youtu.be/YOUR_VIDEO_ID" \
  --sync-utc  "2025-08-10T14:05:30" \
  --sync-offset 330
```

The command fetches the video title and duration from YouTube and stores the
sync point. It prints a verification URL at the sync moment so you can
confirm the alignment is correct.

### List linked videos

```bash
j105-logger list-videos
```

```
Title                                      Duration  Sync UTC
--------------------------------------------------------------------------------
J105 Race — August 2025                      2:03:14  2025-08-10T14:05:30+00:00
  https://youtu.be/YOUR_VIDEO_ID
```

### Video links in the CSV export

Once a video is linked, run `export` as normal:

```bash
j105-logger export \
  --start "2025-08-10T13:00:00" \
  --end   "2025-08-10T15:30:00" \
  --out   data/race1.csv
```

The `video_url` column in the output will contain a clickable link for every
second that falls within the video's duration, and will be empty outside that
range. In Excel or Numbers, click the cell to jump directly to that moment.

---

## External data — weather and tides

When `j105-logger run` is active, two background tasks automatically fetch
external data and store it in the same SQLite database:

| Source | Data | Coverage |
|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | Wind speed, wind direction, air temperature, pressure | Global, free, no API key |
| [NOAA CO-OPS](https://tidesandcurrents.noaa.gov/) | Hourly tide height predictions (MLLW datum) | US coastal waters, free, no API key |

Both tasks start as soon as the Pi has a GPS lock and internet access:

- **Weather** is fetched once per hour for the current position.
- **Tides** are fetched once per day — today's and tomorrow's full 24-hour
  prediction set — from the nearest NOAA station to the boat's position.
  Re-fetching is idempotent, so restarting the logger never creates duplicates.

The data appears automatically as extra columns in the CSV export (`WX_TWS`,
`WX_TWD`, `AIR_TEMP`, `PRESSURE`, `TIDE_HT`). No configuration is needed.

> **Offline use**: If the Pi has no internet (e.g. at anchor without Wi-Fi),
> the external fetches fail silently. The logger continues normally and those
> CSV columns will be empty for that session.

---

## Recording audio commentary

When `j105-logger run` is active, it automatically records audio from the
first available USB input device (or the one matching `AUDIO_DEVICE` in `.env`).
This is designed for the **Gordik 2T1R** wireless lavalier system, whose USB
receiver appears as a standard UAC device — no drivers needed.

Audio is saved as a WAV file per session in `data/audio/`, named with the UTC
start timestamp so it lines up directly with the instrument log.

### Initial setup

1. Plug the Gordik USB receiver into any USB port on the Pi.
2. Find its device name:

   ```bash
   j105-logger list-devices
   ```

   ```
   Idx  Name                                      Ch    Default rate
   -----------------------------------------------------------------
     0  Built-in Microphone                        2           44100
     1  Gordik 2T1R USB Audio                      1           48000
   ```

3. Set `AUDIO_DEVICE` in `.env` to a substring of the name (case-insensitive):

   ```bash
   # In ~/j105-logger/.env:
   AUDIO_DEVICE=Gordik
   ```

   Or use the integer index (`AUDIO_DEVICE=1`). If `AUDIO_DEVICE` is not set,
   the first available input device is used automatically.

4. Restart the logger service:

   ```bash
   sudo systemctl restart j105-logger
   ```

   Confirm with:
   ```bash
   sudo journalctl -fu j105-logger | grep -i audio
   # Audio recording started: data/audio/audio_20250810_140530.wav
   ```

### List recorded audio sessions

```bash
j105-logger list-audio
```

```
File                                          Duration  Start UTC
--------------------------------------------------------------------------------
data/audio/audio_20250810_140530.wav             1:23:45  2025-08-10T14:05:30+00:00
```

### WAV file naming

Files are named `audio_YYYYMMDD_HHMMSS.wav` using the UTC start time, so they
can be matched to the instrument log by timestamp.

### Listening to recordings in the browser

The **History** page shows an inline audio player for each completed race that
has an associated recording. You can also download the WAV file directly from
the same card using the **↓ WAV** button.

### Graceful degradation

If no audio device is found at startup (e.g. Gordik receiver not plugged in),
the logger logs a warning and continues running normally — instrument data is
never interrupted by a missing audio device.

See `docs/audio-setup.md` for full details, including system dependency notes
and troubleshooting.

---

## Audio transcription

Completed audio recordings can be transcribed to text directly from the
**History** page. Transcription runs on the Pi using
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no cloud service
or internet connection required.

### Transcribing a recording

1. On the History page, open a race card that has an audio recording.
2. Click **📝 Transcript ▶**.
3. The button shows a spinner while the job runs. When done, the transcript text
   appears in the panel below.

Transcription is CPU-bound and takes roughly 0.5–1× real-time on a Pi 4
(i.e. a 60-minute race takes about 30–60 minutes). You can navigate away and
come back — the job continues in the background and the result is stored in
SQLite.

### Model selection

The default model is `base` (good accuracy, fast on Pi). You can choose a larger
model for better accuracy by setting `WHISPER_MODEL` in `.env`:

| Model | Speed on Pi 4 | Accuracy |
|---|---|---|
| `tiny` | Fastest | Lower |
| `base` | ~1× real-time | Good (default) |
| `small` | ~2× real-time | Better |
| `medium` | ~4× real-time | Best practical |

```bash
# In ~/j105-logger/.env:
WHISPER_MODEL=small
```

Restart the logger after changing the model. The model is downloaded on first
use and cached automatically.

### Speaker diarisation (who said what)

When a Hugging Face token is configured, transcription automatically labels
each segment with the speaker (`SPEAKER_00`, `SPEAKER_01`, …). The result is
displayed as colour-coded blocks on the History page.

Diarisation uses [pyannote.audio](https://github.com/pyannote/pyannote-audio)
(`pyannote/speaker-diarization-3.1`) running locally on the Pi — no audio is
sent to any cloud service.

#### 1. Create a free Hugging Face account

Go to [huggingface.co](https://huggingface.co) and sign up (or log in).

#### 2. Accept the model terms

You must accept the licence for both models before they can be downloaded:

1. [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) — click **Agree and access repository**
2. [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) — click **Agree and access repository**

Both require being logged in. The pages will show a licence gate the first
time you visit; once accepted, access is granted immediately.

#### 3. Generate a read token

1. Go to [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
2. Click **New token**.
3. Give it a name (e.g. `corvopi`) and set **Type** to **Read**.
4. Click **Generate a token** and copy the value — it starts with `hf_`.

Keep this token private. It grants read access to any public or gated model
your account has accepted terms for.

#### 4. Add the token to `.env` on the Pi

```bash
ssh weaties@corvopi
nano ~/j105-logger/.env
```

Add this line (replace with your actual token):

```
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx
```

Then restart the logger:

```bash
sudo systemctl restart j105-logger
```

The model weights (~1 GB) are downloaded and cached on the first transcription
that uses diarisation. Subsequent runs use the cached weights.

#### Performance

Diarisation adds roughly 2–3× real-time on a Pi 4 on top of the Whisper pass.
A 60-minute recording will take approximately 2–3 hours total. You can navigate
away and return — the job runs in the background and results are stored in
SQLite.

#### Disabling diarisation

Remove (or comment out) `HF_TOKEN` from `.env` and restart the logger.
Transcription will continue to work using the plain Whisper path.

### Limitations

- Accuracy degrades in high wind/engine noise environments.
- Transcripts are stored in the `transcripts` SQLite table and cannot yet be
  exported to CSV or PDF from the UI.

---

## System health monitoring

The logger automatically monitors the Pi's CPU, memory, disk usage, and
temperature, writing a `system_health` measurement to InfluxDB every 60 seconds.

The **home page** polls `/api/system-health` every 30 seconds and shows a
warning banner if:
- Disk usage exceeds **85 %**
- CPU temperature exceeds **75 °C**

No configuration is needed. If InfluxDB is not configured, the metric write
fails silently and only the web banner is active.

---

## Mac development

The full test suite runs on a Mac with no Pi, CAN bus, Signal K, InfluxDB, or
Grafana required. Hardware access is isolated to `can_reader.py` and `audio.py`,
both of which are mocked in tests.

### One-time setup

```bash
# System audio libraries (required by sounddevice / soundfile)
brew install portaudio libsndfile

# Install Python dependencies
uv sync

# Create a local .env
cp .env.example .env
```

You don't need Signal K or a CAN interface running locally. The only `.env`
values that matter for running tests are:

```
DB_PATH=data/logger.db
LOG_LEVEL=DEBUG
```

### Daily dev loop

```bash
# Run tests (no hardware required)
uv run pytest

# Run with coverage
uv run pytest --cov=src/logger

# Lint + type check before pushing
uv run ruff check . && uv run ruff format --check . && uv run mypy src/

# Auto-fix lint and formatting
uv run ruff check --fix . && uv run ruff format .
```

### PR workflow

1. Branch off `main`:
   ```bash
   git checkout main && git pull
   git checkout -b feature/my-feature
   ```
2. Develop and test locally until `uv run pytest` and lint/type checks pass.
3. Push and open a PR:
   ```bash
   git push -u origin feature/my-feature
   gh pr create
   ```
4. Merge when ready. The branch can then be deleted.

---

## Fresh SD card setup

This covers everything from a blank SD card to a fully running stack.

### 1. Flash the OS

Download and install **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)**.

- **OS**: Raspberry Pi OS Lite (64-bit) — "Other → Raspberry Pi OS (other)"
- **Storage**: your SD card or SSD

Click the gear icon (⚙) before writing to pre-configure:

| Setting | Value |
|---|---|
| Hostname | `corvopi` (or whatever you like) |
| Enable SSH | Yes — "Allow public-key authentication only" |
| SSH public key | paste your Mac's `~/.ssh/id_ed25519.pub` |
| Username | `weaties` |
| Password | set one (used for sudo) |
| Wi-Fi | your home network SSID/password |
| Locale | your timezone |

Write the card, insert it into the Pi, and power on.

### 2. First SSH in

Find the Pi on your local network and connect:

```bash
ssh weaties@corvopi.local
```

If `.local` doesn't resolve, check your router's DHCP table for the IP.

### 3. System update

```bash
sudo apt-get update && sudo apt-get upgrade -y
```

### 4. Add to Tailscale

Tailscale lets you SSH into the Pi from anywhere — marina, dock, home — without
port-forwarding or a static IP.

```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sudo bash

# Connect to your Tailscale network and enable Tailscale SSH
sudo tailscale up --ssh --accept-dns=false

# Follow the URL printed in the terminal to authenticate in your browser
```

`--ssh` enables Tailscale's built-in SSH (you won't need to manage authorized_keys).
`--accept-dns=false` prevents Tailscale from overriding the Pi's DNS, which can
cause issues on some networks.

After joining, approve the machine in the [Tailscale admin console](https://login.tailscale.com/admin/machines).
From then on, SSH from anywhere with:

```bash
ssh weaties@corvopi
```

Pin the Pi's Tailscale IP if you want a stable address — check it with
`tailscale ip -4`.

#### Keep Tailscale running across reboots

```bash
sudo systemctl enable --now tailscaled
```

This is done automatically by the installer, but worth confirming:

```bash
sudo systemctl status tailscaled
```

### 5. Configure the CAN HAT

The HAT uses an MCP2515 CAN controller connected over SPI with a 16 MHz crystal
and the interrupt line on GPIO 25. Add this to the Pi's boot config:

```bash
sudo nano /boot/firmware/config.txt
```

Add at the very end of the file (before any `[pi*]` section tags, or after `[all]`):

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

> **Note**: `spi=on` may already be present. Only add it once.

Save and reboot:

```bash
sudo reboot
```

After rebooting, confirm the CAN interface appeared:

```bash
ip link show can0
# Should show: can0: <NOARP,ECHO> ...
```

### 6. Clone the repository

```bash
cd ~
git clone https://github.com/weaties/j105-logger.git
cd j105-logger
```

### 7. Run the setup script

```bash
./scripts/setup.sh
```

This script is fully idempotent — safe to re-run after updates. It installs and
configures:

1. Node.js 24 LTS (via NodeSource)
2. Signal K Server + plugins (`signalk-to-influxdb2`, `@signalk/derived-data`)
3. InfluxDB 2.7.11 (pinned; loopback-only binding; `apt-mark hold` prevents v3 auto-upgrade)
4. Grafana OSS (loopback-only; login required; pre-provisioned InfluxDB datasource; port 3001)
5. `uv` and all Python dependencies
6. System audio libraries (`libportaudio2`, `libsndfile1`) for USB audio recording
7. `.env` config file from the template (chmod 600)
8. `data/` directory for SQLite, audio, and notes — owned by the `j105logger` service account
9. `j105logger` dedicated service account (UID ≈ 997; `nologin`; in `audio` + `netdev` groups)
10. `netdev` group membership for non-root CAN bus access
11. `can-interface.service` — brings up `can0` at boot
12. `signalk.service` — starts Signal K after CAN is up
13. `j105-logger.service` — starts logger as `j105logger` after Signal K is up
14. Signal K bcrypt admin password (saved to `~/.signalk-admin-pass.txt`)
15. Automatic security updates (`unattended-upgrades`)
16. Unused services masked (cups, avahi-daemon, bluetooth, etc.)
17. SSH hardened (X11Forwarding disabled; `~/.ssh` permissions tightened)
18. Scoped NOPASSWD sudo replacing the Pi OS blanket `NOPASSWD:ALL`

The InfluxDB admin token is saved to `~/influx-token.txt` (permissions 600).
If you ever lose it, retrieve it with:

```bash
influx auth list
```

### 8. Add `uv` to your PATH

`uv` is installed to `~/.local/bin/`. Add it permanently:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 9. Create your admin user

Before rebooting, create the first admin account for the race-marker web app:

```bash
j105-logger add-user --email you@example.com --name "Your Name" --role admin
```

This uses the SQLite DB directly — no running service needed. After this you can
log in at `http://corvopi:3002` and generate invite links for crew.

Also change the Grafana admin password from the default `changeme123`:

```bash
# Open in a browser and change the password via the UI
open http://corvopi:3001   # Mac
# or: xdg-open http://corvopi:3001   (Linux)
```

### 10. Reboot and verify

```bash
sudo reboot
```

After rebooting:

```bash
# All five should be active
sudo systemctl status can-interface signalk influxd grafana-server j105-logger

# Logger rows accumulating
j105-logger status

# Signal K dashboard (login with admin password from ~/.signalk-admin-pass.txt)
# Open http://corvopi:3000 in a browser

# Grafana dashboards (login required — admin / your-new-password)
# Open http://corvopi:3001 in a browser

# Race marker (login required — use the account created in step 9)
# Open http://corvopi:3002 in a browser
```

---

## Updating / deploying

### Current: manual deploy

After a PR merges to `main`, SSH into the Pi and run:

```bash
ssh weaties@corvopi
cd ~/j105-logger
./scripts/deploy.sh
```

This pulls `main`, syncs Python dependencies, re-applies Tailscale Funnel routes,
updates `PUBLIC_URL` in `.env`, and restarts the `j105-logger` service. Service
status is printed at the end for a quick sanity check.

All `sudo` commands in `deploy.sh` are in the scoped `/etc/sudoers.d/j105-logger-allowed`
file (configured by `setup.sh`), so no password prompt is needed during a normal deploy.

### Full update (new deps, systemd service file changes, or Signal K updates)

If systemd service files or apt packages changed, run the full idempotent setup instead:

```bash
cd ~/j105-logger
git pull
./scripts/setup.sh
sudo npm update -g signalk-server
sudo systemctl daemon-reload
sudo systemctl restart signalk j105-logger
```

---

## Configuration

Settings live in `~/j105-logger/.env`:

```bash
CAN_INTERFACE=can0      # SocketCAN interface name
CAN_BITRATE=250000      # NMEA 2000 standard bitrate
DB_PATH=data/logger.db  # SQLite database path (relative to project root)
LOG_LEVEL=INFO          # loguru log level: DEBUG, INFO, WARNING, ERROR
DATA_SOURCE=signalk     # signalk (default) or can (legacy direct CAN mode)
SK_HOST=localhost        # Signal K server hostname
SK_PORT=3000             # Signal K WebSocket port
# Audio recording (Gordik 2T1R or any USB Audio Class device)
# AUDIO_DEVICE=Gordik   # name substring or integer index; omit to auto-detect
AUDIO_DIR=data/audio    # directory for WAV files
AUDIO_SAMPLE_RATE=48000
AUDIO_CHANNELS=1
# Audio transcription
WHISPER_MODEL=base      # faster-whisper model: tiny, base, small, medium, large
# HF_TOKEN=hf_...      # Hugging Face token — enables speaker diarisation (optional)
# Photo notes
NOTES_DIR=data/notes    # directory where uploaded photo notes are stored
# Web interface (race marker)
WEB_HOST=0.0.0.0        # bind address
WEB_PORT=3002           # http://corvopi:3002 on Tailscale
# WEB_PIN=             # optional PIN (reserved, not yet implemented)
# Grafana deep-link buttons in the web UI
GRAFANA_URL=http://corvopi:3001
GRAFANA_DASHBOARD_UID=j105-sailing
# Authentication
# AUTH_DISABLED=true          # bypass auth entirely — only for local/LAN dev; NEVER with Tailscale Funnel
AUTH_SESSION_TTL_DAYS=90      # session cookie lifetime in days
# ADMIN_EMAIL=you@example.com # if set, this user is auto-created as admin on first startup
# Public URL — set by setup.sh/deploy.sh from Tailscale hostname; used for Grafana deep-links
# PUBLIC_URL=https://corvopi.<tailnet>.ts.net
# InfluxDB — required only for system health metrics; omit if not using InfluxDB
# INFLUX_URL=http://localhost:8086
# INFLUX_TOKEN=<token from ~/influx-token.txt>
# INFLUX_ORG=j105
# INFLUX_BUCKET=signalk
```

Edit with `nano ~/j105-logger/.env`. Changes take effect on the next
`sudo systemctl restart j105-logger`.

---

## Troubleshooting

### `j105-logger: command not found`

`uv` isn't in your PATH. Either:
```bash
export PATH="$HOME/.local/bin:$PATH"   # temporary
# or add it to ~/.bashrc permanently (see step 8 above)
```

Or use the full invocation:
```bash
~/.local/bin/uv run --project ~/j105-logger j105-logger status
```

### j105-logger can't connect to Signal K

The logger connects to Signal K's WebSocket at `ws://${SK_HOST}:${SK_PORT}/signalk/v1/stream`.

```bash
# Check Signal K is running
sudo systemctl status signalk

# Check Signal K logs
sudo journalctl -u signalk --no-pager -n 50

# Verify the WebSocket endpoint is up
curl -s http://localhost:3000/signalk/v1/api/ | python3 -m json.tool
```

If Signal K is running but the logger can't connect, check `SK_HOST` and
`SK_PORT` in `.env` match the Signal K server configuration.

### Signal K reports "socketcan stopped" (can-interface.service not running)

```bash
sudo systemctl status can-interface
sudo systemctl restart can-interface
sudo systemctl restart signalk
```

If `can-interface` fails, the CAN HAT likely isn't configured — see step 5
in the fresh SD card setup above.

### InfluxDB token lost

The token was saved at setup time to `~/influx-token.txt`:

```bash
cat ~/influx-token.txt
```

Or list all tokens via the CLI:

```bash
influx auth list
```

### `can0` interface missing after reboot

**Check 1 — `can-interface.service` not installed:**

If the setup script was never run (or failed partway through), the service that
brings up `can0` won't exist:

```bash
sudo systemctl status can-interface
```

If it shows "Unit can-interface.service could not be found", re-run setup:

```bash
./scripts/setup.sh
```

**Check 2 — `dtoverlay` line missing or malformed:**

The `dtoverlay` line in `/boot/firmware/config.txt` may be missing:

```bash
grep -n "mcp2515\|spi" /boot/firmware/config.txt
```

Expected output:
```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

### CAN bus stays in `ERROR-PASSIVE`

The Pi is not connected to an active NMEA 2000 bus (no other nodes to
acknowledge frames). This is normal at home. On the boat, this should
clear to `ERROR-ACTIVE` within seconds of the bus powering up.

### Logger service fails to start

```bash
sudo journalctl -u j105-logger --no-pager
```

Common causes:
- Signal K not running yet (check `signalk.service` status)
- `.env` file missing (re-run `./scripts/setup.sh`)
- `netdev` group not applied (reboot required after first setup)

### Permission denied on `can0`

Signal K owns the CAN bus — j105-logger never touches it directly (it reads
from the Signal K WebSocket). If you see this error, check that `DATA_SOURCE`
in `.env` is set to `signalk` (the default), not `can`.
