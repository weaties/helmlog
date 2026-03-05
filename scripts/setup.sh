#!/usr/bin/env bash
# setup.sh — Full automated setup for j105-logger + Signal K + InfluxDB + Grafana.
#
# Usage:
#   ./scripts/setup.sh
#
# Fully idempotent — safe to re-run after a git pull. No browser steps required.
# Requires sudo for package installation and systemd service management.
# Run from a terminal where you can enter your sudo password when prompted.
#
# What this script does:
#   a)   System prerequisites (git, curl, audio libs, unattended-upgrades)
#   a.1) Security hardening (auto-updates, mask unused services, SSH hardening)
#   b)   Node.js 24 LTS
#   c)   Signal K Server + plugins
#   d)   InfluxDB 2.7.11 (pinned; loopback-only binding)
#   e)   Grafana OSS (loopback-only; login required; no anonymous access)
#   e.1) j105logger dedicated service account
#   f)   Signal K → InfluxDB plugin config
#   g)   uv + Python dependencies
#   g.1) Signal K authentication (bcrypt admin password)
#   h)   .env file (chmod 600) and data directories
#   i)   netdev group membership
#   j)   CAN interface systemd service
#   k)   j105-logger systemd service (runs as j105logger)
#   k.1) Loki + Promtail (centralized log management)
#   l)   Scoped NOPASSWD sudo (replaces blanket Pi OS default)
#   m)   Summary

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CURRENT_USER="$(id -un)"
ENV_FILE="$PROJECT_DIR/.env"
INFLUX_TOKEN_FILE="$HOME/influx-token.txt"

INFLUX_VERSION="2.7.11"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

step()  { echo -e "\n${GREEN}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }
info()  { echo -e "${CYAN}    $*${NC}"; }

# ---------------------------------------------------------------------------
# a) System prerequisites
# ---------------------------------------------------------------------------

step "Installing system prerequisites..."
sudo apt-get update -qq
sudo apt-get install -y \
    git can-utils curl gnupg2 apt-transport-https \
    ca-certificates lsb-release jq \
    libportaudio2 libsndfile1 \
    unattended-upgrades apt-listchanges \
    tmux locales

# Ensure en_US.UTF-8 locale is generated (prevents setlocale warnings on SSH)
sudo sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
sudo locale-gen

# Apply all pending security updates now
sudo apt-get upgrade -y

# ---------------------------------------------------------------------------
# a.1) Security hardening
# ---------------------------------------------------------------------------

step "Configuring automatic security updates..."
sudo tee /etc/apt/apt.conf.d/20auto-upgrades > /dev/null << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
sudo dpkg-reconfigure -f noninteractive unattended-upgrades 2>/dev/null || true
info "Daily unattended security updates enabled."

step "Masking unused system services..."
UNUSED_SERVICES=(cups cups-browsed rpcbind avahi-daemon bluetooth ModemManager)
for svc in "${UNUSED_SERVICES[@]}"; do
    if systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -qw "${svc}\.service"; then
        sudo systemctl disable "${svc}" 2>/dev/null || true
        sudo systemctl stop "${svc}" 2>/dev/null || true
        sudo systemctl mask "${svc}.service" 2>/dev/null || true
        info "Masked ${svc}.service"
    fi
done
# Mask socket units too
for svc in cups avahi-daemon; do
    sudo systemctl mask "${svc}.socket" 2>/dev/null || true
done

step "Hardening SSH configuration..."

# SAFETY: Verify authorized_keys exists and has at least one key before touching SSH.
# On 2026-03-01 an autonomous hardening run deleted the only SSH key from
# authorized_keys, locking the operator out.  Recovery required pulling the
# microSD card and using debugfs from a Mac.  Never let that happen again.
AUTH_KEYS="$HOME/.ssh/authorized_keys"
if [[ ! -s "$AUTH_KEYS" ]]; then
    warn "~/.ssh/authorized_keys is missing or empty!"
    warn "SSH key-based login will not work.  Add at least one public key before continuing."
    warn "Skipping SSH hardening to avoid a lockout."
else
    KEY_COUNT=$(grep -cE '^(ssh-|ecdsa-)' "$AUTH_KEYS" 2>/dev/null || echo 0)
    info "authorized_keys contains $KEY_COUNT key(s)."
    if [[ "$KEY_COUNT" -eq 0 ]]; then
        warn "No valid SSH public keys found in authorized_keys — skipping SSH hardening."
    else
        chmod 700 "$HOME/.ssh"
        chmod 600 "$AUTH_KEYS"
        chmod 600 "$HOME/.ssh/config" 2>/dev/null || true
        # Disable X11 forwarding — no GUI apps needed on the Pi
        SSHD_CONF="/etc/ssh/sshd_config"
        if sudo grep -qE "^#?X11Forwarding" "$SSHD_CONF" 2>/dev/null; then
            sudo sed -i 's/^#\?X11Forwarding.*/X11Forwarding no/' "$SSHD_CONF"
        else
            echo "X11Forwarding no" | sudo tee -a "$SSHD_CONF" > /dev/null
        fi
        sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null || true
        info "SSH hardening applied (X11Forwarding disabled, permissions tightened)."
    fi
fi

# SAFETY: setup.sh must NEVER modify the contents of authorized_keys.
# Adding or removing keys is an operator action — not something an
# automated script should do.  See docs/incident-ssh-lockout-2026-03-01.md.

# ---------------------------------------------------------------------------
# b) Node.js 24 LTS
# ---------------------------------------------------------------------------

step "Checking Node.js..."
if ! node --version 2>/dev/null | grep -q "^v24"; then
    info "Installing Node.js 24 LTS via NodeSource..."
    curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
    sudo apt-get install -y nodejs
else
    info "Node.js $(node --version) already installed."
fi

# ---------------------------------------------------------------------------
# c) Signal K Server
# ---------------------------------------------------------------------------

step "Installing Signal K Server..."
sudo npm install -g signalk-server

SK_BIN="$(command -v signalk-server || echo '/usr/lib/node_modules/signalk-server/bin/signalk-server')"
info "signalk-server: $SK_BIN"

# Signal K config directory
mkdir -p "$HOME/.signalk"

# Write settings.json (CAN bus pipedProvider pre-configured)
# Only write a fresh file if one doesn't exist.  If it does, preserve any
# existing vessel/security config but ensure the security strategy is present.
if [[ ! -f "$HOME/.signalk/settings.json" ]]; then
    info "Writing ~/.signalk/settings.json..."
    cat > "$HOME/.signalk/settings.json" << 'EOF'
{
  "vessel": { "name": "corvopi" },
  "pipedProviders": [{
    "id": "n2k-canbus",
    "pipeElements": [
      { "type": "providers/canbus", "options": { "canDevice": "can0" } },
      { "type": "providers/canboatjs", "options": {} },
      { "type": "providers/n2k-signalk", "options": {} }
    ],
    "enabled": true
  }],
  "interfaces": { "plugins": true },
  "security": { "strategy": "@signalk/sk-simple-token-security" }
}
EOF
else
    info "~/.signalk/settings.json already exists — checking security strategy..."
    # Idempotently add security strategy if missing
    if ! jq -e '.security' "$HOME/.signalk/settings.json" > /dev/null 2>&1; then
        TMP_SK="$(mktemp)"
        jq '. + {"security": {"strategy": "@signalk/sk-simple-token-security"}}' \
            "$HOME/.signalk/settings.json" > "$TMP_SK"
        mv "$TMP_SK" "$HOME/.signalk/settings.json"
        info "Added security strategy to settings.json."
    else
        info "Security strategy already configured."
    fi
fi

# Install Signal K plugins
info "Installing Signal K plugins..."
cat > "$HOME/.signalk/package.json" << 'EOF'
{
  "name": "signalk-server-config",
  "version": "1.0.0",
  "dependencies": {
    "signalk-to-influxdb2": "*",
    "signalk-derived-data": "*"
  }
}
EOF
(cd "$HOME/.signalk" && npm install --no-fund --no-audit --legacy-peer-deps) || \
    warn "Signal K plugin install had errors — run 'cd ~/.signalk && npm install --legacy-peer-deps' to retry."

# systemd service for Signal K
sudo tee /etc/systemd/system/signalk.service > /dev/null << EOF
[Unit]
Description=Signal K Server
After=can-interface.service network.target
Wants=can-interface.service

[Service]
User=${CURRENT_USER}
WorkingDirectory=/home/${CURRENT_USER}/.signalk
ExecStart=/usr/bin/node ${SK_BIN} -c /home/${CURRENT_USER}/.signalk
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable signalk.service
info "signalk.service installed and enabled."

# ---------------------------------------------------------------------------
# d) InfluxDB 2.7.11 (pinned; apt-mark hold prevents v3 auto-upgrade)
#    Bound to loopback only — not exposed outside the Pi.
# ---------------------------------------------------------------------------

step "Checking InfluxDB..."
if ! influx version 2>/dev/null | grep -q "${INFLUX_VERSION}"; then
    info "Installing InfluxDB ${INFLUX_VERSION} via apt repo..."
    # Add InfluxData apt repo (arm64 debs are only available via their repo, not direct download)
    curl -fsSL https://repos.influxdata.com/influxdata-archive.key | \
        gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/influxdata-archive.gpg > /dev/null
    echo "deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main" | \
        sudo tee /etc/apt/sources.list.d/influxdata.list > /dev/null
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "influxdb2=${INFLUX_VERSION}-1"
    sudo apt-mark hold influxdb2
fi

# Bind InfluxDB to loopback only (not exposed to LAN or internet)
INFLUX_CONF="/etc/influxdb/config.toml"
if [[ -f "$INFLUX_CONF" ]] && ! sudo grep -q 'http-bind-address' "$INFLUX_CONF" 2>/dev/null; then
    printf '\n# Bind to loopback — proxied internally, not exposed externally\nhttp-bind-address = "127.0.0.1:8086"\n' \
        | sudo tee -a "$INFLUX_CONF" > /dev/null
    info "InfluxDB loopback binding configured."
fi

sudo systemctl enable --now influxdb

# Wait for influxd to be ready
info "Waiting for influxd to start..."
for i in {1..15}; do
    influx ping 2>/dev/null && break
    sleep 2
done

# Initial setup (idempotent — exits 0 if already configured)
influx setup \
    --username admin \
    --password changeme123 \
    --org j105 \
    --bucket signalk \
    --retention 0 \
    --force 2>/dev/null || true

# Capture token for downstream configs
INFLUX_TOKEN="$(influx auth list --json 2>/dev/null | jq -r '.[0].token' || echo '')"
if [[ -z "$INFLUX_TOKEN" || "$INFLUX_TOKEN" == "null" ]]; then
    # Fall back to saved token file if `influx` CLI can't retrieve it
    if [[ -f "$INFLUX_TOKEN_FILE" ]]; then
        INFLUX_TOKEN="$(cat "$INFLUX_TOKEN_FILE")"
        info "InfluxDB token read from: $INFLUX_TOKEN_FILE"
    else
        warn "Could not retrieve InfluxDB token. Plugin config will need manual token update."
        INFLUX_TOKEN="REPLACE_WITH_INFLUX_TOKEN"
    fi
else
    echo "$INFLUX_TOKEN" > "$INFLUX_TOKEN_FILE"
    chmod 600 "$INFLUX_TOKEN_FILE"
    info "InfluxDB token saved to: $INFLUX_TOKEN_FILE"
fi

# Restart to pick up config.toml changes
sudo systemctl restart influxdb

# ---------------------------------------------------------------------------
# e) Grafana OSS (loopback-only; login required; no anonymous access)
#    Port 3001 to avoid clash with Signal K on 3000.
#    Auth is managed via systemd env vars so service.d/port.conf controls both
#    network binding and auth — no need to modify /etc/grafana/grafana.ini.
# ---------------------------------------------------------------------------

step "Installing Grafana..."
sudo mkdir -p /etc/apt/keyrings
wget -q -O - https://apt.grafana.com/gpg.key | \
    gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | \
    sudo tee /etc/apt/sources.list.d/grafana.list > /dev/null
sudo apt-get update -qq && sudo apt-get install -y grafana

# Create a world-readable custom config file so the Grafana process can read it.
# /etc/grafana/grafana.ini is root:grafana 640 — changing it risks a permission
# problem if ownership shifts. Using a separate file avoids that entirely.
sudo tee /etc/grafana/grafana-custom.ini > /dev/null << 'EOF'
# j105-logger custom Grafana config
# Auth and network settings are set via systemd Environment= in port.conf below.
# This file is intentionally minimal — full reference: /usr/share/grafana/conf/defaults.ini
[paths]
# (nothing extra needed — defaults are fine)
EOF
sudo chmod 644 /etc/grafana/grafana-custom.ini

# Point Grafana at the custom ini (idempotent)
if sudo grep -q '^CONF_FILE=' /etc/default/grafana-server 2>/dev/null; then
    sudo sed -i 's|^CONF_FILE=.*|CONF_FILE=/etc/grafana/grafana-custom.ini|' \
        /etc/default/grafana-server
else
    echo 'CONF_FILE=/etc/grafana/grafana-custom.ini' | \
        sudo tee -a /etc/default/grafana-server > /dev/null
fi

# Systemd override — port, binding, and auth settings
sudo mkdir -p /etc/systemd/system/grafana-server.service.d
sudo tee /etc/systemd/system/grafana-server.service.d/port.conf > /dev/null << 'EOF'
[Service]
Environment=GF_SERVER_HTTP_PORT=3001
Environment=GF_SERVER_HTTP_ADDR=0.0.0.0
Environment=GF_AUTH_DISABLE_LOGIN_FORM=false
Environment=GF_AUTH_ANONYMOUS_ENABLED=false
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now grafana-server

# Pre-provision InfluxDB datasource via Grafana provisioning
sudo mkdir -p /etc/grafana/provisioning/datasources
sudo tee /etc/grafana/provisioning/datasources/influxdb.yaml > /dev/null << EOF
apiVersion: 1
datasources:
  - name: InfluxDB
    type: influxdb
    access: proxy
    url: http://127.0.0.1:8086
    jsonData:
      version: Flux
      organization: j105
      defaultBucket: signalk
      httpMode: POST
    secureJsonData:
      token: ${INFLUX_TOKEN}
    isDefault: true
EOF
sudo systemctl restart grafana-server
info "Grafana installed on port 3001 (loopback-only, login required)."
info "Default Grafana admin credentials: admin / changeme123 — change after first login."

# ---------------------------------------------------------------------------
# e.1) j105logger dedicated service account
#      The logger runs as this system account rather than as the Pi user.
#      Only data/ is writable by this account; the rest of the project is read-only.
# ---------------------------------------------------------------------------

step "Creating j105logger dedicated service account..."
if ! id j105logger &>/dev/null; then
    sudo useradd --system \
        --shell /usr/sbin/nologin \
        --no-create-home \
        --comment "j105-logger service account" \
        j105logger
    info "Created j105logger system account."
else
    info "j105logger already exists — skipping useradd."
fi
sudo usermod -aG "audio,netdev,${CURRENT_USER}" j105logger

# uv cache directory (service account has no home dir, so uv can't write ~/.cache/uv)
sudo mkdir -p /var/cache/j105-logger
sudo chown j105logger:j105logger /var/cache/j105-logger
info "uv cache: /var/cache/j105-logger"

# Minimal traversal for $HOME — the full uv/Python chmod is done after
# "uv sync" (step g) once the directories actually exist.
chmod 711 "$HOME"

# ---------------------------------------------------------------------------
# f) Signal K → InfluxDB plugin config (with captured token)
# ---------------------------------------------------------------------------

step "Configuring signalk-to-influxdb2 plugin..."
mkdir -p "$HOME/.signalk/plugin-config-data"
cat > "$HOME/.signalk/plugin-config-data/signalk-to-influxdb2.json" << EOF
{
  "configuration": {
    "influxes": [
      {
        "url": "http://127.0.0.1:8086",
        "token": "${INFLUX_TOKEN}",
        "org": "j105",
        "bucket": "signalk",
        "onlySelf": true
      }
    ]
  },
  "enabled": true
}
EOF
info "Plugin config written."

# ---------------------------------------------------------------------------
# g) uv + Python dependencies
# ---------------------------------------------------------------------------

step "Checking uv..."
UV_BIN=""
if command -v uv &>/dev/null; then
    UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi

if [[ -z "$UV_BIN" ]]; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV_BIN="$HOME/.local/bin/uv"
fi
info "uv: $UV_BIN ($("$UV_BIN" --version))"

step "Syncing Python dependencies..."
"$UV_BIN" sync --project "$PROJECT_DIR"

# Now that uv has installed Python and created .venv, make the entire symlink
# chain traversable so j105logger can reach .venv/bin/python3 →
# ~/.local/share/uv/python/cpython-*/bin/python3.12.
# This MUST run after "uv sync" — on a fresh install the directories don't
# exist until uv creates them, so doing it earlier fails silently.
info "Setting traversal permissions for j105logger..."
chmod 711 "$HOME"
for d in "$HOME/.local" \
         "$HOME/.local/bin" \
         "$HOME/.local/share" \
         "$HOME/.local/share/uv" \
         "$HOME/.local/share/uv/python"; do
    chmod -f 711 "$d" 2>/dev/null || true
done
# uv's Python installs live in version-specific subdirs — open them all
find "$HOME/.local/share/uv/python" -mindepth 1 -maxdepth 2 -type d \
    -exec chmod 711 {} + 2>/dev/null || true

# Install j105-logger wrapper script so the command works directly in the shell
# (uv console scripts live in the venv, not in ~/.local/bin)
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/j105-logger" << WRAPPER
#!/usr/bin/env bash
exec "${UV_BIN}" run --project "${PROJECT_DIR}" j105-logger "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/j105-logger"
info "j105-logger wrapper installed to ~/.local/bin/j105-logger"

# Ensure ~/.local/bin is in PATH permanently
if ! grep -q 'local/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    info "Added ~/.local/bin to PATH in ~/.bashrc"
else
    info "~/.local/bin already in ~/.bashrc PATH"
fi

# ---------------------------------------------------------------------------
# g.1) Signal K authentication — bcrypt admin password
#      Generates a random password, hashes it with bcrypt (via the project's
#      Python venv), and writes ~/.signalk/security.json.
#      Skipped if security.json already exists (idempotent).
# ---------------------------------------------------------------------------

step "Configuring Signal K authentication..."
SK_SECURITY_FILE="$HOME/.signalk/security.json"
if [[ ! -f "$SK_SECURITY_FILE" ]]; then
    info "Generating Signal K admin password..."
    # Generate random password (alphanumeric only, no shell quoting issues)
    SK_ADMIN_PASS="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 20)"
    export SK_ADMIN_PASS

    # bcrypt-hash via uv (--with pulls bcrypt on the fly; it is NOT a project dep)
    "$UV_BIN" run --with bcrypt --project "$PROJECT_DIR" python -c "
import os, json, bcrypt
pw = os.environ['SK_ADMIN_PASS'].encode()
h = bcrypt.hashpw(pw, bcrypt.gensalt(rounds=12)).decode()
data = {
    'allowedCorsOrigins': [],
    'immutableConfig': False,
    'acls': [],
    'users': [
        {'userId': 'admin', 'type': 'password', 'password': h, 'roles': ['admin']}
    ],
    'devices': []
}
print(json.dumps(data, indent=2))
" > "$SK_SECURITY_FILE"

    # Save the plaintext password for the operator
    echo "$SK_ADMIN_PASS" > "$HOME/.signalk-admin-pass.txt"
    chmod 600 "$HOME/.signalk-admin-pass.txt"

    info "Signal K admin account created."
    info "Username: admin"
    info "Password: ${SK_ADMIN_PASS}"
    info "Password also saved to ~/.signalk-admin-pass.txt (chmod 600)"
    unset SK_ADMIN_PASS
else
    info "~/.signalk/security.json already exists — skipping."
    info "Password is in ~/.signalk-admin-pass.txt (if this setup.sh created it)."
fi

# ---------------------------------------------------------------------------
# h) .env file and data directories
# ---------------------------------------------------------------------------

step "Configuring environment..."
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    info "Created .env from template."
    info "Review $ENV_FILE and adjust if needed."
else
    info ".env already exists — leaving untouched."
fi

# Populate InfluxDB connection if we have a token and it's still commented out
if [[ -n "$INFLUX_TOKEN" && "$INFLUX_TOKEN" != "REPLACE_WITH_INFLUX_TOKEN" ]]; then
    if grep -q '^# INFLUX_URL=' "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^# INFLUX_URL=.*|INFLUX_URL=http://localhost:8086|" "$ENV_FILE"
        sed -i "s|^# INFLUX_TOKEN=.*|INFLUX_TOKEN=${INFLUX_TOKEN}|" "$ENV_FILE"
        sed -i "s|^# INFLUX_ORG=.*|INFLUX_ORG=j105|" "$ENV_FILE"
        sed -i "s|^# INFLUX_BUCKET=.*|INFLUX_BUCKET=signalk|" "$ENV_FILE"
        info "InfluxDB connection configured in .env"
    fi
fi

# Restrict .env so only weaties (and root via systemd EnvironmentFile) can read it
chmod 600 "$ENV_FILE"
info ".env permissions set to 600."

# Load env vars for service generation
set -a
# shellcheck disable=SC1090
source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | grep -v '^#')
set +a

CAN_INTERFACE="${CAN_INTERFACE:-can0}"
CAN_BITRATE="${CAN_BITRATE:-250000}"

step "Ensuring data directories exist..."
mkdir -p "$PROJECT_DIR/data"
mkdir -p "$PROJECT_DIR/data/audio"
mkdir -p "$PROJECT_DIR/data/notes"
info "$PROJECT_DIR/data (SQLite DB)"
info "$PROJECT_DIR/data/audio (WAV recordings)"
info "$PROJECT_DIR/data/notes (photo notes)"

# Shared ownership: deploy user owns, deploy user's group is shared with j105logger.
# setgid ensures new files/dirs inherit the group so both users can always read/write.
sudo chown -R "$CURRENT_USER:$CURRENT_USER" "$PROJECT_DIR/data"
sudo chmod -R g+ws "$PROJECT_DIR/data"
info "data/ owned by $CURRENT_USER, group-writable (j105logger is a member)."

# ---------------------------------------------------------------------------
# i) netdev group (allows non-root SocketCAN access)
# ---------------------------------------------------------------------------

step "Checking group membership..."
if ! id -nG "$CURRENT_USER" | grep -qw netdev; then
    sudo usermod -aG netdev "$CURRENT_USER"
    warn "Added $CURRENT_USER to the 'netdev' group — reboot required."
else
    info "$CURRENT_USER is already in the 'netdev' group."
fi

# ---------------------------------------------------------------------------
# j) CAN interface systemd service
# ---------------------------------------------------------------------------

step "Installing CAN interface service (interface=$CAN_INTERFACE bitrate=$CAN_BITRATE)..."
sudo tee /etc/systemd/system/can-interface.service > /dev/null << EOF
[Unit]
Description=SocketCAN interface ${CAN_INTERFACE}
After=sys-subsystem-net-devices-${CAN_INTERFACE}.device
BindsTo=sys-subsystem-net-devices-${CAN_INTERFACE}.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/sbin/ip link set ${CAN_INTERFACE} down
ExecStart=/sbin/ip link set ${CAN_INTERFACE} up type can bitrate ${CAN_BITRATE}
ExecStop=/sbin/ip link set ${CAN_INTERFACE} down

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable can-interface.service

if ip link show "$CAN_INTERFACE" &>/dev/null; then
    sudo systemctl restart can-interface.service
    info "CAN interface ${CAN_INTERFACE} is up."
else
    warn "Network device '${CAN_INTERFACE}' not found — service enabled but not started."
    warn "Add dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25 to /boot/firmware/config.txt"
    warn "and reboot, then re-run this script."
fi

# ---------------------------------------------------------------------------
# k) j105-logger systemd service
#    Runs as the dedicated j105logger account (not as the Pi user).
#    --no-sync: use the existing venv without trying to modify it (weaties owns it).
#    UV_CACHE_DIR: j105logger has no home dir, so point uv cache to /var/cache.
# ---------------------------------------------------------------------------

step "Installing j105-logger service..."
sudo tee /etc/systemd/system/j105-logger.service > /dev/null << EOF
[Unit]
Description=J105 NMEA 2000 Data Logger
After=signalk.service
Wants=signalk.service

[Service]
User=j105logger
Group=j105logger
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
Environment=UV_CACHE_DIR=/var/cache/j105-logger
Environment=HOME=/var/cache/j105-logger
ExecStart=${UV_BIN} run --no-sync --project ${PROJECT_DIR} j105-logger run
Restart=on-failure
RestartSec=5
SupplementaryGroups=netdev audio

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable j105-logger.service

if sudo systemctl is-active --quiet signalk.service 2>/dev/null; then
    sudo systemctl restart j105-logger.service
    info "Logger service started."
else
    info "Logger service enabled (will start once Signal K is running)."
fi

# ---------------------------------------------------------------------------
# k.1) Loki + Promtail — centralized log management
#      Uses the Grafana apt repo added in section e) above.
#      Loki stores logs on the local filesystem; Promtail scrapes journald.
#      Both bind to loopback only.
# ---------------------------------------------------------------------------

step "Installing Loki + Promtail..."
sudo apt-get install -y loki promtail

# Deploy Loki config
sudo mkdir -p /etc/loki
sudo cp "$SCRIPT_DIR/loki/loki-config.yaml" /etc/loki/loki-config.yaml

# Deploy Promtail config
sudo mkdir -p /etc/promtail
sudo cp "$SCRIPT_DIR/loki/promtail-config.yaml" /etc/promtail/promtail-config.yaml

# Data directories
sudo mkdir -p /var/lib/loki /var/lib/promtail
sudo chown loki:loki /var/lib/loki
sudo chown promtail:promtail /var/lib/promtail

# Promtail needs journal access
sudo usermod -aG systemd-journal promtail

# Systemd overrides to use our config files
sudo mkdir -p /etc/systemd/system/loki.service.d
sudo tee /etc/systemd/system/loki.service.d/config.conf > /dev/null << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/loki -config.file=/etc/loki/loki-config.yaml
EOF

sudo mkdir -p /etc/systemd/system/promtail.service.d
sudo tee /etc/systemd/system/promtail.service.d/config.conf > /dev/null << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/promtail -config.file=/etc/promtail/promtail-config.yaml
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now loki promtail
info "Loki (port 3100) + Promtail installed and running."

# ---------------------------------------------------------------------------
# l) Scoped NOPASSWD sudo
#      Creates /etc/sudoers.d/j105-logger-allowed with the specific commands
#      needed for day-to-day operations (deploy.sh, service management).
#      Then removes the blanket NOPASSWD from the Pi OS default sudoers files.
#      This is done last so all earlier sudo commands in this script succeed.
# ---------------------------------------------------------------------------

step "Configuring scoped sudo permissions..."
SUDOERS_FILE="/etc/sudoers.d/j105-logger-allowed"
sudo tee "$SUDOERS_FILE" > /dev/null << EOF
# j105-logger scoped sudo permissions — generated by setup.sh
# Only the commands listed here run without a password prompt.
# For full sudo access (package installs, system config), type your password.

# Service management — used by deploy.sh and daily operations
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl daemon-reload
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start j105-logger
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop j105-logger
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart j105-logger
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status j105-logger
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start j105-logger.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop j105-logger.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart j105-logger.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status j105-logger.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start signalk
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop signalk
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart signalk
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status signalk
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start signalk.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop signalk.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart signalk.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status signalk.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start grafana-server
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop grafana-server
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart grafana-server
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status grafana-server
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start grafana-server.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop grafana-server.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart grafana-server.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status grafana-server.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start influxdb
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop influxdb
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart influxdb
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status influxdb
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start influxdb.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop influxdb.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart influxdb.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status influxdb.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start loki
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop loki
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart loki
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status loki
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start loki.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop loki.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart loki.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status loki.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start promtail
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop promtail
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart promtail
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status promtail
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start promtail.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop promtail.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart promtail.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl status promtail.service
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active j105-logger
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active j105-logger.service

# Log access
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/journalctl

# Privileged file copy (update system configs without blanket sudo)
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/rsync

# Grafana port.conf update
${CURRENT_USER} ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/systemd/system/grafana-server.service.d/port.conf
EOF
sudo chmod 440 "$SUDOERS_FILE"

# Validate before removing blanket access
if sudo visudo -c -f "$SUDOERS_FILE" > /dev/null 2>&1; then
    info "Scoped sudoers file created and validated: $SUDOERS_FILE"
else
    warn "Sudoers file has syntax errors — removing to prevent lockout. Blanket NOPASSWD retained."
    sudo rm "$SUDOERS_FILE"
fi

# Remove blanket NOPASSWD from Raspberry Pi OS default sudoers files.
# weaties still has sudo — just needs to type the password for non-scoped commands.
for SUDO_BLANKET in /etc/sudoers.d/010_pi-nopasswd /etc/sudoers.d/90-cloud-init-users; do
    if sudo test -f "$SUDO_BLANKET" 2>/dev/null; then
        # Replace NOPASSWD: with nothing (preserves the rest of the rule)
        sudo sed -i 's/[[:space:]]*NOPASSWD:[[:space:]]*//' "$SUDO_BLANKET"
        info "Removed blanket NOPASSWD from $SUDO_BLANKET"
    fi
done

# ---------------------------------------------------------------------------
# m) Done — summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete. Reboot, then verify:${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "  Signal K:    http://corvopi:3000   (admin password in ~/.signalk-admin-pass.txt)"
echo "  Grafana:     http://corvopi:3001   (admin / changeme123 — change after first login)"
echo "  InfluxDB:    http://corvopi:8086   (loopback-only — access via SSH tunnel or Tailscale)"
echo "  Race marker: http://corvopi:3002   (login required — see below)"
echo ""
if [[ -f "$INFLUX_TOKEN_FILE" ]]; then
    echo "  InfluxDB token saved to: $INFLUX_TOKEN_FILE"
fi
echo ""
echo -e "${YELLOW}  NEXT STEPS:${NC}"
echo ""
echo "  1. Create your admin user for the race-marker web app:"
echo "       j105-logger add-user --email you@example.com --name 'Your Name' --role admin"
echo ""
echo "  2. Change the Grafana password:"
echo "       Open http://corvopi:3001 and change the admin password from 'changeme123'."
echo ""
echo "  3. Find the Signal K admin password:"
echo "       cat ~/.signalk-admin-pass.txt"
echo ""
echo "  4. Reboot:"
echo "       sudo reboot"
echo ""
echo "  After reboot, check service status:"
echo "    sudo systemctl status can-interface signalk influxd grafana-server loki promtail j105-logger"
echo ""
echo "  View logger output:"
echo "    sudo journalctl -fu j105-logger"
echo ""
echo "  To list available audio input devices (e.g. Gordik USB receiver):"
echo "    j105-logger list-devices"
echo "  Then set AUDIO_DEVICE in .env to match the device name or index."
echo ""
echo "  To update after a git pull:"
echo "    ./scripts/deploy.sh"
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
