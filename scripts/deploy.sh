#!/usr/bin/env bash
# deploy.sh — Deploy code to the Raspberry Pi and restart services.
#
# Usage:
#   ./scripts/deploy.sh              # on main: pull & deploy latest main
#                                    # on a PR branch: prompt to switch PR or revert
#   ./scripts/deploy.sh --pr 126     # deploy PR #126's branch
#
# provision-grafana.sh is called every time and is fully idempotent.
#
# All sudo commands used here are in /etc/sudoers.d/j105-logger-allowed
# so they run without a password prompt (set up by setup.sh).
#
# If systemd service files or apt packages changed, also run:
#   ./scripts/setup.sh && sudo systemctl daemon-reload

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

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

# Ensure j105logger can traverse the uv Python symlink chain (.venv/bin/python →
# ~/.local/share/uv/python/cpython-*/bin/python3.12).  A Python version upgrade
# creates a new cpython-* dir that would otherwise be 700.
for d in "$HOME/.local" "$HOME/.local/share" "$HOME/.local/share/uv" \
         "$HOME/.local/share/uv/python"; do
    chmod -f 711 "$d" 2>/dev/null || true
done
find "$HOME/.local/share/uv/python" -mindepth 1 -maxdepth 2 -type d \
    -exec chmod 711 {} + 2>/dev/null || true

echo "==> Provisioning Grafana (dashboard, datasources, plugins)..."
"$SCRIPT_DIR/provision-grafana.sh"

echo "==> Updating Loki + Promtail configs..."
LOKI_CHANGED=false
PROMTAIL_CHANGED=false
if ! diff -q "$SCRIPT_DIR/loki/loki-config.yaml" /etc/loki/loki-config.yaml &>/dev/null; then
    sudo cp "$SCRIPT_DIR/loki/loki-config.yaml" /etc/loki/loki-config.yaml
    LOKI_CHANGED=true
fi
if ! diff -q "$SCRIPT_DIR/loki/promtail-config.yaml" /etc/promtail/promtail-config.yaml &>/dev/null; then
    sudo cp "$SCRIPT_DIR/loki/promtail-config.yaml" /etc/promtail/promtail-config.yaml
    PROMTAIL_CHANGED=true
fi
if $LOKI_CHANGED; then
    sudo systemctl restart loki
    echo "    Loki config updated and restarted."
fi
if $PROMTAIL_CHANGED; then
    sudo systemctl restart promtail
    echo "    Promtail config updated and restarted."
fi
$LOKI_CHANGED || $PROMTAIL_CHANGED || echo "    Loki + Promtail configs unchanged."

echo "==> Fixing data directory permissions..."
chmod -R g+w "$PROJECT_DIR/data" 2>/dev/null || true

echo "==> Restarting j105-logger service..."
sudo systemctl restart j105-logger

echo ""
echo "==> Deploy complete."
systemctl is-active j105-logger && echo "    j105-logger is running." || echo "    WARNING: j105-logger is NOT running."
