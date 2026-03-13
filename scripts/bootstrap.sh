#!/usr/bin/env bash
# bootstrap.sh — One-command setup for HelmLog on a fresh Raspberry Pi.
#
# Paste this on a newly imaged Pi and walk away:
#
#   curl -fsSL https://raw.githubusercontent.com/weaties/helmlog/main/scripts/bootstrap.sh \
#     | ADMIN_EMAIL=you@example.com bash
#
# When you come back, HelmLog will be running with an admin account ready to log in.
#
# Required environment variables:
#   ADMIN_EMAIL      — email address for the first admin account
#
# Optional environment variables:
#   HELMLOG_BRANCH   — git branch to deploy (default: main)
#   HELMLOG_DIR      — clone destination (default: ~/helmlog)
#   HELMLOG_REPO     — git clone URL (default: git@github.com:weaties/helmlog.git)
#                      For HTTPS: https://github.com/weaties/helmlog.git
#
# Prerequisites:
#   - Fresh Raspberry Pi OS (64-bit, Bookworm or later)
#   - SSH access with a non-root user that has sudo
#   - SSH key or HTTPS credentials for GitHub (private repo)
#   - Internet connection

# Wrap everything in a function so the entire script is downloaded before
# execution begins (safe for curl | bash).
bootstrap() {
    set -euo pipefail

    # -------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------

    ADMIN_EMAIL="${ADMIN_EMAIL:-}"
    HELMLOG_BRANCH="${HELMLOG_BRANCH:-main}"
    HELMLOG_DIR="${HELMLOG_DIR:-$HOME/helmlog}"
    HELMLOG_REPO="${HELMLOG_REPO:-git@github.com:weaties/helmlog.git}"

    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    NC='\033[0m'

    step()  { echo -e "\n${GREEN}==> $*${NC}"; }
    warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }
    err()   { echo -e "${RED}ERROR:${NC} $*" >&2; }
    info()  { echo -e "${CYAN}    $*${NC}"; }

    # -------------------------------------------------------------------
    # Pre-flight checks
    # -------------------------------------------------------------------

    if [[ -z "$ADMIN_EMAIL" ]]; then
        err "ADMIN_EMAIL is required."
        echo ""
        echo "Usage:"
        echo "  curl -fsSL <url>/bootstrap.sh | ADMIN_EMAIL=you@example.com bash"
        return 1
    fi

    if [[ "$(uname -s)" != "Linux" ]]; then
        err "This script is for Raspberry Pi (Linux). Detected: $(uname -s)"
        return 1
    fi

    if [[ "$(id -u)" -eq 0 ]]; then
        err "Do not run as root. Run as your normal Pi user (e.g. weaties)."
        return 1
    fi

    if ! sudo -n true 2>/dev/null; then
        err "Passwordless sudo required for unattended install."
        err "On a fresh Pi OS image this is the default. If you changed it,"
        err "re-enable it temporarily or run setup.sh interactively instead."
        return 1
    fi

    step "HelmLog bootstrap starting..."
    info "Admin email: ${ADMIN_EMAIL}"
    info "Branch:      ${HELMLOG_BRANCH}"
    info "Directory:   ${HELMLOG_DIR}"
    info "Repository:  ${HELMLOG_REPO}"

    # -------------------------------------------------------------------
    # 1. Install git if missing
    # -------------------------------------------------------------------

    step "Ensuring git is installed..."
    if ! command -v git &>/dev/null; then
        sudo apt-get update -qq
        sudo apt-get install -y git
    else
        info "git $(git --version | awk '{print $3}') already installed."
    fi

    # -------------------------------------------------------------------
    # 2. Clone the repository
    # -------------------------------------------------------------------

    step "Cloning helmlog repository..."
    if [[ -d "$HELMLOG_DIR/.git" ]]; then
        info "Repository already exists at ${HELMLOG_DIR} — pulling latest..."
        cd "$HELMLOG_DIR"
        git fetch origin
        git checkout "$HELMLOG_BRANCH"
        git pull origin "$HELMLOG_BRANCH"
    else
        git clone --branch "$HELMLOG_BRANCH" "$HELMLOG_REPO" "$HELMLOG_DIR"
        cd "$HELMLOG_DIR"
    fi
    info "On branch: $(git rev-parse --abbrev-ref HEAD) ($(git rev-parse --short HEAD))"

    # -------------------------------------------------------------------
    # 3. Run setup.sh (installs everything — 10-20 minutes)
    # -------------------------------------------------------------------

    step "Running setup.sh (this takes 10–20 minutes on a Pi 4/5)..."
    bash "$HELMLOG_DIR/scripts/setup.sh"

    # -------------------------------------------------------------------
    # 4. Set ADMIN_EMAIL in .env for future deploys
    # -------------------------------------------------------------------

    ENV_FILE="$HELMLOG_DIR/.env"
    if [[ -f "$ENV_FILE" ]]; then
        if grep -q '^# ADMIN_EMAIL=' "$ENV_FILE"; then
            sed -i "s|^# ADMIN_EMAIL=.*|ADMIN_EMAIL=${ADMIN_EMAIL}|" "$ENV_FILE"
            info "ADMIN_EMAIL set in .env"
        elif ! grep -q '^ADMIN_EMAIL=' "$ENV_FILE"; then
            echo "ADMIN_EMAIL=${ADMIN_EMAIL}" >> "$ENV_FILE"
            info "ADMIN_EMAIL appended to .env"
        fi
    fi

    # -------------------------------------------------------------------
    # 5. Create admin user
    # -------------------------------------------------------------------

    step "Creating admin user..."

    # Resolve uv
    UV_BIN=""
    if command -v uv &>/dev/null; then
        UV_BIN="$(command -v uv)"
    elif [[ -x "$HOME/.local/bin/uv" ]]; then
        UV_BIN="$HOME/.local/bin/uv"
    fi

    if [[ -z "$UV_BIN" ]]; then
        err "uv not found after setup.sh — something went wrong."
        return 1
    fi

    # Ensure helmlog is running — setup.sh may have started it earlier, but
    # subsequent daemon-reload / apt activity can stop it before we get here.
    if ! sudo systemctl is-active --quiet helmlog.service 2>/dev/null; then
        info "helmlog not running — restarting..."
        sudo systemctl restart helmlog.service
    fi

    # Wait for helmlog to create/migrate the DB
    DB_FILE="$HELMLOG_DIR/data/logger.db"
    info "Waiting for database..."
    for _i in {1..30}; do
        [[ -f "$DB_FILE" ]] && break
        sleep 2
    done

    if [[ ! -f "$DB_FILE" ]]; then
        warn "Database not found after 60s — helmlog may not have started."
        warn "Check: sudo journalctl -u helmlog --no-pager -n 20"
    fi

    # Create the admin user and capture the login URL.
    # Must run as helmlog user so any DB writes have correct ownership.
    # cd into HELMLOG_DIR so the relative DB path (data/logger.db) resolves correctly.
    ADD_USER_OUTPUT=$(cd "$HELMLOG_DIR" && sudo -u helmlog \
        env UV_CACHE_DIR=/var/cache/helmlog HOME=/var/cache/helmlog \
        "$UV_BIN" run --no-sync --project "$HELMLOG_DIR" helmlog add-user \
        --email "$ADMIN_EMAIL" --name "Admin" --role admin 2>&1) || true

    LOGIN_URL=$(echo "$ADD_USER_OUTPUT" | grep -oE 'http[s]?://[^ ]+\?token=[^ ]+' | head -1) || true

    # -------------------------------------------------------------------
    # 6. Summary
    # -------------------------------------------------------------------

    # Final safety net — make sure helmlog is running before we declare victory.
    if ! sudo systemctl is-active --quiet helmlog.service 2>/dev/null; then
        sudo systemctl restart helmlog.service
    fi

    PI_HOSTNAME="$(hostname)"

    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  HelmLog bootstrap complete!${NC}"
    echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
    echo ""
    echo "  HelmLog is running at: http://${PI_HOSTNAME}/"
    echo ""
    if [[ -n "$LOGIN_URL" ]]; then
        # Replace localhost/127.0.0.1 with the Pi hostname for remote access
        REMOTE_URL=$(echo "$LOGIN_URL" | sed "s|localhost|${PI_HOSTNAME}|; s|127\.0\.0\.1|${PI_HOSTNAME}|")
        echo -e "${GREEN}  Admin login URL (paste into your browser):${NC}"
        echo ""
        echo "    ${REMOTE_URL}"
        echo ""
        echo "  Token expires in 7 days. After login, the session lasts 90 days."
    else
        warn "Could not extract login URL. Create admin manually:"
        echo "    cd ~/helmlog && helmlog add-user --email ${ADMIN_EMAIL} --role admin"
        if [[ -n "$ADD_USER_OUTPUT" ]]; then
            echo ""
            echo "  add-user output:"
            echo "$ADD_USER_OUTPUT" | sed 's/^/    /'
        fi
    fi
    echo ""
    echo "  Services:"
    echo "    http://${PI_HOSTNAME}/           HelmLog (race marker, history, exports)"
    echo "    http://${PI_HOSTNAME}/grafana/   Grafana dashboards"
    echo "    http://${PI_HOSTNAME}/signalk/   Signal K data explorer"
    echo ""
    echo "  Next: reboot to verify all services start cleanly:"
    echo "    sudo reboot"
    echo ""
    echo "  After reboot, check status:"
    echo "    sudo systemctl status helmlog signalk influxdb grafana-server"
    echo ""
    echo "  To update after a git pull:"
    echo "    cd ~/helmlog && ./scripts/deploy.sh"
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
}

bootstrap "$@"
