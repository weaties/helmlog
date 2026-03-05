# Insta360 X4 Video Pipeline

Automated pipeline that processes Insta360 X4 recordings into 360° YouTube
videos linked to J105 Logger race/practice sessions.

## Overview

```
SD card inserted into Mac
        │
        ▼
  launchd detects /Volumes change
        │
        ▼
  macOS dialog: "Process race videos?"
        │ Yes
        ▼
  Discover .insv files → group by recording
        │
        ▼
  Stitch via Docker + insta360-cli-utils
        │
        ▼
  Inject 360° spatial metadata (exiftool)
        │
        ▼
  Upload to YouTube (unlisted)
        │
        ▼
  Match to race/practice session (Pi API)
        │
        ▼
  Link video in J105 Logger
```

## Prerequisites

Install on your Mac:

```bash
# Docker Desktop (for stitching)
# Download from https://docker.com/get-started

# uv (Python package manager — already installed if you develop j105-logger)

# exiftool is optional on the host (bundled in the Docker image)
# brew install exiftool
```

## YouTube API Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a project (or use an existing one)
3. Enable the **YouTube Data API v3**
4. Create an **OAuth 2.0 Client ID** (type: Desktop app)
5. Download the JSON and save as `~/.j105-youtube-client-secrets.json`
6. **Important**: Set the OAuth consent screen to **Production** mode so refresh
   tokens don't expire after 7 days

First upload will open a browser for authorization. After that, the token is
cached and refreshes automatically.

## Building the Docker Image

The setup script builds the Docker image automatically on first run. You can
also build it manually:

```bash
# Default: ffmpeg fallback (stream-copy, dual-fisheye — works today)
./docker/build.sh

# With Insta360 MediaSDK (proper 360° stitching + FlowState)
# 1. Apply for SDK at https://www.insta360.com/sdk/apply (~3 day approval)
# 2. Download the Linux SDK and extract the .deb file
# 3. Place libMediaSDK-dev-*.deb in docker/
# 4. Build:
./docker/build.sh --mediasdk
```

Both modes tag the image as `insta360-cli-utils`, so the pipeline works without
config changes. The ffmpeg fallback produces a dual-fisheye MP4 (not properly
stitched as equirectangular) — good for testing the pipeline end-to-end. Rebuild
with `--mediasdk` once your SDK access is approved for proper 360° output.

### Upgrading to MediaSDK

When your Insta360 SDK application is approved:

1. Download the Linux SDK zip from the link in the approval email
2. Extract `libMediaSDK-dev-*.deb` from the zip
3. Copy it into `docker/`
4. Rebuild: `./docker/build.sh --mediasdk`
5. Re-process any videos that were converted with the ffmpeg fallback

## Installation

```bash
cd ~/src/j105-logger  # or wherever your clone is
./scripts/setup-video-mac.sh
```

This will:
- Verify Docker, exiftool, and uv are available
- Create `~/Videos/j105/` for output files
- Check YouTube credentials
- Install the launchd agent to watch for SD card mounts

## Usage

### Automatic (recommended)

Insert the Insta360 X4 SD card. A macOS dialog will ask if you want to
process the videos. Click **Process**.

### Manual

```bash
# Auto-detect SD card
./scripts/process-videos.sh

# Explicit mount point
./scripts/process-videos.sh /Volumes/Insta360\ X4
```

## Configuration

All via environment variables (or set in `~/.zshrc`):

| Variable | Default | Description |
|----------|---------|-------------|
| `VIDEO_OUTPUT_DIR` | `~/Videos/j105` | Where stitched MP4s are saved |
| `VIDEO_RESOLUTION` | `3840x1920` | Output resolution (4K) |
| `DOCKER_IMAGE` | `insta360-cli-utils` | Docker image for stitching |
| `PI_API_URL` | `http://corvopi:3002` | J105 Logger API on the Pi |
| `VIDEO_PRIVACY` | `unlisted` | YouTube privacy (private/unlisted/public) |
| `TIMEZONE` | `America/Los_Angeles` | Camera's local timezone |
| `YOUTUBE_CLIENT_SECRETS` | `~/.j105-youtube-client-secrets.json` | OAuth2 client secrets |
| `YOUTUBE_TOKEN_FILE` | `~/.j105-youtube-token.json` | Cached OAuth2 token |
| `PI_SESSION_COOKIE` | *(none)* | Session cookie for auto-linking videos (see below) |

## How Videos Get Linked to Sessions

After uploading to YouTube, the pipeline connects back to the J105 Logger on
the Pi to automatically link each video to the matching race or practice session:

1. **Fetch sessions** — `GET /api/sessions` retrieves recent sessions from the Pi
   (no auth required for reading).
2. **Match by timestamp** — Each recording's start time (from the Insta360
   filename) is compared against session start/end times. The session with the
   most time overlap is selected.
3. **Build rich metadata** — If matched, the YouTube title includes the event
   name, session type, and race number (e.g. "Ballard Cup Race 2 — 2026-08-10").
4. **Link on the Pi** — `POST /api/sessions/{id}/videos` creates the link so
   the video appears in the session's history page with sync-point data.

### Setting up the session cookie

The video linking endpoint requires `crew`-level authentication. To enable
auto-linking, you need to provide a session cookie:

1. Log into J105 Logger in your browser at `http://corvopi:3002`
2. Open developer tools → Application → Cookies
3. Copy the value of the `session` cookie
4. Set the environment variable:
   ```bash
   export PI_SESSION_COOKIE="<paste cookie value>"
   ```

The session cookie is valid for 90 days (configurable via `AUTH_SESSION_TTL_DAYS`).
If the cookie expires, the pipeline still uploads to YouTube — it just skips
the auto-linking step and prints a warning.

Without `PI_SESSION_COOKIE`, videos are uploaded to YouTube but not linked.
You can still link them manually from the session history page.

## SD Card File Structure

The Insta360 X4 stores recordings in:

```
DCIM/Camera01/
  VID_20260810_140530_00_000.insv   (back lens, segment 0)
  VID_20260810_140530_10_000.insv   (front lens, segment 0)
  VID_20260810_140530_00_001.insv   (back lens, segment 1)
  LRV_20260810_140530_01_000.mp4    (low-res preview)
```

The pipeline groups files by timestamp and only uses back-lens (`_00_`) files —
the stitcher pairs front+back automatically.

## Logs

```bash
# View pipeline logs
tail -f ~/Videos/j105/video-pipeline.log

# Trigger manually via launchd
launchctl start com.j105.video

# Check agent status
launchctl list com.j105.video
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.j105.video.plist
rm ~/Library/LaunchAgents/com.j105.video.plist
```

## Troubleshooting

**"Docker is not running"** — Start Docker Desktop.

**"No Insta360 SD card detected"** — Ensure the card is mounted and has
`DCIM/Camera01/VID_*.insv` files.

**YouTube upload fails with 403** — Your API quota may be exhausted (default:
~6 uploads/day). Check [Google Cloud Console](https://console.cloud.google.com/apis/dashboard).

**Video not recognized as 360° on YouTube** — The Docker image injects spatial
metadata automatically. If using the ffmpeg fallback, the output is dual-fisheye
(not equirectangular) so YouTube can't render it as 360°. Rebuild with
`./docker/build.sh --mediasdk` for proper 360° output.

**"Stitcher: ffmpeg (stream-copy fallback)"** — The Docker image was built
without MediaSDK. Videos are converted but not properly stitched. Apply for
the SDK at https://www.insta360.com/sdk/apply and rebuild.

**Refresh token expired** — Set your Google Cloud project's OAuth consent screen
to Production mode (not Testing). Testing mode tokens expire after 7 days.

**"Skipping link — set PI_SESSION_COOKIE to enable"** — The pipeline uploaded
to YouTube but couldn't link the video to a session. Set `PI_SESSION_COOKIE`
(see "Setting up the session cookie" above).

**"Warning: link failed (HTTP 401)"** — Your session cookie has expired. Log
into J105 Logger again and copy a fresh cookie.

**"No matching session found"** — The recording timestamps didn't overlap with
any session. The video is still uploaded; link it manually from the history page.
