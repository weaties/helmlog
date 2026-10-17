#!/usr/bin/env bash
# mirror-sync.sh — Keep the Mac mirror of the boat's HelmLog fresh.
#
# Run every ~15 min by launchd (com.helmlog.mirror-sync, installed by
# setup-mirror-mac.sh). Each run:
#   0. If $MIRROR_TRACK_BRANCH is set, moves $MIRROR_CODE_DIR to the latest
#      origin/<branch> and restarts the server (evergreen, like the Pi).
#   1. Pulls logger.db from the Pi into $MIRROR_ROOT/replica/ with
#      sqlite3_rsync — only changed pages cross the wire, the Pi's live DB is
#      read in a consistent transaction, and nothing is written on the Pi.
#   2. rsyncs data/audio, data/notes, data/avatars (append-only in practice).
#   3. At most every $REFRESH_INTERVAL_S, swaps a clone of the replica in as
#      the served DB ($MIRROR_ROOT/data/logger.db), carrying over logins and
#      coach grants made on the mirror, and restarts com.helmlog.mirror-serve.
#
# When the Pi is unreachable (on the water) the run exits 0 quietly — the
# mirror keeps serving the last copy and catches up when the Pi is back.
#
# Holding a demo: `touch $MIRROR_ROOT/HOLD` skips steps 0 and 3 (no code
# update, no swap, no restart) until the file is removed. Steps 1–2 keep running.
#
# Environment (all optional):
#   PI                 SSH target              (default: helmlog-backup@corvopi-live)
#   SSH_KEY            local SSH key           (default: ~/.ssh/helmlog-backup)
#   MIRROR_ROOT        mirror directory        (default: ~/helmlog-mirror)
#   MIRROR_CODE_DIR    checkout to serve       (default: $MIRROR_ROOT/code)
#   MIRROR_TRACK_BRANCH evergreen: follow origin/<branch> (default: unset = pinned)
#   PI_HELMLOG_DIR     helmlog dir on the Pi   (default: /home/weaties/helmlog)
#   PI_SQLITE3_RSYNC   sqlite3_rsync on the Pi (default: /home/helmlog-backup/bin/sqlite3_rsync)
#   SQLITE3_RSYNC      sqlite3_rsync locally   (default: $MIRROR_ROOT/bin/sqlite3_rsync)
#   REFRESH_INTERVAL_S min secs between swaps  (default: 3600)
#   SSH_TIMEOUT_S      hard cap on preflight   (default: 30)
#
# Exit codes: 0 ok or Pi offline, 1 sync/refresh error, 75 another run in progress.

set -uo pipefail

PI="${PI:-helmlog-backup@corvopi-live}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/helmlog-backup}"
MIRROR_ROOT="${MIRROR_ROOT:-$HOME/helmlog-mirror}"
MIRROR_CODE_DIR="${MIRROR_CODE_DIR:-$MIRROR_ROOT/code}"
MIRROR_TRACK_BRANCH="${MIRROR_TRACK_BRANCH:-}"
PI_HELMLOG_DIR="${PI_HELMLOG_DIR:-/home/weaties/helmlog}"
PI_SQLITE3_RSYNC="${PI_SQLITE3_RSYNC:-/home/helmlog-backup/bin/sqlite3_rsync}"
SQLITE3_RSYNC="${SQLITE3_RSYNC:-$MIRROR_ROOT/bin/sqlite3_rsync}"
REFRESH_INTERVAL_S="${REFRESH_INTERVAL_S:-3600}"
SSH_TIMEOUT_S="${SSH_TIMEOUT_S:-30}"
SERVE_LABEL="com.helmlog.mirror-serve"
SERVE_PLIST="$HOME/Library/LaunchAgents/$SERVE_LABEL.plist"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPLICA="$MIRROR_ROOT/replica/logger.db"
SERVED="$MIRROR_ROOT/data/logger.db"
STAMP="$MIRROR_ROOT/state/last-refresh"
SSH_WRAPPER="$MIRROR_ROOT/bin/ssh-pi"

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# Same pinned options as backup.sh; the wrapper exists because sqlite3_rsync's
# --ssh takes a program, not a command line.
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -F /dev/null)
mkdir -p "$MIRROR_ROOT"/{bin,replica,data,state,logs}
cat >"$SSH_WRAPPER" <<EOF
#!/bin/sh
exec ssh ${SSH_OPTS[*]} "\$@"
EOF
chmod +x "$SSH_WRAPPER"

# Hard timeout: Tailscale SSH "check" mode blocks forever waiting for a browser
# re-auth, which ConnectTimeout doesn't cover.
with_timeout() { perl -e 'alarm shift; exec @ARGV' "$@"; }

LOCK="$MIRROR_ROOT/state/sync.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  log "Another mirror-sync is running ($LOCK) — skipping"
  exit 75
fi
trap 'rmdir "$LOCK"' EXIT

# ── 0. Code (evergreen) ───────────────────────────────────────────────────────
# Like the Pi's evergreen deploy mode: follow origin/$MIRROR_TRACK_BRANCH and
# restart the server when it advances. Runs even when the Pi is offline.
if [ -n "$MIRROR_TRACK_BRANCH" ] && [ ! -e "$MIRROR_ROOT/HOLD" ]; then
  if with_timeout 60 git -C "$MIRROR_CODE_DIR" fetch -q origin "$MIRROR_TRACK_BRANCH"; then
    want=$(git -C "$MIRROR_CODE_DIR" rev-parse FETCH_HEAD)
    have=$(git -C "$MIRROR_CODE_DIR" rev-parse HEAD)
    if [ "$want" != "$have" ]; then
      log "Code: ${have:0:7} → ${want:0:7} (origin/$MIRROR_TRACK_BRANCH)"
      if git -C "$MIRROR_CODE_DIR" checkout -q --detach "$want" \
        && (cd "$MIRROR_CODE_DIR" && uv sync -q); then
        launchctl kickstart -k "gui/$(id -u)/$SERVE_LABEL" 2>/dev/null \
          || log "  WARNING: $SERVE_LABEL not loaded — not restarted"
      else
        log "  WARNING: code update failed — still serving ${have:0:7}"
      fi
    fi
  else
    log "  WARNING: git fetch origin $MIRROR_TRACK_BRANCH failed — code not updated"
  fi
fi

# ── 1. Database ───────────────────────────────────────────────────────────────
if ! with_timeout "$SSH_TIMEOUT_S" "$SSH_WRAPPER" "$PI" true 2>/dev/null; then
  log "Pi unreachable or SSH blocked ($PI; Tailscale 'check' mode?) — serving last copy"
  exit 0
fi

log "sqlite3_rsync $PI:$PI_HELMLOG_DIR/data/logger.db → $REPLICA"
if ! "$SQLITE3_RSYNC" --ssh "$SSH_WRAPPER" --exe "$PI_SQLITE3_RSYNC" \
  "$PI:$PI_HELMLOG_DIR/data/logger.db" "$REPLICA" -v; then
  log "FAIL: sqlite3_rsync"
  exit 1
fi

# ── 2. Files referenced by the DB ─────────────────────────────────────────────
for d in audio notes avatars; do
  rsync -a -e "$SSH_WRAPPER" "$PI:$PI_HELMLOG_DIR/data/$d/" "$MIRROR_ROOT/data/$d/" 2>/dev/null \
    || log "  WARNING: rsync data/$d failed (may not exist on the Pi)"
done

# ── 3. Refresh the served copy ────────────────────────────────────────────────
if [ -e "$MIRROR_ROOT/HOLD" ]; then
  log "HOLD present — not refreshing the served DB"
  exit 0
fi
now=$(date +%s)
last=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ -e "$SERVED" ] && [ $((now - last)) -lt "$REFRESH_INTERVAL_S" ]; then
  log "Served DB refreshed $(((now - last) / 60)) min ago — next swap after ${REFRESH_INTERVAL_S}s"
  exit 0
fi

log "Refreshing served DB"
sqlite3 "$REPLICA" 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null
# APFS clone: instant and takes no extra space until pages diverge.
cp -c "$REPLICA" "$SERVED.next" || { log "FAIL: clone replica"; exit 1; }

launchctl bootout "gui/$(id -u)/$SERVE_LABEL" 2>/dev/null
for _ in $(seq 30); do
  launchctl print "gui/$(id -u)/$SERVE_LABEL" >/dev/null 2>&1 || break
  perl -e 'select(undef,undef,undef,0.5)'
done

if [ -e "$SERVED" ]; then
  # Reads the old DB with its WAL, so do it before the WAL files go away.
  uv run --project "$MIRROR_CODE_DIR" python "$SCRIPT_DIR/mirror.py" carry "$SERVED" "$SERVED.next" \
    || log "  WARNING: carry-over failed — mirror logins/coach links reset"
fi
rm -f "$SERVED-wal" "$SERVED-shm"
mv "$SERVED.next" "$SERVED"
echo "$now" >"$STAMP"

if [ -e "$SERVE_PLIST" ]; then
  launchctl bootstrap "gui/$(id -u)" "$SERVE_PLIST" || { log "FAIL: restart $SERVE_LABEL"; exit 1; }
fi
log "Served DB refreshed; $SERVE_LABEL restarted"
