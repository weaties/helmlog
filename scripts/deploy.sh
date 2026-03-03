#!/usr/bin/env bash
# deploy.sh — Deploy code to the Raspberry Pi and restart services.
#
# Usage:
#   ./scripts/deploy.sh              # on main: pull & deploy latest main
#                                    # on a PR branch: prompt to switch PR or revert
#   ./scripts/deploy.sh --pr 126     # deploy PR #126's branch
#
# provision-grafana.sh is called every time and is fully idempotent.
# Tailscale Funnel routes are re-applied on every deploy (idempotent).
#
# All sudo commands used here are in /etc/sudoers.d/j105-logger-allowed
# so they run without a password prompt (set up by setup.sh).
#
# If systemd service files or apt packages changed, also run:
#   ./scripts/setup.sh && sudo systemctl daemon-reload

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
PR_NUMBER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pr)
            PR_NUMBER="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: deploy.sh [--pr NUMBER]"
            echo ""
            echo "  --pr NUMBER   Deploy the branch for GitHub PR #NUMBER"
            echo ""
            echo "On main: pulls latest and deploys."
            echo "On a PR branch: prompts to switch PRs or revert to main."
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Run deploy.sh --help for usage." >&2
            exit 1
            ;;
    esac
done

cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# Resolve uv — not on PATH in non-interactive SSH sessions
# ---------------------------------------------------------------------------
if command -v uv &>/dev/null; then
    UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
else
    echo "ERROR: uv not found. Run setup.sh first." >&2
    exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# ---------------------------------------------------------------------------
# Resolve which ref to deploy
# ---------------------------------------------------------------------------
deploy_main() {
    echo "==> Deploying main..."
    git fetch origin main
    git checkout main
    git pull origin main
}

deploy_pr() {
    local pr="$1"
    echo "==> Deploying PR #${pr}..."
    if ! command -v gh &>/dev/null; then
        echo "ERROR: gh CLI is required for --pr deploys. Install: https://cli.github.com" >&2
        exit 1
    fi
    PR_BRANCH="$(gh pr view "$pr" --json headRefName -q .headRefName)"
    if [[ -z "$PR_BRANCH" ]]; then
        echo "ERROR: Could not resolve branch for PR #${pr}" >&2
        exit 1
    fi
    echo "    Branch: ${PR_BRANCH}"
    git fetch origin "$PR_BRANCH"
    git checkout "$PR_BRANCH"
    git pull origin "$PR_BRANCH"
}

if [[ -n "$PR_NUMBER" ]]; then
    # Explicit --pr flag always wins
    deploy_pr "$PR_NUMBER"
elif [[ "$CURRENT_BRANCH" == "main" ]]; then
    # On main with no --pr flag: pull latest main
    deploy_main
else
    # On a non-main branch (i.e. a PR deployment) with no --pr flag: ask
    echo ""
    echo "Currently deployed: branch '${CURRENT_BRANCH}' (not main)"
    echo ""
    echo "  1) Revert to main"
    echo "  2) Deploy a different PR"
    echo "  3) Re-deploy current branch (pull latest)"
    echo ""
    read -rp "Choice [1/2/3]: " choice
    case "$choice" in
        1)
            deploy_main
            ;;
        2)
            read -rp "PR number: " pr_num
            deploy_pr "$pr_num"
            ;;
        3)
            echo "==> Re-deploying ${CURRENT_BRANCH}..."
            git pull origin "$CURRENT_BRANCH"
            ;;
        *)
            echo "Invalid choice. Aborting." >&2
            exit 1
            ;;
    esac
fi

echo "==> Syncing Python dependencies..."
"$UV_BIN" sync

echo "==> Provisioning Grafana (dashboard, datasources, plugins)..."
"$SCRIPT_DIR/provision-grafana.sh"

# ---------------------------------------------------------------------------
# Tailscale Funnel routes — re-applied on every deploy (idempotent, fast)
# Also updates PUBLIC_URL in .env and Grafana ROOT_URL so deep-links stay current.
# ---------------------------------------------------------------------------
echo "==> Configuring Tailscale Funnel routes..."
if command -v tailscale &>/dev/null; then
    TS_HOSTNAME="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\\.$//' || echo '')"
    if [[ -n "$TS_HOSTNAME" ]]; then
        tailscale funnel --bg 3002
        tailscale funnel --bg --set-path /grafana/ 3001
        tailscale funnel --bg --set-path /signalk/ 3000
        echo "    Routes verified for https://${TS_HOSTNAME}"
        # Update Grafana ROOT_URL with the actual public hostname
        TMPCONF="$(mktemp)"
        cat > "$TMPCONF" << EOF
[Service]
Environment=GF_SERVER_HTTP_PORT=3001
Environment=GF_SERVER_ROOT_URL=https://${TS_HOSTNAME}/grafana/
Environment=GF_SERVER_HTTP_ADDR=127.0.0.1
Environment=GF_AUTH_DISABLE_LOGIN_FORM=false
Environment=GF_AUTH_ANONYMOUS_ENABLED=false
EOF
        sudo rsync "$TMPCONF" /etc/systemd/system/grafana-server.service.d/port.conf
        rm -f "$TMPCONF"
        sudo systemctl daemon-reload
        sudo systemctl restart grafana-server
        # Keep PUBLIC_URL in .env current so the webapp generates correct links
        PUBLIC_URL_VALUE="https://${TS_HOSTNAME}"
        if [[ -f "$ENV_FILE" ]]; then
            if grep -q '^PUBLIC_URL=' "$ENV_FILE" 2>/dev/null; then
                sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=${PUBLIC_URL_VALUE}|" "$ENV_FILE"
            else
                printf '\nPUBLIC_URL=%s\n' "${PUBLIC_URL_VALUE}" >> "$ENV_FILE"
            fi
        fi
    else
        echo "    Tailscale not connected — skipping (run 'tailscale up' then re-deploy)."
    fi
else
    echo "    tailscale CLI not found — skipping."
fi

# ---------------------------------------------------------------------------
# Cloudflare Tunnel — update ingress to route /grafana/ and /signalk/ via nginx
# ---------------------------------------------------------------------------
# The systemd-managed cloudflared reads from /etc/cloudflared/config.yml;
# the user-level copy at ~/.cloudflared/config.yml is only used for manual runs.
CF_SYSTEM_CONFIG="/etc/cloudflared/config.yml"
CF_USER_CONFIG="$HOME/.cloudflared/config.yml"
if [[ -f "$CF_SYSTEM_CONFIG" ]] || [[ -f "$CF_USER_CONFIG" ]]; then
    echo "==> Configuring Cloudflare Tunnel ingress..."
    # Read tunnel metadata from whichever config exists
    CF_SRC="${CF_SYSTEM_CONFIG}"
    [[ -f "$CF_SRC" ]] || CF_SRC="$CF_USER_CONFIG"
    TUNNEL_ID="$(grep '^tunnel:' "$CF_SRC" | awk '{print $2}')"
    CRED_FILE="$(grep '^credentials-file:' "$CF_SRC" | awk '{print $2}')"
    if [[ -n "$TUNNEL_ID" && -n "$CRED_FILE" ]]; then
        TMPCF="$(mktemp)"
        cat > "$TMPCF" << EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${CRED_FILE}
protocol: http2

ingress:
  - hostname: corvo.saillog.io
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF
        # Update both the system and user configs
        [[ -f "$CF_SYSTEM_CONFIG" ]] && sudo rsync "$TMPCF" "$CF_SYSTEM_CONFIG"
        cp "$TMPCF" "$CF_USER_CONFIG"
        rm -f "$TMPCF"
        echo "    cloudflared config updated (routing via nginx on :8080)."
    else
        echo "    WARNING: could not parse tunnel/credentials from $CF_SRC — skipping."
    fi
else
    echo "    No cloudflared config found — skipping."
fi

# ---------------------------------------------------------------------------
# nginx — reverse proxy for Cloudflare Tunnel (path-based routing) and hotspot
# ---------------------------------------------------------------------------
echo "==> Configuring nginx reverse proxy..."
NGINX_CONF="/etc/nginx/conf.d/cloudflare-tunnel.conf"
TMPNGINX="$(mktemp)"
cat > "$TMPNGINX" << 'EOF'
# Reverse proxy for Cloudflare Tunnel — routes sub-paths to backend services.
# cloudflared sends all corvo.saillog.io traffic here on 127.0.0.1:8080.
server {
    listen 127.0.0.1:8080;

    location /grafana/ {
        proxy_pass http://127.0.0.1:3001/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /signalk/ {
        proxy_pass http://127.0.0.1:3000/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:3002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo rsync "$TMPNGINX" "$NGINX_CONF"
rm -f "$TMPNGINX"
echo "    nginx cloudflare-tunnel.conf written."

# Also update the hotspot site config with Grafana/Signal K routes
NGINX_SITE="/etc/nginx/sites-available/corvo"
if [[ -f "$NGINX_SITE" ]]; then
    if ! grep -q '/grafana/' "$NGINX_SITE" 2>/dev/null; then
        # Insert Grafana and Signal K location blocks before the catch-all location /
        sudo sed -i '/location \/ {/i \
    location /grafana/ {\
        proxy_pass http://127.0.0.1:3001/;\
        proxy_http_version 1.1;\
        proxy_set_header Host $host;\
        proxy_set_header X-Real-IP $remote_addr;\
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\
        proxy_set_header X-Forwarded-Proto $scheme;\
    }\
\
    location /signalk/ {\
        proxy_pass http://127.0.0.1:3000/;\
        proxy_http_version 1.1;\
        proxy_set_header Upgrade $http_upgrade;\
        proxy_set_header Connection "upgrade";\
        proxy_set_header Host $host;\
        proxy_set_header X-Real-IP $remote_addr;\
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\
        proxy_set_header X-Forwarded-Proto $scheme;\
    }\
' "$NGINX_SITE"
        echo "    nginx hotspot site updated with /grafana/ and /signalk/ routes."
    fi
fi

sudo nginx -t && sudo systemctl reload nginx
echo "    nginx reloaded."

if systemctl is-active cloudflared &>/dev/null; then
    sudo systemctl restart cloudflared
    echo "    cloudflared restarted."
fi

echo "==> Restarting j105-logger service..."
sudo systemctl restart j105-logger

echo ""
echo "==> Deploy complete."
systemctl is-active j105-logger && echo "    j105-logger is running." || echo "    WARNING: j105-logger is NOT running."
systemctl is-active cloudflared 2>/dev/null && echo "    cloudflared tunnel is running." || echo "    WARNING: cloudflared tunnel is NOT running."
systemctl is-active nginx 2>/dev/null && echo "    nginx is running." || echo "    WARNING: nginx is NOT running."
