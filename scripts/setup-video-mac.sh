#!/usr/bin/env bash
# setup-video-mac.sh — Install the Insta360 video pipeline launchd agent.
#
# Run once on your Mac from the project root:
#   ./scripts/setup-video-mac.sh
#
# What it does:
#   1. Checks Docker, exiftool, and uv are available
#   2. Creates ~/Videos/j105/ output directory
#   3. Verifies YouTube OAuth2 credentials exist (or prompts setup)
#   4. Copies com.j105.video.plist → ~/Library/LaunchAgents/ (with real paths)
#   5. Loads (enables) the agent — triggers on SD card mount
#
# To uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.j105.video.plist
#   rm ~/Library/LaunchAgents/com.j105.video.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROCESS_SCRIPT="$SCRIPT_DIR/process-videos.sh"
PLIST_SRC="$SCRIPT_DIR/com.j105.video.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.j105.video.plist"
OUTPUT_DIR="${VIDEO_OUTPUT_DIR:-$HOME/Videos/j105}"
LOG_FILE="$OUTPUT_DIR/video-pipeline.log"

SECRETS="${YOUTUBE_CLIENT_SECRETS:-$HOME/.j105-youtube-client-secrets.json}"
TOKEN="${YOUTUBE_TOKEN_FILE:-$HOME/.j105-youtube-token.json}"

log() { echo "==> $*"; }
warn() { echo "    WARNING: $*" >&2; }

# ── 1. Dependency checks ───────────────────────────────────────────────────

log "Checking dependencies..."

if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker not found. Install Docker Desktop: https://docker.com/get-started" >&2
  exit 1
fi
log "  Docker: $(docker --version | head -1)"

if ! docker info &>/dev/null 2>&1; then
  echo "ERROR: Docker is not running. Start Docker Desktop and try again." >&2
  exit 1
fi
log "  Docker daemon: running"

if command -v exiftool &>/dev/null; then
  log "  exiftool: $(exiftool -ver) (also bundled in Docker image)"
else
  log "  exiftool: not installed (OK — bundled in Docker image)"
fi

if ! command -v uv &>/dev/null; then
  echo "ERROR: uv not found. Install from: https://docs.astral.sh/uv/" >&2
  exit 1
fi

# ── 2. Create output directory ─────────────────────────────────────────────

mkdir -p "$OUTPUT_DIR"
log "Video output dir: $OUTPUT_DIR"

# ── 3. Check Docker image ──────────────────────────────────────────────────

IMAGE="${DOCKER_IMAGE:-insta360-cli-utils}"
BUILD_SCRIPT="$SCRIPT_DIR/../docker/build.sh"
if docker image inspect "$IMAGE" &>/dev/null; then
  log "  Docker image '$IMAGE': available"
else
  log "  Docker image '$IMAGE': not found — building (first time only)..."
  if [ -x "$BUILD_SCRIPT" ]; then
    # Check if MediaSDK .deb is available for enhanced stitching
    DOCKER_DIR="$(cd "$SCRIPT_DIR/../docker" && pwd)"
    DEB_COUNT=$(find "$DOCKER_DIR" -maxdepth 1 -name 'libMediaSDK-dev*.deb' 2>/dev/null | wc -l | tr -d ' ')
    if [ "$DEB_COUNT" -gt 0 ]; then
      log "  MediaSDK .deb found — building with full stitching support"
      "$BUILD_SCRIPT" --mediasdk
    else
      log "  No MediaSDK .deb — building with ffmpeg fallback"
      log "  For proper 360° stitching, get the SDK at https://www.insta360.com/sdk/apply"
      "$BUILD_SCRIPT"
    fi
  else
    warn "Build script not found at $BUILD_SCRIPT"
    warn "Run: cd $(dirname "$BUILD_SCRIPT") && ./build.sh"
  fi
fi

# ── 4. Check YouTube credentials ───────────────────────────────────────────

log "Checking YouTube credentials..."

if [ ! -f "$SECRETS" ]; then
  echo "" >&2
  echo "ERROR: YouTube client secrets not found at: $SECRETS" >&2
  echo "" >&2
  echo "Setup steps:" >&2
  echo "  1. Go to https://console.cloud.google.com/apis/credentials" >&2
  echo "  2. Create an OAuth 2.0 Client ID (Desktop app type)" >&2
  echo "  3. Download the JSON and save it as: $SECRETS" >&2
  echo "  4. Enable the YouTube Data API v3 for your project" >&2
  echo "  5. Set the OAuth consent screen to 'Production' mode" >&2
  echo "     (so refresh tokens don't expire after 7 days)" >&2
  echo "" >&2
  exit 1
fi
log "  Client secrets: $SECRETS"

if [ -f "$TOKEN" ]; then
  log "  OAuth token: $TOKEN (cached — will refresh automatically)"
else
  log "  OAuth token: not yet created"
  log "  First upload will open a browser for authorization."
fi

# ── 5. Install plist ───────────────────────────────────────────────────────

log "Installing launchd agent..."

sed \
  -e "s|PROCESS_SCRIPT_PATH|$PROCESS_SCRIPT|g" \
  -e "s|VIDEO_LOG_PATH|$LOG_FILE|g" \
  -e "s|HOME_PATH|$HOME|g" \
  "$PLIST_SRC" > "$PLIST_DEST"

log "  Plist written → $PLIST_DEST"

# ── 6. Load the agent ─────────────────────────────────────────────────────

launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
log "  Agent loaded (watches /Volumes for SD card mounts)"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "Setup complete."
echo ""
echo "  Trigger   : mount Insta360 X4 SD card (or any volume with DCIM/Camera01/)"
echo "  Log       : $LOG_FILE"
echo "  Output    : $OUTPUT_DIR/"
echo ""
echo "Run manually:"
echo "  $PROCESS_SCRIPT"
echo ""
echo "Or trigger via launchd:"
echo "  launchctl start com.j105.video"
