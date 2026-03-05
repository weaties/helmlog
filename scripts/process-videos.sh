#!/usr/bin/env bash
# process-videos.sh — Automated Insta360 X4 video pipeline.
#
# Discovers .insv files on a mounted SD card, stitches them into
# equirectangular 360° MP4 via Docker + insta360-cli-utils, injects
# spatial metadata, uploads to YouTube, and links to J105 Logger sessions.
#
# Usage:
#   ./scripts/process-videos.sh                    # auto-detect SD card
#   ./scripts/process-videos.sh /Volumes/MyCard    # explicit mount point
#
# Called by com.j105.video.plist (launchd) when a volume is mounted,
# or run manually after inserting the SD card.
#
# Environment overrides:
#   VIDEO_OUTPUT_DIR          where stitched MP4s go  (default: ~/Videos/j105)
#   VIDEO_RESOLUTION          output resolution       (default: 3840x1920)
#   DOCKER_IMAGE              stitcher image           (default: insta360-cli-utils)
#   PI_API_URL                J105 Logger API          (default: http://corvopi:3002)
#   VIDEO_PRIVACY             YouTube privacy          (default: unlisted)
#   TIMEZONE                  camera local timezone    (default: America/Los_Angeles)
#   YOUTUBE_CLIENT_SECRETS    OAuth2 client secrets    (default: ~/.j105-youtube-client-secrets.json)
#   YOUTUBE_TOKEN_FILE        OAuth2 token cache       (default: ~/.j105-youtube-token.json)
#   PI_SESSION_COOKIE         session cookie for Pi API (enables auto-linking videos to sessions)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Configuration ────────────────────────────────────────────────────────────

OUTPUT_DIR="${VIDEO_OUTPUT_DIR:-$HOME/Videos/j105}"
RESOLUTION="${VIDEO_RESOLUTION:-3840x1920}"
IMAGE="${DOCKER_IMAGE:-insta360-cli-utils}"
PI_API="${PI_API_URL:-http://corvopi:3002}"
PRIVACY="${VIDEO_PRIVACY:-unlisted}"
TZ_NAME="${TIMEZONE:-America/Los_Angeles}"

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }
warn() { echo "[$(date -u +%H:%M:%SZ)] WARNING: $*" >&2; }
die() { echo "[$(date -u +%H:%M:%SZ)] ERROR: $*" >&2; exit 1; }

# ── 1. Locate the Insta360 SD card ──────────────────────────────────────────

find_insta360_mount() {
  # Explicit argument takes priority
  if [ -n "${1:-}" ] && [ -d "$1/DCIM/Camera01" ]; then
    echo "$1"
    return 0
  fi
  # Auto-detect: look for Insta360 volume in /Volumes
  for vol in /Volumes/*; do
    if [ -d "$vol/DCIM/Camera01" ]; then
      # Verify it has .insv files (not just any camera)
      if ls "$vol"/DCIM/Camera01/VID_*.insv &>/dev/null; then
        echo "$vol"
        return 0
      fi
    fi
  done
  return 1
}

SD_MOUNT=$(find_insta360_mount "${1:-}") || {
  log "No Insta360 SD card detected — nothing to do."
  exit 0
}

log "Insta360 SD card found at: $SD_MOUNT"

# ── 2. Confirmation dialog (only when triggered by launchd) ─────────────────

# If stdin is not a terminal (launchd trigger), show a macOS dialog
if ! [ -t 0 ]; then
  RESPONSE=$(osascript -e "
    display dialog \"Insta360 X4 detected at $SD_MOUNT.\" & return & return & \
    \"Process and upload race videos?\" \
    buttons {\"Cancel\", \"Process\"} default button \"Process\" \
    with title \"J105 Video Pipeline\"
  " 2>/dev/null) || {
    log "User cancelled — exiting."
    exit 0
  }
fi

# ── 3. Discover recordings ──────────────────────────────────────────────────

log "Discovering recordings..."
RECORDINGS=$(cd "$PROJECT_DIR" && uv run python -c "
import json
from pathlib import Path
from logger.insta360 import discover_recordings
recs = discover_recordings(Path('$SD_MOUNT'))
print(json.dumps([
    {'timestamp': r.timestamp_str, 'segments': [str(s) for s in r.segments], 'size': r.total_size_bytes}
    for r in recs
]))
")

REC_COUNT=$(echo "$RECORDINGS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$REC_COUNT" -eq 0 ]; then
  log "No recordings found on SD card."
  exit 0
fi

log "Found $REC_COUNT recording(s)"

# ── 4. Stitch each recording ────────────────────────────────────────────────

mkdir -p "$OUTPUT_DIR"

# Clean up any stale pending-uploads from a previous interrupted run
rm -f "$OUTPUT_DIR/.pending_uploads"

# Check Docker
if ! docker info &>/dev/null; then
  die "Docker is not running. Start Docker Desktop and try again."
fi

echo "$RECORDINGS" | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    segs = ' '.join(r['segments'])
    print(f\"{r['timestamp']}|{segs}\")
" | while IFS='|' read -r TS SEGS; do
  OUTPUT_FILE="$OUTPUT_DIR/${TS}.mp4"

  if [ -f "$OUTPUT_FILE" ]; then
    log "  [$TS] Already stitched → $OUTPUT_FILE (skipping)"
  else
    log "  [$TS] Stitching..."

    # Build Docker volume mounts for all segment directories
    CAMERA_DIR="$SD_MOUNT/DCIM/Camera01"

    # Map the segment filenames to container paths
    INPUT_ARGS=""
    for seg in $SEGS; do
      BASENAME=$(basename "$seg")
      INPUT_ARGS="$INPUT_ARGS /input/$BASENAME"
    done

    # stitch-360 handles stitching + 360° metadata injection inside the container
    docker run --rm \
      -v "$CAMERA_DIR:/input:ro" \
      -v "$OUTPUT_DIR:/output" \
      "$IMAGE" \
      --output "/output/${TS}.mp4" $INPUT_ARGS \
    || {
      warn "[$TS] Stitching failed — skipping this recording"
      continue
    }

    log "  [$TS] Stitched → $OUTPUT_FILE"
  fi

  echo "$TS $OUTPUT_FILE" >> "$OUTPUT_DIR/.pending_uploads"
done

# ── 5. Upload to YouTube + link to sessions ─────────────────────────────────

if [ ! -f "$OUTPUT_DIR/.pending_uploads" ]; then
  log "No new videos to upload."
  exit 0
fi

log "Uploading to YouTube and linking to sessions..."

cd "$PROJECT_DIR"
uv run python -c "
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from logger.insta360 import InstaRecording
from logger.pipeline import PipelineConfig, fetch_sessions_from_pi, process_recording

async def main():
    cfg = PipelineConfig(
        pi_api_url=os.environ.get('PI_API_URL', 'http://corvopi:3002'),
        pi_session_cookie=os.environ.get('PI_SESSION_COOKIE', ''),
        privacy=os.environ.get('VIDEO_PRIVACY', 'unlisted'),
        timezone=os.environ.get('TIMEZONE', 'America/Los_Angeles'),
    )

    # Fetch sessions from the Pi for matching
    sessions = await fetch_sessions_from_pi(cfg.pi_api_url)
    print(f'  Fetched {len(sessions)} sessions from Pi')

    results = []
    with open('$OUTPUT_DIR/.pending_uploads') as f:
        for line in f:
            ts, filepath = line.strip().split(' ', 1)
            filepath = Path(filepath)
            if not filepath.exists():
                print(f'  [{ts}] File not found: {filepath}', file=sys.stderr)
                continue

            rec = InstaRecording(timestamp_str=ts, segments=[], total_size_bytes=0)
            result = await process_recording(
                rec=rec,
                video_path=filepath,
                sessions=sessions,
                config=cfg,
            )

            entry = {
                'timestamp': ts,
                'video_id': result.video_id,
                'youtube_url': result.youtube_url,
                'session_id': result.session_id,
                'linked': result.linked,
                'uploaded': result.uploaded,
            }
            if result.uploaded:
                print(f'  [{ts}] Uploaded → {result.youtube_url}')
                if result.linked:
                    print(f'  [{ts}] Linked to session {result.session_id}')
                elif result.session_id and not cfg.pi_session_cookie:
                    print(f'  [{ts}] Skipping link — set PI_SESSION_COOKIE', file=sys.stderr)
            else:
                print(f'  [{ts}] Failed: {result.error}', file=sys.stderr)
            results.append(entry)

    uploaded = [r for r in results if r['uploaded']]
    if uploaded:
        with open('$OUTPUT_DIR/.upload_results.json', 'w') as f:
            json.dump(uploaded, f, indent=2)
        linked = sum(1 for r in uploaded if r.get('linked'))
        print(f'  {len(uploaded)} video(s) uploaded, {linked} linked to sessions')

asyncio.run(main())
"

# Clean up pending file
rm -f "$OUTPUT_DIR/.pending_uploads"

# ── 6. Summary ──────────────────────────────────────────────────────────────

if [ -f "$OUTPUT_DIR/.upload_results.json" ]; then
  UPLOAD_COUNT=$(python3 -c "import json; print(len(json.load(open('$OUTPUT_DIR/.upload_results.json'))))")
  log "Pipeline complete — $UPLOAD_COUNT video(s) processed"

  # Show a macOS notification if not running in a terminal
  if ! [ -t 0 ]; then
    osascript -e "display notification \"$UPLOAD_COUNT video(s) uploaded to YouTube\" with title \"J105 Video Pipeline\" sound name \"Glass\"" 2>/dev/null || true
  fi
else
  log "Pipeline complete — no videos uploaded"
fi
