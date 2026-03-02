#!/usr/bin/env bash
# deploy.sh — Deploy code to the Raspberry Pi and restart services.
#
# Usage:
#   ./scripts/deploy.sh              # deploy main (default)
#   ./scripts/deploy.sh --pr 126     # deploy PR #126's branch
#   ./scripts/deploy.sh --pr 126 --revert  # revert back to main
#
# When deploying a PR, the script fetches the PR branch from origin and
# checks it out. When reverting (or deploying without --pr), it checks
# out main and pulls latest.
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
REVERT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pr)
            PR_NUMBER="$2"
            shift 2
            ;;
        --revert)
            REVERT=true
            shift
            ;;
        -h|--help)
            echo "Usage: deploy.sh [--pr NUMBER] [--revert]"
            echo ""
            echo "  --pr NUMBER   Deploy the branch for GitHub PR #NUMBER"
            echo "  --revert      Revert to main (use after testing a PR)"
            echo ""
            echo "With no arguments, deploys latest main."
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

if [[ "$REVERT" == true ]] || [[ -z "$PR_NUMBER" ]]; then
    echo "==> Deploying main..."
    git fetch origin main
    git checkout main
    git pull origin main
else
    echo "==> Deploying PR #${PR_NUMBER}..."
    # Fetch the PR branch using the gh CLI to resolve the branch name
    if ! command -v gh &>/dev/null; then
        echo "ERROR: gh CLI is required for --pr deploys. Install: https://cli.github.com" >&2
        exit 1
    fi
    PR_BRANCH="$(gh pr view "$PR_NUMBER" --json headRefName -q .headRefName)"
    if [[ -z "$PR_BRANCH" ]]; then
        echo "ERROR: Could not resolve branch for PR #${PR_NUMBER}" >&2
        exit 1
    fi
    echo "    Branch: ${PR_BRANCH}"
    git fetch origin "$PR_BRANCH"
    git checkout "$PR_BRANCH"
    git pull origin "$PR_BRANCH"
    echo ""
    echo "    NOTE: You are now on a PR branch. Run 'deploy.sh --revert'"
    echo "    to go back to main when done testing."
fi

echo "==> Syncing Python dependencies..."
uv sync

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
        sudo tee /etc/systemd/system/grafana-server.service.d/port.conf > /dev/null << EOF
[Service]
Environment=GF_SERVER_HTTP_PORT=3001
Environment=GF_SERVER_ROOT_URL=https://${TS_HOSTNAME}/grafana/
Environment=GF_SERVER_HTTP_ADDR=127.0.0.1
Environment=GF_AUTH_DISABLE_LOGIN_FORM=false
Environment=GF_AUTH_ANONYMOUS_ENABLED=false
EOF
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

echo "==> Restarting j105-logger service..."
sudo systemctl restart j105-logger

echo ""
echo "==> Deploy complete."
sudo systemctl status j105-logger --no-pager -l
