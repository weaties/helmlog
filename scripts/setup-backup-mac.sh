#!/usr/bin/env bash
# setup-backup-mac.sh — Install the daily Pi-to-Mac backup launchd agent.
#
# Run once on your Mac from the project root:
#   ./scripts/setup-backup-mac.sh
#
# What it does:
#   1. Verifies SSH connectivity to the Pi (set PI=user@host to override)
#   2. Creates ~/backups/j105-logger/
#   3. Copies com.j105.backup.plist → ~/Library/LaunchAgents/ (with real paths)
#   4. Loads (enables) the agent — runs daily at 03:00
#
# To uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.j105.backup.plist
#   rm ~/Library/LaunchAgents/com.j105.backup.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"
PLIST_SRC="$SCRIPT_DIR/com.j105.backup.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.j105.backup.plist"
BACKUP_DEST="${BACKUP_DEST:-$HOME/backups/j105-logger}"
LOG_FILE="$BACKUP_DEST/backup.log"
PI="${PI:-weaties@corvopi}"

log() { echo "==> $*"; }
warn() { echo "    WARNING: $*" >&2; }

# ── 1. Pre-flight checks ──────────────────────────────────────────────────────
log "Checking dependencies..."

if ! command -v rsync &>/dev/null; then
  echo "ERROR: rsync not found. Install via Xcode CLT: xcode-select --install" >&2
  exit 1
fi

if ! command -v influx &>/dev/null; then
  warn "influx CLI not found — InfluxDB backup will be skipped."
  warn "Install with: brew install influxdb-cli"
fi

log "Testing SSH connectivity to $PI..."
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$PI" "echo ok" &>/dev/null; then
  echo "" >&2
  echo "ERROR: Cannot SSH to $PI." >&2
  echo "Make sure:" >&2
  echo "  1. Tailscale is running on both machines ('tailscale status')" >&2
  echo "  2. SSH key is installed on the Pi ('ssh-copy-id $PI')" >&2
  echo "  3. You can reach the Pi: ssh $PI" >&2
  exit 1
fi
log "  SSH OK"

# ── 2. Create backup destination ─────────────────────────────────────────────
mkdir -p "$BACKUP_DEST"
log "Backup destination: $BACKUP_DEST"

# ── 3. Install plist ──────────────────────────────────────────────────────────
log "Installing launchd agent..."

# Substitute real paths into the plist template
sed \
  -e "s|BACKUP_SCRIPT_PATH|$BACKUP_SCRIPT|g" \
  -e "s|BACKUP_LOG_PATH|$LOG_FILE|g" \
  -e "s|HOME_PATH|$HOME|g" \
  "$PLIST_SRC" > "$PLIST_DEST"

log "  Plist written → $PLIST_DEST"

# ── 4. Load the agent ────────────────────────────────────────────────────────
# Unload first in case it was previously installed with a different path
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
log "  Agent loaded (runs daily at 03:00)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Setup complete."
echo ""
echo "  Schedule : daily at 03:00 local time"
echo "  Log      : $LOG_FILE"
echo "  Snapshots: $BACKUP_DEST/<timestamp>/"
echo "  Retention: 10 snapshots (override with KEEP_SNAPSHOTS=N ./scripts/backup.sh)"
echo ""
echo "Run a backup now:"
echo "  $BACKUP_SCRIPT"
echo ""
echo "Or trigger via launchd:"
echo "  launchctl start com.j105.backup"
