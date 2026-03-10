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
# All sudo commands used here are in /etc/sudoers.d/helmlog-allowed
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
# Ensure .git/ is owned by the current user (fixes ownership conflicts when
# the helmlog service account has run git commands in this directory)
# ---------------------------------------------------------------------------
REPO_OWNER="$(stat -c '%U' "$PROJECT_DIR")"
if [[ "$(whoami)" != "$REPO_OWNER" ]]; then
    echo "ERROR: deploy.sh must run as '$REPO_OWNER' (the repo owner), not '$(whoami)'." >&2
    exit 1
fi

# Fix any mis-owned .git/ files (e.g. from a previous service-account git op)
if find .git -not -user "$(whoami)" -print -quit 2>/dev/null | grep -q .; then
    echo "==> Fixing .git/ ownership (some files owned by wrong user)..."
    sudo chown -R "$(whoami):$(whoami)" .git/
fi

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

# Ensure helmlog can traverse the uv Python symlink chain (.venv/bin/python →
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

echo "==> Updating nginx config..."
if [[ -f /etc/nginx/sites-available/helmlog ]]; then
    if ! diff -q "$SCRIPT_DIR/nginx/helmlog.conf" /etc/nginx/sites-available/helmlog &>/dev/null; then
        sudo cp "$SCRIPT_DIR/nginx/helmlog.conf" /etc/nginx/sites-available/helmlog
        if sudo nginx -t 2>&1; then
            sudo systemctl reload nginx
            echo "    nginx config updated and reloaded."
        else
            echo "    WARNING: nginx config test failed — not reloaded."
        fi
    else
        echo "    nginx config unchanged."
    fi
else
    echo "    nginx not configured — run setup.sh to install."
fi

echo "==> Fixing data directory permissions..."
chmod -R g+w "$PROJECT_DIR/data" 2>/dev/null || true

echo "==> Restarting helmlog service..."
sudo systemctl restart helmlog

# ---------------------------------------------------------------------------
# Bootstrap admin user on first deploy
# If no admin user exists yet, create one so the operator can log in immediately.
# ---------------------------------------------------------------------------

# Source .env for DB_PATH, ADMIN_EMAIL, WEB_PORT
ENV_FILE="$PROJECT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | grep -v '^#')
    set +a
fi

DB_FILE="$PROJECT_DIR/${DB_PATH:-data/logger.db}"

# Wait for helmlog to start and create/migrate the DB
echo "==> Checking for admin user..."
for _i in {1..10}; do
    [[ -f "$DB_FILE" ]] && break
    sleep 1
done

ADMIN_COUNT="0"
if [[ -f "$DB_FILE" ]]; then
    ADMIN_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM users WHERE role='admin';" 2>/dev/null || echo "0")
fi

if [[ "$ADMIN_COUNT" -eq 0 ]]; then
    echo ""
    echo "    No admin user found — creating one for first-time login."

    ADMIN_EMAIL="${ADMIN_EMAIL:-}"
    if [[ -z "$ADMIN_EMAIL" ]]; then
        read -rp "    Admin email address: " ADMIN_EMAIL
    else
        echo "    Using ADMIN_EMAIL from .env: ${ADMIN_EMAIL}"
    fi

    if [[ -n "$ADMIN_EMAIL" ]]; then
        echo ""
        ADD_USER_OUTPUT=$("$UV_BIN" run --project "$PROJECT_DIR" helmlog add-user \
            --email "$ADMIN_EMAIL" --role admin 2>&1) || true
        LOGIN_URL=$(echo "$ADD_USER_OUTPUT" | grep -oE 'http[s]?://[^ ]+/login\?token=[^ ]+' | head -1)

        echo "╔══════════════════════════════════════════════════╗"
        echo "║  Admin user created!                             ║"
        echo "╠══════════════════════════════════════════════════╣"
        echo "║                                                  ║"
        echo "  Email: ${ADMIN_EMAIL}"
        echo "║                                                  ║"
        if [[ -n "$LOGIN_URL" ]]; then
            echo "  Login URL (paste into browser):"
            echo "  ${LOGIN_URL}"
        else
            echo "  (Could not extract login URL — check output below)"
            echo "$ADD_USER_OUTPUT"
        fi
        echo "║                                                  ║"
        echo "╚══════════════════════════════════════════════════╝"
        echo ""
    else
        echo "    Skipped — no email provided."
        echo "    Create one later: helmlog add-user --email you@example.com --role admin"
    fi
else
    echo "    Admin user exists — skipping bootstrap."
fi

echo ""
echo "==> Deploy complete."
systemctl is-active helmlog && echo "    helmlog is running." || echo "    WARNING: helmlog is NOT running."
