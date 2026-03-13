#!/usr/bin/env bash
# reset-pi.sh — Reverse everything setup.sh does, restoring the Pi to pre-setup state.
#
# Usage:
#   ./scripts/reset-pi.sh --confirm
#
# Without --confirm, prints what would be removed and exits.
# Requires sudo for package removal and systemd cleanup.
#
# What this script does NOT touch:
#   - SSH authorized_keys or SSH hardening (safe to keep)
#   - The git repository itself (just resets to origin/main)
#   - User's home directory structure beyond helmlog-specific artifacts
#
# After running this, you can re-run setup.sh from scratch.

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CURRENT_USER="$(id -un)"
ENV_FILE="$PROJECT_DIR/.env"
INFLUX_TOKEN_FILE="$HOME/influx-token.txt"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

step()  { echo -e "\n${RED}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }
info()  { echo -e "${CYAN}    $*${NC}"; }

# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------

DRY_RUN=true
if [[ "${1:-}" == "--confirm" ]]; then
    DRY_RUN=false
fi

if $DRY_RUN; then
    echo -e "${YELLOW}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  DRY RUN — showing what would be removed        ║${NC}"
    echo -e "${YELLOW}║  Pass --confirm to actually execute              ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "Services to stop and remove:"
    echo "  - helmlog, signalk, can-interface"
    echo "  - influxdb, grafana-server"
    echo "  - loki, promtail"
    echo "  - nginx (helmlog site config removed; default restored)"
    echo ""
    echo "Packages to remove:"
    echo "  - influxdb2, grafana, loki, promtail, nginx"
    echo "  - nodejs (NodeSource)"
    echo "  - signalk-server (npm global)"
    echo ""
    echo "Files and directories to remove:"
    echo "  - /etc/systemd/system/helmlog.service"
    echo "  - /etc/systemd/system/signalk.service"
    echo "  - /etc/systemd/system/can-interface.service"
    echo "  - /etc/systemd/system/grafana-server.service.d/"
    echo "  - /etc/systemd/system/loki.service.d/"
    echo "  - /etc/systemd/system/promtail.service.d/"
    echo "  - /etc/nginx/sites-available/helmlog"
    echo "  - /etc/nginx/sites-enabled/helmlog"
    echo "  - /etc/loki/, /etc/promtail/"
    echo "  - /var/lib/loki/, /var/lib/promtail/"
    echo "  - /etc/grafana/grafana-custom.ini"
    echo "  - /etc/grafana/provisioning/datasources/influxdb.yaml"
    echo "  - /etc/sudoers.d/helmlog-allowed"
    echo "  - /etc/apt/sources.list.d/influxdata.list"
    echo "  - /etc/apt/sources.list.d/grafana.list"
    echo "  - /etc/apt/trusted.gpg.d/influxdata-archive.gpg"
    echo "  - /etc/apt/keyrings/grafana.gpg"
    echo "  - /var/cache/helmlog/"
    echo "  - ~/.signalk/"
    echo "  - ~/.signalk-admin-pass.txt"
    echo "  - ~/influx-token.txt"
    echo "  - ~/.local/bin/helmlog (wrapper)"
    echo "  - $ENV_FILE"
    echo "  - $PROJECT_DIR/data/"
    echo "  - $PROJECT_DIR/.venv/"
    echo ""
    echo "Git repo will be reset to origin/main (hard reset + clean)."
    echo ""
    echo "helmlog system account will be removed."
    echo ""
    echo -e "${YELLOW}Run with --confirm to execute.${NC}"
    exit 0
fi

echo -e "${RED}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║  RESETTING Pi to pre-setup state                ║${NC}"
echo -e "${RED}║  This is destructive and cannot be undone!       ║${NC}"
echo -e "${RED}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# 1) Stop all helmlog-related services
# ---------------------------------------------------------------------------

step "Stopping services..."
SERVICES=(helmlog signalk can-interface)
for svc in "${SERVICES[@]}"; do
    if systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -qw "${svc}\.service"; then
        sudo systemctl stop "${svc}" 2>/dev/null || true
        sudo systemctl disable "${svc}" 2>/dev/null || true
        info "Stopped and disabled ${svc}"
    fi
done

# ---------------------------------------------------------------------------
# 2) Remove helmlog systemd unit files
# ---------------------------------------------------------------------------

step "Removing helmlog systemd units..."
for unit in helmlog.service signalk.service can-interface.service; do
    if [[ -f "/etc/systemd/system/${unit}" ]]; then
        sudo rm -f "/etc/systemd/system/${unit}"
        info "Removed /etc/systemd/system/${unit}"
    fi
done

# ---------------------------------------------------------------------------
# 3) Remove nginx helmlog config; restore default site
# ---------------------------------------------------------------------------

step "Removing nginx helmlog config..."
sudo rm -f /etc/nginx/sites-enabled/helmlog
sudo rm -f /etc/nginx/sites-available/helmlog
# Restore default site if nginx is still installed
if [[ -f /etc/nginx/sites-available/default ]]; then
    sudo ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default 2>/dev/null || true
fi
if command -v nginx &>/dev/null; then
    sudo nginx -t 2>/dev/null && sudo systemctl reload nginx 2>/dev/null || true
fi
info "nginx helmlog site removed."

# ---------------------------------------------------------------------------
# 4) Remove Loki + Promtail
# ---------------------------------------------------------------------------

step "Removing Loki + Promtail..."
for svc in promtail loki; do
    sudo systemctl stop "${svc}" 2>/dev/null || true
    sudo systemctl disable "${svc}" 2>/dev/null || true
done
sudo apt-get remove --purge -y loki promtail 2>/dev/null || true
sudo rm -rf /etc/loki /etc/promtail
sudo rm -rf /var/lib/loki /var/lib/promtail
sudo rm -rf /etc/systemd/system/loki.service.d
sudo rm -rf /etc/systemd/system/promtail.service.d
info "Loki + Promtail removed."

# ---------------------------------------------------------------------------
# 5) Remove nginx package
# ---------------------------------------------------------------------------

step "Removing nginx..."
sudo systemctl stop nginx 2>/dev/null || true
sudo systemctl disable nginx 2>/dev/null || true
sudo apt-get remove --purge -y nginx nginx-common 2>/dev/null || true
info "nginx removed."

# ---------------------------------------------------------------------------
# 6) Remove sudo rules
# ---------------------------------------------------------------------------

step "Removing scoped sudo rules..."
sudo rm -f /etc/sudoers.d/helmlog-allowed
info "Removed /etc/sudoers.d/helmlog-allowed"

# Restore blanket NOPASSWD if the Pi OS files were modified by setup.sh.
# We can't know the original content, so just note it.
warn "If setup.sh removed blanket NOPASSWD from /etc/sudoers.d/010_pi-nopasswd,"
warn "you may need to restore it manually or re-image if sudo requires a password."

# ---------------------------------------------------------------------------
# 7) Remove Grafana
# ---------------------------------------------------------------------------

step "Removing Grafana..."
sudo systemctl stop grafana-server 2>/dev/null || true
sudo systemctl disable grafana-server 2>/dev/null || true
sudo apt-get remove --purge -y grafana 2>/dev/null || true
sudo rm -rf /etc/systemd/system/grafana-server.service.d
sudo rm -f /etc/grafana/grafana-custom.ini
sudo rm -f /etc/grafana/provisioning/datasources/influxdb.yaml
sudo rm -f /etc/apt/sources.list.d/grafana.list
sudo rm -f /etc/apt/keyrings/grafana.gpg
info "Grafana removed."

# ---------------------------------------------------------------------------
# 8) Remove InfluxDB
# ---------------------------------------------------------------------------

step "Removing InfluxDB..."
sudo systemctl stop influxdb 2>/dev/null || true
sudo systemctl disable influxdb 2>/dev/null || true
sudo apt-mark unhold influxdb2 2>/dev/null || true
sudo apt-get remove --purge -y influxdb2 2>/dev/null || true
sudo rm -f /etc/apt/sources.list.d/influxdata.list
sudo rm -f /etc/apt/trusted.gpg.d/influxdata-archive.gpg
info "InfluxDB removed."

# ---------------------------------------------------------------------------
# 9) Remove Signal K
# ---------------------------------------------------------------------------

step "Removing Signal K Server..."
sudo npm uninstall -g signalk-server 2>/dev/null || true
rm -rf "$HOME/.signalk"
rm -f "$HOME/.signalk-admin-pass.txt"
info "Signal K removed."

# ---------------------------------------------------------------------------
# 10) Remove Node.js (NodeSource)
# ---------------------------------------------------------------------------

step "Removing Node.js..."
sudo apt-get remove --purge -y nodejs 2>/dev/null || true
sudo rm -f /etc/apt/sources.list.d/nodesource.list
# NodeSource setup_24.x may have left a keyring file
sudo rm -f /etc/apt/keyrings/nodesource.gpg 2>/dev/null || true
sudo rm -f /usr/share/keyrings/nodesource.gpg 2>/dev/null || true
info "Node.js removed."

# ---------------------------------------------------------------------------
# 11) Remove helmlog service account
# ---------------------------------------------------------------------------

step "Removing helmlog service account..."
if id helmlog &>/dev/null; then
    sudo userdel helmlog 2>/dev/null || true
    info "Removed helmlog system account."
else
    info "helmlog account does not exist — skipping."
fi
sudo rm -rf /var/cache/helmlog

# ---------------------------------------------------------------------------
# 12) Remove helmlog wrapper and local artifacts
# ---------------------------------------------------------------------------

step "Removing helmlog local artifacts..."
rm -f "$HOME/.local/bin/helmlog"
rm -f "$INFLUX_TOKEN_FILE"
info "Removed helmlog wrapper and influx token file."

# ---------------------------------------------------------------------------
# 13) Remove .env, data dir, and venv
# ---------------------------------------------------------------------------

step "Removing .env, data directory, and venv..."
rm -f "$ENV_FILE"
sudo rm -rf "$PROJECT_DIR/data"
sudo rm -rf "$PROJECT_DIR/.venv"
info "Removed .env, data/, and .venv/"

# ---------------------------------------------------------------------------
# 14) Clean up apt cache
# ---------------------------------------------------------------------------

step "Cleaning apt cache..."
sudo apt-get autoremove -y 2>/dev/null || true
sudo apt-get update -qq 2>/dev/null || true
sudo systemctl daemon-reload
info "apt cleaned up and systemd reloaded."

# ---------------------------------------------------------------------------
# 15) Hard-reset git repo to origin/main
# ---------------------------------------------------------------------------

step "Resetting git repo to origin/main..."
cd "$PROJECT_DIR"
# Remove __pycache__ dirs that may be owned by the helmlog service account
sudo find "$PROJECT_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
git fetch origin
git checkout main 2>/dev/null || git checkout -b main origin/main
git reset --hard origin/main
git clean -fdx
info "Git repo reset to origin/main."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Reset complete.${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "  The Pi is now in a pre-setup state."
echo "  To set up again, run:"
echo ""
echo "    ./scripts/setup.sh"
echo ""
echo -e "${YELLOW}  NOTE: If sudo now requires a password for all commands,${NC}"
echo -e "${YELLOW}  and you don't know it, you may need to re-image the SD card.${NC}"
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
