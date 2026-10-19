#!/usr/bin/env bash
# setup-mirror-mac.sh — Install the Mac mirror of the boat's HelmLog.
#
# Run once from the project root (re-running is safe; existing files are kept):
#   ./scripts/setup-mirror-mac.sh
#
# Installs three launchd agents (see docs/mirror.md):
#   com.helmlog.mirror-sync    every 15 min: scripts/mirror-sync.sh
#   com.helmlog.mirror-serve   always on:    scripts/mirror.py serve on 127.0.0.1:$MIRROR_PORT
#   com.helmlog.mirror-tunnel  always on:    cloudflared → https://$MIRROR_HOSTNAME
#                              (only if $MIRROR_ROOT/cloudflare-token exists)
#
# Prerequisites:
#   - backup SSH access to the Pi (scripts/setup-backup-mac.sh) — same key + user
#   - sqlite3_rsync at $MIRROR_ROOT/bin/sqlite3_rsync and on the Pi at
#     /home/helmlog-backup/bin/sqlite3_rsync (same SQLite version; docs/mirror.md)
#   - for the public hostname: `brew install cloudflared` and a tunnel token
#
# Environment (all optional):
#   MIRROR_ROOT      (default: ~/helmlog-mirror)
#   MIRROR_CODE_DIR  checkout to serve        (default: $MIRROR_ROOT/code, created detached)
#   MIRROR_REF       ref for a new code dir   (default: origin/main; mirror-sync
#                    then moves it to whatever the Pi runs)
#   MIRROR_FOLLOW_PI 1 = code dir follows the Pi's deploy branch + mode;
#                    0 = pinned, update it by hand (default: 1)
#   MIRROR_PORT      local web port           (default: 3012)
#   MIRROR_HOSTNAME  public hostname          (default: corvo105-mirror.helmlog.org)
#   BACKUP_DEST      snapshot root to seed the replica from (default: ~/backups/helmlog)
#
# To uninstall:
#   for a in sync serve tunnel; do
#     launchctl bootout gui/$(id -u)/com.helmlog.mirror-$a
#     rm ~/Library/LaunchAgents/com.helmlog.mirror-$a.plist
#   done

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MIRROR_ROOT="${MIRROR_ROOT:-$HOME/helmlog-mirror}"
MIRROR_CODE_DIR="${MIRROR_CODE_DIR:-$MIRROR_ROOT/code}"
MIRROR_REF="${MIRROR_REF:-origin/main}"
MIRROR_FOLLOW_PI="${MIRROR_FOLLOW_PI:-1}"
MIRROR_PORT="${MIRROR_PORT:-3012}"
MIRROR_HOSTNAME="${MIRROR_HOSTNAME:-corvo105-mirror.helmlog.org}"
BACKUP_DEST="${BACKUP_DEST:-$HOME/backups/helmlog}"
AGENTS="$HOME/Library/LaunchAgents"
UV="$(command -v uv)"
LAUNCHD_PATH="$(dirname "$UV"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

log() { echo "==> $*"; }

mkdir -p "$MIRROR_ROOT"/{bin,replica,data,state,logs} "$AGENTS"

# ── Code to serve ─────────────────────────────────────────────────────────────
if [ ! -d "$MIRROR_CODE_DIR" ]; then
  log "Creating detached checkout $MIRROR_CODE_DIR at $MIRROR_REF"
  git -C "$REPO_DIR" worktree add --detach "$MIRROR_CODE_DIR" "$MIRROR_REF"
fi
(cd "$MIRROR_CODE_DIR" && "$UV" sync -q)

# ── Seed the replica from the newest snapshot (first sync is then a delta) ────
if [ ! -e "$MIRROR_ROOT/replica/logger.db" ]; then
  seed=$(ls -d "$BACKUP_DEST"/2*/data/logger.db 2>/dev/null | sort | tail -1 || true)
  if [ -n "$seed" ]; then
    log "Seeding replica from $seed"
    cp -c "$seed" "$MIRROR_ROOT/replica/logger.db"
  fi
fi
if [ ! -e "$MIRROR_ROOT/data/logger.db" ] && [ -e "$MIRROR_ROOT/replica/logger.db" ]; then
  log "Creating served DB from the replica"
  cp -c "$MIRROR_ROOT/replica/logger.db" "$MIRROR_ROOT/data/logger.db"
fi

# ── Mirror .env (read by mirror.py serve from $MIRROR_ROOT) ───────────────────
if [ ! -e "$MIRROR_ROOT/.env" ]; then
  log "Writing $MIRROR_ROOT/.env"
  cat >"$MIRROR_ROOT/.env" <<EOF
DB_PATH=data/logger.db
AUDIO_DIR=data/audio
NOTES_DIR=data/notes
AVATAR_DIR=data/avatars
WEB_HOST=127.0.0.1
WEB_PORT=$MIRROR_PORT
PUBLIC_URL=https://$MIRROR_HOSTNAME
EXTERNAL_DATA_ENABLED=false
SESSION_SECRET=$(openssl rand -hex 32)
EOF
  chmod 600 "$MIRROR_ROOT/.env"
fi

# ── launchd agents ────────────────────────────────────────────────────────────
write_plist() { # label, program-args xml, extra keys xml
  cat >"$AGENTS/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array>$2</array>
  <key>WorkingDirectory</key><string>$MIRROR_ROOT</string>
  <key>StandardOutPath</key><string>$MIRROR_ROOT/logs/${1#com.helmlog.}.log</string>
  <key>StandardErrorPath</key><string>$MIRROR_ROOT/logs/${1#com.helmlog.}.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$LAUNCHD_PATH</string>
    <key>HOME</key><string>$HOME</string>
    <key>MIRROR_ROOT</key><string>$MIRROR_ROOT</string>
    <key>MIRROR_CODE_DIR</key><string>$MIRROR_CODE_DIR</string>
    <key>MIRROR_FOLLOW_PI</key><string>$MIRROR_FOLLOW_PI</string>
  </dict>
  $3
</dict>
</plist>
EOF
}

load() {
  launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
  # bootout returns before the job is gone; bootstrapping too early fails with EIO.
  for _ in $(seq 30); do
    launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1 || break
    sleep 0.5
  done
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$1.plist"
  log "Loaded $1"
}

write_plist com.helmlog.mirror-serve \
  "<string>$UV</string><string>run</string><string>--project</string><string>$MIRROR_CODE_DIR</string><string>python</string><string>$SCRIPT_DIR/mirror.py</string><string>serve</string>" \
  "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>"

write_plist com.helmlog.mirror-sync \
  "<string>/bin/bash</string><string>$SCRIPT_DIR/mirror-sync.sh</string>" \
  "<key>RunAtLoad</key><true/><key>StartInterval</key><integer>900</integer>"

load com.helmlog.mirror-serve
load com.helmlog.mirror-sync

if [ -s "$MIRROR_ROOT/cloudflare-token" ]; then
  write_plist com.helmlog.mirror-tunnel \
    "<string>$(command -v cloudflared)</string><string>tunnel</string><string>--no-autoupdate</string><string>run</string><string>--token-file</string><string>$MIRROR_ROOT/cloudflare-token</string>" \
    "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
  load com.helmlog.mirror-tunnel
else
  log "No $MIRROR_ROOT/cloudflare-token — skipping the tunnel (local only: http://127.0.0.1:$MIRROR_PORT)"
fi

log "Done. Logs: $MIRROR_ROOT/logs/   Hold refreshes during a demo: touch $MIRROR_ROOT/HOLD"
