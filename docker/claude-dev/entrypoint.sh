#!/usr/bin/env bash
set -euo pipefail

# ── Git identity ─────────────────────────────────────────────────────────────

git config --global user.name "${GIT_AUTHOR_NAME:-Claude Code Bot}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-noreply@anthropic.com}"

# ── Clone repo ───────────────────────────────────────────────────────────────

REPO="${GITHUB_REPO:-weaties/helmlog}"

if [ ! -d "/workspace/helmlog/.git" ]; then
    echo "▸ Cloning ${REPO}..."
    gh repo clone "$REPO" /workspace/helmlog
fi

cd /workspace/helmlog
git fetch origin
git checkout main && git pull origin main

# ── Install Python deps ─────────────────────────────────────────────────────

echo "▸ Installing dependencies..."
uv sync --quiet

# ── Run Claude Code ──────────────────────────────────────────────────────────

echo "▸ Starting Claude Code..."
exec claude --dangerously-skip-permissions -p "$@"
