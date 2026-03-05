# Backup Guide

The logger stores all data on a microSD card inside the Raspberry Pi.
This guide covers how to pull a complete backup to your Mac automatically,
and how to restore from it if the card fails.

> **Multiple Pis?** All scripts accept a `PI` environment variable for the SSH
> target (e.g. `PI=weaties@testpi ./scripts/backup.sh`). The default is
> `weaties@corvopi`. Examples below use the placeholder `<pi-user>@<pi-host>` —
> substitute your actual SSH target.

---

## What gets backed up

| Source | Destination in snapshot |
|---|---|
| `~/j105-logger/data/` — SQLite DB, WAV audio, photo notes, exports | `<snapshot>/data/` |
| InfluxDB (system health metrics) | `<snapshot>/influxdb/` |
| Grafana (dashboards, datasources) | `<snapshot>/grafana/` |

Snapshots land in `~/backups/j105-logger/<timestamp>/`.
The 10 most recent are kept; older ones are deleted automatically.

---

## First-time setup

### 1. Prerequisites on the Mac

```bash
# SSH key-based auth to the Pi (skip if already done)
ssh-keygen -t ed25519 -C "mac-to-pi-backup"
ssh-copy-id <pi-user>@<pi-host>

# InfluxDB CLI (for restore; backup is handled on the Pi)
brew install influxdb-cli
```

### 2. Pi: allow sudo rsync without a password (for Grafana backup)

SSH into the Pi and add a sudoers rule:

```bash
ssh <pi-user>@<pi-host>
echo '<pi-user> ALL=(ALL) NOPASSWD: /usr/bin/rsync' | sudo tee /etc/sudoers.d/rsync-backup
sudo chmod 440 /etc/sudoers.d/rsync-backup
```

### 3. Install the daily launchd agent on the Mac

From the project root:

```bash
./scripts/setup-backup-mac.sh
```

This:
- Checks SSH connectivity to the Pi
- Creates `~/backups/j105-logger/`
- Installs `~/Library/LaunchAgents/com.j105.backup.plist`
- Schedules the backup to run every day at **03:00 local time**

### 4. Run a test backup now

```bash
./scripts/backup.sh
```

Verify the snapshot contains:

```
~/backups/j105-logger/20260228T030000Z/
  data/
    logger.db
    audio/
    notes/
    exports/
  influxdb/
  grafana/
```

---

## Monitoring backups

Logs are written to `~/backups/j105-logger/backup.log`:

```bash
tail -f ~/backups/j105-logger/backup.log
```

To check that the launchd agent is active:

```bash
launchctl list com.j105.backup
```

To trigger an immediate run:

```bash
launchctl start com.j105.backup
```

---

## Restoring from a backup

Pick the snapshot you want:

```bash
ls ~/backups/j105-logger/
# e.g. 20260228T030000Z
SNAP=~/backups/j105-logger/20260228T030000Z
```

### SQLite

```bash
# Stop the logger service on the Pi first
ssh $PI "sudo systemctl stop j105-logger"

# Copy the DB back
rsync -az "$SNAP/data/" $PI:~/j105-logger/data/

# Restart
ssh $PI "sudo systemctl start j105-logger"
```

### InfluxDB

```bash
# Restore to the running InfluxDB instance (overwrites existing data)
influx restore "$SNAP/influxdb/" \
  --host http://<pi-host>:8086 \
  --token "$(cat ~/influx-token.txt)"
```

If restoring to a fresh InfluxDB install, add `--full` to wipe and replace all buckets.

### Grafana

```bash
# Stop Grafana on the Pi
ssh $PI "sudo systemctl stop grafana-server"

# Restore the data directory
rsync -az --rsync-path='sudo rsync' \
  "$SNAP/grafana/" \
  $PI:/var/lib/grafana/

# Fix ownership and restart
ssh $PI "sudo chown -R grafana:grafana /var/lib/grafana && sudo systemctl start grafana-server"
```

---

## Configuration

All settings are overridable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `PI` | `weaties@corvopi` | SSH target for the Pi |
| `BACKUP_DEST` | `~/backups/j105-logger` | Local snapshot root |
| `KEEP_SNAPSHOTS` | `10` | Number of snapshots to retain |
| `INFLUX_TOKEN_FILE` | `~/influx-token.txt` | Path on the Pi to the InfluxDB token |

Example — keep 30 days of snapshots:

```bash
KEEP_SNAPSHOTS=30 ./scripts/backup.sh
```

---

## Changing the backup schedule

Edit `~/Library/LaunchAgents/com.j105.backup.plist` and change the
`StartCalendarInterval` hour/minute, then reload:

```bash
launchctl unload ~/Library/LaunchAgents/com.j105.backup.plist
launchctl load   ~/Library/LaunchAgents/com.j105.backup.plist
```

---

## Disabling the backup

```bash
launchctl unload ~/Library/LaunchAgents/com.j105.backup.plist
rm ~/Library/LaunchAgents/com.j105.backup.plist
```
