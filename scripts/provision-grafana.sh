#!/usr/bin/env bash
# provision-grafana.sh — Deploy sailing dashboards and datasource provisioning.
# Idempotent: safe to run multiple times.
#
# NOTE: Auth and network binding for Grafana are managed via systemd env vars
# in /etc/systemd/system/grafana-server.service.d/port.conf (set by setup.sh).
# This script does NOT touch grafana.ini or auth settings.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_SRC="$SCRIPT_DIR/grafana/sailing-data.json"
PI_HEALTH_SRC="$SCRIPT_DIR/grafana/pi-health.json"
PROVISION_SRC="$SCRIPT_DIR/grafana/dashboards.yaml"
DATASOURCE_SRC="$SCRIPT_DIR/grafana/datasources.yaml"

DASHBOARD_DEST_DIR="/var/lib/grafana/dashboards"
PROVISION_DEST="/etc/grafana/provisioning/dashboards/j105-logger.yaml"
DATASOURCE_DEST="/etc/grafana/provisioning/datasources/j105-logger.yaml"

echo "==> Copying dashboard JSONs"
sudo rsync --mkpath "$DASHBOARD_SRC" "$DASHBOARD_DEST_DIR/sailing-data.json"
sudo rsync "$PI_HEALTH_SRC" "$DASHBOARD_DEST_DIR/pi-health.json"

echo "==> Copying dashboard provisioning config"
sudo rsync "$PROVISION_SRC" "$PROVISION_DEST"

echo "==> Copying datasource provisioning config"
sudo rsync "$DATASOURCE_SRC" "$DATASOURCE_DEST"

echo "==> Restarting grafana-server"
sudo systemctl restart grafana-server

echo ""
echo "Done."
echo "  Sailing data: http://$(hostname):3001/d/j105-sailing/sailing-data"
echo "  Pi health:    http://$(hostname):3001/d/pi-health/pi-health"
