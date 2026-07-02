#!/usr/bin/env bash
# backup.sh — Pull all persistent data from a Pi to this Mac.
#
# Run manually:  ./scripts/backup.sh
# Or let setup-backup-mac.sh schedule it daily via launchd.
#
# Creates a timestamped snapshot under $BACKUP_DEST (default ~/backups/helmlog/).
# Keeps the most recent $KEEP_SNAPSHOTS snapshots; older ones are deleted.
#
# Prerequisites on the Mac:
#   - SSH access to the Pi as `helmlog-backup` (provisioned by
#     scripts/setup-backup-user.sh; key-based auth via ~/.ssh/helmlog-backup)
#   - influx CLI for InfluxDB backup: brew install influxdb-cli
#   - Python 3 on PATH (for the email report helper; stdlib only)
#
# Required environment:
#   PI                 SSH target, e.g. helmlog-backup@pi-hostname (no default)
#   REPORT_TO          email recipient (required unless SKIP_EMAIL=1)
#
# Optional environment overrides:
#   BACKUP_DEST        local snapshot root       (default: ~/backups/helmlog)
#   KEEP_SNAPSHOTS     how many to retain        (default: 10)
#   SSH_KEY            local SSH key path        (default: ~/.ssh/helmlog-backup)
#   PI_HELMLOG_DIR     helmlog dir on the Pi     (default: /home/weaties/helmlog)
#   PI_SIGNALK_DIR     signalk dir on the Pi     (default: /home/weaties/.signalk)
#   INFLUX_TOKEN_FILE  path on the Pi            (default: /home/weaties/influx-token.txt)
#   SMTP_CACHE         local creds cache path    (default: ~/.config/helmlog-backup/smtp.env)
#   MIN_SNAPSHOT_BYTES safety-gate threshold     (default: 10485760 — 10 MiB)
#   SKIP_EMAIL         set to 1 to suppress mail (default: unset)
#
# Exit codes:
#   0   backup succeeded
#   10  SSH preflight failed (source unreachable)
#   11  Safety gate failed (snapshot suspiciously empty; rotation skipped)
#   1   other error

set -uo pipefail

: "${PI:?PI must be set — e.g. export PI=user@pi-hostname}"
if [ "${SKIP_EMAIL:-0}" != "1" ]; then
  : "${REPORT_TO:?REPORT_TO must be set (or set SKIP_EMAIL=1)}"
fi
BACKUP_DEST="${BACKUP_DEST:-$HOME/backups/helmlog}"
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-10}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/helmlog-backup}"
PI_HELMLOG_DIR="${PI_HELMLOG_DIR:-/home/weaties/helmlog}"
PI_SIGNALK_DIR="${PI_SIGNALK_DIR:-/home/weaties/.signalk}"
INFLUX_TOKEN_FILE="${INFLUX_TOKEN_FILE:-/home/weaties/influx-token.txt}"
SMTP_CACHE="${SMTP_CACHE:-$HOME/.config/helmlog-backup/smtp.env}"
MIN_SNAPSHOT_BYTES="${MIN_SNAPSHOT_BYTES:-10485760}"
# Where on the Pi to stage the sqlite3 .backup. MUST be (a) on a roomy
# filesystem, (b) writable by the backup SSH user (helmlog-backup), and
# (c) OUTSIDE every rsync'd tree. The old default /tmp is a 4 GB tmpfs on
# corvopi-live, so once logger.db passed ~4 GB the .backup ran out of space
# and left a corrupt all-zeros file that silently clobbered the good copy
# (every snapshot from 2026-06-16 on). /var/tmp is disk-backed on the 382 GB
# root partition and world-writable — the right drop-in. (#807)
PI_STAGE_DIR="${PI_STAGE_DIR:-/var/tmp/helmlog-backup}"

# Pin every ssh/scp/rsync to the dedicated key + non-interactive options. This
# avoids picking up a stale agent identity and ensures predictable behaviour
# under launchd. `-F /dev/null` ignores ~/.ssh/config so a stray Host stanza
# can't redirect the connection.
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -F /dev/null)
SSH="ssh ${SSH_OPTS[*]}"
RSYNC_SSH="ssh ${SSH_OPTS[*]}"

DATE=$(date -u +%Y%m%dT%H%M%SZ)
SNAP="$BACKUP_DEST/$DATE"
REPORT="/tmp/helmlog-backup-report-$DATE.md"
STDERR_LOG="/tmp/helmlog-backup-stderr-$DATE.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAIL_HELPER="$SCRIPT_DIR/backup_report_mail.py"

STATUS="ok"
STATUS_REASON=""
WARNINGS=()
SECONDS=0

# Capture everything this script writes to stderr so the failure report can
# include the full diagnostic trail. Use `tee` so we still see output on the
# tty when running manually, and so launchd's own log gets populated too.
exec 2> >(tee "$STDERR_LOG" >&2)

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# Append a line to the markdown report. First arg is the line; no newline
# handling beyond what `echo` does.
report_line() { echo "$*" >> "$REPORT"; }

mark_failed() {
  STATUS="failed"
  if [ -z "$STATUS_REASON" ]; then
    STATUS_REASON="$*"
  fi
}

# Record a warning without flipping STATUS. Used for rsync partial-transfer
# exit codes (23, 24) — the snapshot is usable but some files were skipped.
mark_warning() {
  WARNINGS+=("$*")
}

# Human-readable size for a path (falls back to "—" if the path is missing).
human_size() {
  local path="$1"
  if [ -d "$path" ] || [ -f "$path" ]; then
    du -sh "$path" 2>/dev/null | cut -f1
  else
    echo "—"
  fi
}

# File count under a path (0 if missing).
file_count() {
  local path="$1"
  if [ -d "$path" ]; then
    find "$path" -type f 2>/dev/null | wc -l | tr -d ' '
  else
    echo 0
  fi
}

# Bytes for a path, integer (0 if missing). macOS `du` lacks --bytes; use the
# BSD-friendly 512-byte block output and multiply.
bytes_size() {
  local path="$1"
  if [ -d "$path" ] || [ -f "$path" ]; then
    du -sk "$path" 2>/dev/null | awk '{print $1 * 1024}'
  else
    echo 0
  fi
}

# Seconds since an epoch timestamp (set before each step).
elapsed_since() {
  local start="$1"
  echo $(( $(date +%s) - start ))
}

fmt_duration() {
  local s="$1"
  local h=$(( s / 3600 ))
  local m=$(( (s % 3600) / 60 ))
  local sec=$(( s % 60 ))
  if [ "$h" -gt 0 ]; then
    printf '%dh%02dm%02ds' "$h" "$m" "$sec"
  elif [ "$m" -gt 0 ]; then
    printf '%dm%02ds' "$m" "$sec"
  else
    printf '%ds' "$sec"
  fi
}

# Initialise the report header early so preflight failures still have a file
# to email out.
init_report() {
  cat > "$REPORT" <<EOF
# Helmlog backup report

- **Target Pi**: \`$PI\`
- **Snapshot**: \`$SNAP\`
- **Started**: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- **Backup root**: \`$BACKUP_DEST\`

## Steps

EOF
}

# Runs on EVERY exit from this script: successful, expected failure, or
# unexpected error. Finalises the report and fires the email helper. We
# guard against recursion by disabling the trap inside the handler.
on_exit() {
  local rc=$?
  trap - EXIT

  local total_size
  local total_files
  local total_bytes
  total_size=$(human_size "$SNAP")
  total_files=$(file_count "$SNAP")
  total_bytes=$(bytes_size "$SNAP")

  report_line ""
  report_line "## Summary"
  report_line ""
  report_line "- **Duration**: $(fmt_duration "$SECONDS")"
  report_line "- **Snapshot size**: $total_size ($total_bytes bytes)"
  report_line "- **File count**: $total_files"
  if [ -n "${PREV:-}" ]; then
    local prev_size
    prev_size=$(human_size "$PREV")
    report_line "- **Previous snapshot**: \`$PREV\` ($prev_size)"
  fi
  report_line "- **Exit code**: $rc"
  if [ "$STATUS" = "ok" ] && [ "${#WARNINGS[@]}" -gt 0 ]; then
    report_line "- **Status**: SUCCESS (with warnings)"
  else
    report_line "- **Status**: $([ "$STATUS" = "ok" ] && echo SUCCESS || echo FAILURE)"
  fi
  if [ -n "$STATUS_REASON" ]; then
    report_line "- **Reason**: $STATUS_REASON"
  fi

  if [ "${#WARNINGS[@]}" -gt 0 ]; then
    report_line ""
    report_line "## Warnings"
    report_line ""
    local w
    for w in "${WARNINGS[@]}"; do
      report_line "- $w"
    done
  fi

  report_line ""
  report_line "## Snapshots on disk"
  report_line ""
  report_line '```'
  ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | head -"$KEEP_SNAPSHOTS" >> "$REPORT" || true
  report_line '```'

  # Send the email unless explicitly suppressed.
  if [ "${SKIP_EMAIL:-0}" = "1" ]; then
    log "SKIP_EMAIL=1; not sending report"
  elif [ ! -x "$MAIL_HELPER" ] && [ ! -f "$MAIL_HELPER" ]; then
    log "mail helper not found at $MAIL_HELPER; not sending report"
  elif [ ! -f "$SMTP_CACHE" ]; then
    log "no SMTP cache at $SMTP_CACHE; cannot send report (first run must succeed once while Pi is reachable)"
  else
    if python3 "$MAIL_HELPER" \
        --status "$STATUS" \
        --report "$REPORT" \
        --creds "$SMTP_CACHE" \
        --to "$REPORT_TO" \
        --target "$PI" \
        --stderr "$STDERR_LOG"; then
      log "Report emailed to $REPORT_TO"
    else
      log "WARNING: report email failed (rc=$?); report retained at $REPORT"
    fi
  fi
}
trap on_exit EXIT

init_report

# ── Preflight ─────────────────────────────────────────────────────────────────
log "Preflight: SSH check to $PI"
if ! $SSH -o ConnectTimeout=10 "$PI" true 2>&1; then
  mark_failed "SSH preflight to $PI failed (host unreachable or auth failure)"
  log "  FAIL: SSH preflight"
  report_line "- **Preflight**: FAILED — could not SSH to \`$PI\`"
  report_line ""
  report_line "Snapshot directory was NOT created. No rotation performed."
  exit 10
fi
log "  OK"
report_line "- **Preflight**: OK — \`$PI\` reachable"

# ── Refresh SMTP credential cache from the Pi ────────────────────────────────
log "Refreshing SMTP credential cache → $SMTP_CACHE"
mkdir -p "$(dirname "$SMTP_CACHE")"
# Pull just the SMTP_* lines from the Pi's .env so the cache never holds
# unrelated secrets. `grep -E` yields rc=1 on no match, which we swallow.
SMTP_TMP="$(mktemp)"
if $SSH "$PI" "grep -E '^SMTP_' $PI_HELMLOG_DIR/.env 2>/dev/null" > "$SMTP_TMP" 2>&1; then
  if [ -s "$SMTP_TMP" ]; then
    mv "$SMTP_TMP" "$SMTP_CACHE"
    chmod 600 "$SMTP_CACHE"
    log "  Cached $(wc -l < "$SMTP_CACHE" | tr -d ' ') SMTP_* vars"
  else
    rm -f "$SMTP_TMP"
    log "  WARNING: Pi's .env has no SMTP_* lines; keeping previous cache if any"
  fi
else
  rm -f "$SMTP_TMP"
  log "  WARNING: could not read Pi's .env for SMTP cache; falling back to existing cache"
fi

mkdir -p "$SNAP"
log "Starting backup → $SNAP"

# Find the most recent previous snapshot to use as --link-dest base.
PREV=$(ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | grep -v "^$SNAP\$" | head -1 || true)

LINK_DATA=""
LINK_INFLUX=""
LINK_GRAFANA=""
if [ -n "$PREV" ]; then
  [ -d "$PREV/data" ]     && LINK_DATA="--link-dest=$PREV/data"
  [ -d "$PREV/influxdb" ] && LINK_INFLUX="--link-dest=$PREV/influxdb"
  [ -d "$PREV/grafana" ]  && LINK_GRAFANA="--link-dest=$PREV/grafana"
fi

# --info=progress2 requires rsync 3.1+; macOS ships with BSD rsync 2.6.9.
if rsync --info=progress2 --version >/dev/null 2>&1; then
  RSYNC_PROGRESS="--info=progress2"
else
  RSYNC_PROGRESS="--progress"
fi

[ -n "$PREV" ] && log "Link-dest (hardlink base): $PREV"

# run_step NAME REMOTE_SUBDIR <command...>
# Records per-step status/duration/size/count in the report. On failure the
# step is logged as FAILED and mark_failed is called, but the backup continues
# so we still capture whatever other steps work. The safety gate at the end
# decides whether to honour rotation.
run_step() {
  local name="$1"; shift
  local local_dir="$1"; shift
  local start
  start=$(date +%s)
  log "Step: $name"
  if "$@"; then
    local dur
    dur=$(elapsed_since "$start")
    local size count
    size=$(human_size "$local_dir")
    count=$(file_count "$local_dir")
    log "  OK ($size, $count files, $(fmt_duration "$dur"))"
    report_line "- **$name**: OK — $size, $count files, $(fmt_duration "$dur")"
    return 0
  else
    local rc=$?
    local dur
    dur=$(elapsed_since "$start")
    log "  FAILED (rc=$rc, $(fmt_duration "$dur"))"
    report_line "- **$name**: FAILED (rc=$rc, $(fmt_duration "$dur"))"
    mark_failed "$name failed (rc=$rc)"
    return "$rc"
  fi
}

# run_rsync_step NAME LOCAL_DIR <rsync...>
# Like run_step, but distinguishes rsync's partial-transfer exit codes from
# hard failures:
#
#   rc  0        → OK
#   rc  23       → WARN (some files could not be transferred, e.g. permission
#                  denied or the user can't traverse a directory). The
#                  snapshot of every readable file is still valid.
#   rc  24       → WARN (some files vanished between the file-list build and
#                  the transfer — benign on a live system).
#   any other rc → FAILED, mark_failed is called.
#
# This keeps the overall STATUS="ok" when rsync only complained about a few
# unreadable files. The safety gate still has the final say on whether the
# snapshot is usable. See #544.
run_rsync_step() {
  local name="$1"; shift
  local local_dir="$1"; shift
  local start
  start=$(date +%s)
  log "Step: $name"
  "$@"
  local rc=$?
  local dur
  dur=$(elapsed_since "$start")
  local size count
  size=$(human_size "$local_dir")
  count=$(file_count "$local_dir")
  case "$rc" in
    0)
      log "  OK ($size, $count files, $(fmt_duration "$dur"))"
      report_line "- **$name**: OK — $size, $count files, $(fmt_duration "$dur")"
      return 0
      ;;
    23|24)
      log "  WARN (rc=$rc partial transfer, $size, $count files, $(fmt_duration "$dur"))"
      report_line "- **$name**: WARN — partial transfer (rc=$rc), $size, $count files, $(fmt_duration "$dur")"
      mark_warning "$name: rsync rc=$rc (partial transfer — see stderr for skipped files)"
      return 0
      ;;
    *)
      log "  FAILED (rc=$rc, $(fmt_duration "$dur"))"
      report_line "- **$name**: FAILED (rc=$rc, $(fmt_duration "$dur"))"
      mark_failed "$name failed (rc=$rc)"
      return "$rc"
      ;;
  esac
}

# ── 1. SQLite — consistent snapshot via sqlite3 .backup, then rsync ──────────
# Previous versions relied on PRAGMA wal_checkpoint(TRUNCATE) and hoped no
# writes landed mid-rsync. `sqlite3 .backup` gives an atomic copy that is
# safe to transfer regardless of active WAL (#676). We overwrite the raw DB
# that rsync picks up with the staged snapshot after the tree copy.
#
# Stage on a roomy filesystem and VERIFY before trusting it. The .backup is
# only honoured if the command exits 0 AND the staged file opens as a valid
# SQLite DB (PRAGMA quick_check = ok). Otherwise we leave rsync's live-file
# copy in place rather than clobbering it with a partial/corrupt snapshot —
# the failure mode that silently broke every backup from 2026-06-16 on. (#807)
PI_STAGE="$PI_STAGE_DIR/logger.db.snap"
DB_SNAP_OK=0
if $SSH "$PI" "mkdir -p '$PI_STAGE_DIR' && rm -f '$PI_STAGE'*; \
    sqlite3 '$PI_HELMLOG_DIR/data/logger.db' \".backup '$PI_STAGE'\" && \
    [ \"\$(sqlite3 'file:$PI_STAGE?immutable=1' 'PRAGMA quick_check;' 2>/dev/null | head -1)\" = ok ]" 2>/dev/null; then
  DB_SNAP_OK=1
  log "  DB snapshot staged + verified at $PI_STAGE on $PI"
else
  log "  WARNING: sqlite3 .backup missing/failed/invalid; keeping rsync'd live-file copy"
  mark_warning "consistent DB snapshot unavailable — snapshot holds the rsync'd live-file copy (verify it opens)"
  $SSH "$PI" "rm -f '$PI_STAGE'*" 2>/dev/null || true
fi

# --rsync-path='sudo rsync' so we can read photos in notes/<session>/ — those
# are written by the helmlog service as 0600/helmlog and were previously
# silently skipped (rc=23 partial transfer), producing the orphaned
# moment_attachments rows tracked in #676.
# shellcheck disable=SC2086  # LINK_DATA is intentionally unquoted (empty or single flag)
run_rsync_step "SQLite + file data" "$SNAP/data" \
  rsync -e "$RSYNC_SSH" -az $RSYNC_PROGRESS $LINK_DATA \
    --rsync-path='sudo rsync' \
    "$PI:$PI_HELMLOG_DIR/data/" \
    "$SNAP/data/" || true

# Replace rsync's live-DB copy with the consistent snapshot — ONLY when the
# stage verified above. Then drop any logger.db-wal / -shm rsync pulled from
# the live tree: they belong to the live DB, not this consistent copy, and a
# stale WAL alongside it confuses readers.
if [ "$DB_SNAP_OK" = 1 ]; then
  rsync -e "$RSYNC_SSH" -az "$PI:$PI_STAGE" "$SNAP/data/logger.db" && \
    log "  DB replaced with consistent snapshot"
  rm -f "$SNAP/data/logger.db-wal" "$SNAP/data/logger.db-shm"
  $SSH "$PI" "rm -f '$PI_STAGE'*" 2>/dev/null || true
fi

# Cross-check the snapshot: every moment_attachments / audio_sessions /
# users.avatar_path row must point at a file that actually exists in the
# snapshot. Silent losses here are how corvopi-tst1 ended up with 25 photo
# rows pointing at empty directories (#676).
VALIDATOR="$SCRIPT_DIR/validate_snapshot.py"
if [ -f "$SNAP/data/logger.db" ] && [ -f "$VALIDATOR" ] && command -v python3 >/dev/null; then
  log "Step: Snapshot validation"
  VALIDATE_OUT="/tmp/helmlog-validate-$DATE.txt"
  if python3 "$VALIDATOR" "$SNAP/data" > "$VALIDATE_OUT" 2>&1; then
    log "  OK (all referenced files present)"
    report_line "- **Snapshot validation**: OK (all referenced files present)"
  else
    rc=$?
    log "  WARNING: orphaned DB rows detected (rc=$rc)"
    report_line "- **Snapshot validation**: WARN (rc=$rc) — orphaned DB rows detected"
    report_line '```'
    head -40 "$VALIDATE_OUT" >> "$REPORT" || true
    report_line '```'
    mark_warning "Snapshot validation: orphaned rows (see report body)"
  fi
  rm -f "$VALIDATE_OUT"
fi

# ── 2. InfluxDB — remote backup then rsync ───────────────────────────────────
influx_backup() {
  if ! $SSH "$PI" "test -f $INFLUX_TOKEN_FILE" 2>/dev/null; then
    log "  $INFLUX_TOKEN_FILE not found on Pi; skipping"
    report_line "- **InfluxDB**: SKIPPED — no token file on Pi"
    return 0
  fi
  $SSH "$PI" "influx backup /tmp/influx-backup \
    --host http://localhost:8086 \
    --token \$(cat $INFLUX_TOKEN_FILE)" || return $?
  # shellcheck disable=SC2086
  rsync -e "$RSYNC_SSH" -az $RSYNC_PROGRESS $LINK_INFLUX \
    "$PI:/tmp/influx-backup/" \
    "$SNAP/influxdb/" || return $?
  $SSH "$PI" "rm -rf /tmp/influx-backup" || true
}
if influx_backup; then
  size=$(human_size "$SNAP/influxdb")
  count=$(file_count "$SNAP/influxdb")
  report_line "- **InfluxDB**: OK — $size, $count files"
else
  report_line "- **InfluxDB**: FAILED (rc=$?)"
  mark_failed "InfluxDB backup failed"
fi

# ── 3. Config — .env, Signal K, influx token ────────────────────────────────
mkdir -p "$SNAP/config"
if rsync -e "$RSYNC_SSH" -az $RSYNC_PROGRESS \
    "$PI:$PI_HELMLOG_DIR/.env" \
    "$SNAP/config/helmlog.env" 2>&1; then
  report_line "- **helmlog .env**: OK — $(human_size "$SNAP/config/helmlog.env")"
else
  report_line "- **helmlog .env**: FAILED"
  mark_failed ".env rsync failed"
fi

# influx-token.txt is needed by restore.sh to authenticate against the
# target's InfluxDB after a prior `influx restore --full`, which replaces all
# auth on the target with the source's tokens.
if rsync -e "$RSYNC_SSH" -az "$PI:$INFLUX_TOKEN_FILE" "$SNAP/config/influx-token.txt" 2>&1; then
  report_line "- **Influx token file**: OK"
else
  report_line "- **Influx token file**: FAILED"
  mark_failed "influx-token.txt rsync failed"
fi

# Signal K — exclude node_modules (huge, reinstallable via npm install)
LINK_SK=""
[ -n "$PREV" ] && [ -d "$PREV/signalk" ] && LINK_SK="--link-dest=$PREV/signalk"
# shellcheck disable=SC2086
run_rsync_step "Signal K config + data" "$SNAP/signalk" \
  rsync -e "$RSYNC_SSH" -az $RSYNC_PROGRESS $LINK_SK \
    --exclude='node_modules/' \
    --exclude='package-lock.json' \
    "$PI:$PI_SIGNALK_DIR/" \
    "$SNAP/signalk/" || true

# ── 4. Grafana — data dir + provisioning config ──────────────────────────────
# shellcheck disable=SC2086
run_rsync_step "Grafana data dir" "$SNAP/grafana" \
  rsync -e "$RSYNC_SSH" -az $RSYNC_PROGRESS \
    --rsync-path='sudo rsync' \
    $LINK_GRAFANA \
    "$PI:/var/lib/grafana/" \
    "$SNAP/grafana/" || true

LINK_GP=""
[ -n "$PREV" ] && [ -d "$PREV/grafana-provisioning" ] && \
  LINK_GP="--link-dest=$PREV/grafana-provisioning"
# shellcheck disable=SC2086
run_rsync_step "Grafana provisioning" "$SNAP/grafana-provisioning" \
  rsync -e "$RSYNC_SSH" -az $RSYNC_PROGRESS \
    --rsync-path='sudo rsync' \
    $LINK_GP \
    "$PI:/etc/grafana/provisioning/" \
    "$SNAP/grafana-provisioning/" || true

# ── 5. Safety gate + rotation ─────────────────────────────────────────────────
log "Safety gate: data/logger.db must be present, > $MIN_SNAPSHOT_BYTES bytes, and a valid SQLite DB"
snap_bytes=$(bytes_size "$SNAP")
if [ ! -f "$SNAP/data/logger.db" ]; then
  mark_failed "safety gate failed: $SNAP/data/logger.db is missing"
  log "  FAIL: logger.db missing in new snapshot"
  report_line ""
  report_line "**Safety gate**: FAILED — \`data/logger.db\` missing. Rotation skipped; old snapshots preserved."
  exit 11
fi
if [ "$snap_bytes" -lt "$MIN_SNAPSHOT_BYTES" ]; then
  mark_failed "safety gate failed: snapshot only $snap_bytes bytes (< $MIN_SNAPSHOT_BYTES)"
  log "  FAIL: snapshot too small ($snap_bytes bytes)"
  report_line ""
  report_line "**Safety gate**: FAILED — snapshot size $snap_bytes bytes is below the $MIN_SNAPSHOT_BYTES-byte minimum. Rotation skipped; old snapshots preserved."
  exit 11
fi
# logger.db must be a VALID SQLite database, not just present and large. The
# 2026-06-16..30 corruption (a 4 GB all-zeros file) passed the size check; a
# header + schema read catches it instantly. `immutable=1` opens the static
# backup read-only without needing a -wal/-shm so it can't mutate the snapshot.
if ! head -c 16 "$SNAP/data/logger.db" | grep -aq 'SQLite format 3' || \
   ! sqlite3 "file:$SNAP/data/logger.db?immutable=1" 'SELECT count(*) FROM sqlite_master;' >/dev/null 2>&1; then
  mark_failed "safety gate failed: $SNAP/data/logger.db is not a valid SQLite database"
  log "  FAIL: logger.db is not a valid SQLite database"
  report_line ""
  report_line "**Safety gate**: FAILED — \`data/logger.db\` is not a valid SQLite database (corrupt/partial snapshot). Rotation skipped; old snapshots preserved."
  exit 11
fi
log "  OK ($snap_bytes bytes, logger.db valid SQLite)"
report_line "- **Safety gate**: OK — $snap_bytes bytes, logger.db is a valid SQLite DB"

log "Rotating snapshots (keeping $KEEP_SNAPSHOTS)"
# shellcheck disable=SC2012
STALE=$(ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | tail -n +"$((KEEP_SNAPSHOTS + 1))")
if [ -n "$STALE" ]; then
  echo "$STALE" | xargs rm -rf
  removed=$(echo "$STALE" | wc -l | tr -d ' ')
  log "  Removed $removed old snapshot(s)"
  report_line "- **Rotation**: removed $removed snapshot(s), kept $KEEP_SNAPSHOTS"
else
  log "  Nothing to rotate"
  report_line "- **Rotation**: nothing to rotate, kept $KEEP_SNAPSHOTS"
fi

log "Backup complete — $SNAP ($(human_size "$SNAP"))"
exit 0
