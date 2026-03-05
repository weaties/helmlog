#!/usr/bin/env bash
# backup.sh — Pull all persistent data from a Pi to this Mac.
#
# Run manually:  ./scripts/backup.sh
# Or let setup-backup-mac.sh schedule it daily via launchd.
#
# Creates a timestamped snapshot under $BACKUP_DEST (default ~/backups/j105-logger/).
# Keeps the most recent $KEEP_SNAPSHOTS snapshots; older ones are deleted.
#
# Prerequisites on the Mac:
#   - SSH access to the Pi (via Tailscale; key-based auth recommended)
#   - influx CLI for InfluxDB restore: brew install influxdb-cli
#   - sudo rsync allowed on the Pi for the SSH user (for Grafana dir)
#
# Environment overrides:
#   PI                 SSH target           (default: weaties@corvopi)
#   BACKUP_DEST        local snapshot root  (default: ~/backups/j105-logger)
#   KEEP_SNAPSHOTS     how many to retain   (default: 10)
#   INFLUX_TOKEN_FILE  path on the Pi       (default: ~/influx-token.txt)

set -euo pipefail

PI="${PI:-weaties@corvopi}"
BACKUP_DEST="${BACKUP_DEST:-$HOME/backups/j105-logger}"
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-10}"
INFLUX_TOKEN_FILE="${INFLUX_TOKEN_FILE:-~/influx-token.txt}"

DATE=$(date -u +%Y%m%dT%H%M%SZ)
SNAP="$BACKUP_DEST/$DATE"

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# Find the most recent previous snapshot to use as --link-dest base.
# rsync --link-dest creates hardlinks for files that are byte-for-byte identical
# to the previous snapshot, so unchanged files (photos, WAVs, etc.) cost zero
# extra disk space across snapshots.
PREV=$(ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | head -1)

# Pre-compute link-dest flags as variables to avoid command substitution
# inside &&-chains (which interacts badly with inline comments).
LINK_DATA=""
LINK_INFLUX=""
LINK_GRAFANA=""
if [ -n "$PREV" ]; then
  [ -d "$PREV/data" ]    && LINK_DATA="--link-dest=$PREV/data"
  [ -d "$PREV/influxdb" ] && LINK_INFLUX="--link-dest=$PREV/influxdb"
  [ -d "$PREV/grafana" ]  && LINK_GRAFANA="--link-dest=$PREV/grafana"
fi

# --info=progress2 requires rsync 3.1+; macOS ships with BSD rsync 2.6.9.
# Feature-detect rather than parse the version string.
if rsync --info=progress2 --version >/dev/null 2>&1; then
  RSYNC_PROGRESS="--info=progress2"
else
  RSYNC_PROGRESS="--progress"
fi

mkdir -p "$SNAP"
log "Starting backup → $SNAP"
log "Source: $PI"
[ -n "$PREV" ] && log "Link-dest (hardlink base): $PREV"

# ── 1. SQLite — WAL checkpoint then rsync ────────────────────────────────────
log "Step 1/4: SQLite WAL checkpoint + rsync"
ssh "$PI" "sqlite3 ~/j105-logger/data/logger.db 'PRAGMA wal_checkpoint(TRUNCATE);'" 2>/dev/null || \
  log "  WARNING: WAL checkpoint failed (DB may not exist yet); continuing"

# shellcheck disable=SC2086  # LINK_DATA is intentionally unquoted (empty or single flag)
rsync -az $RSYNC_PROGRESS $LINK_DATA \
  "$PI:~/j105-logger/data/" \
  "$SNAP/data/"
log "  SQLite + file data done"

# ── 2. InfluxDB — remote backup then rsync ───────────────────────────────────
log "Step 2/4: InfluxDB backup"
if ssh "$PI" "test -f $INFLUX_TOKEN_FILE" 2>/dev/null; then
  ssh "$PI" "influx backup /tmp/influx-backup \
    --host http://localhost:8086 \
    --token \$(cat $INFLUX_TOKEN_FILE)" 2>/dev/null && \
  rsync -az $RSYNC_PROGRESS $LINK_INFLUX \
    "$PI:/tmp/influx-backup/" \
    "$SNAP/influxdb/" && \
  ssh "$PI" "rm -rf /tmp/influx-backup" && \
  log "  InfluxDB backup done" || \
  log "  WARNING: InfluxDB backup failed; skipping (data dir intact on Pi)"
else
  log "  WARNING: $INFLUX_TOKEN_FILE not found on Pi; skipping InfluxDB backup"
fi

# ── 3. Grafana — rsync with sudo ─────────────────────────────────────────────
log "Step 3/4: Grafana data dir"
# shellcheck disable=SC2046
if rsync -az $RSYNC_PROGRESS \
    --rsync-path='sudo rsync' \
    $LINK_GRAFANA \
    "$PI:/var/lib/grafana/" \
    "$SNAP/grafana/" 2>/dev/null; then
  log "  Grafana backup done"
else
  log "  WARNING: Grafana rsync failed (sudo rsync not configured?); skipping"
fi

# ── 4. Rotate old snapshots ───────────────────────────────────────────────────
log "Step 4/4: Rotating snapshots (keeping $KEEP_SNAPSHOTS)"
# shellcheck disable=SC2012
STALE=$(ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | tail -n +"$((KEEP_SNAPSHOTS + 1))")
if [ -n "$STALE" ]; then
  echo "$STALE" | xargs rm -rf
  log "  Removed $(echo "$STALE" | wc -l | tr -d ' ') old snapshot(s)"
else
  log "  Nothing to rotate"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
SNAP_SIZE=$(du -sh "$SNAP" 2>/dev/null | cut -f1)
log "Backup complete — $SNAP  ($SNAP_SIZE)"
echo ""
echo "Snapshots in $BACKUP_DEST:"
ls -1t "$BACKUP_DEST" | head -"$KEEP_SNAPSHOTS" | sed 's/^/  /'
