#!/usr/bin/env bash
# Usage:
#   ./docker/claude-dev/run-issue.sh 42
#   ./docker/claude-dev/run-issue.sh 42 "Also update the migration version"
#   TIMEOUT=3600 ./docker/claude-dev/run-issue.sh 42
#
# Required env vars: ANTHROPIC_API_KEY, GH_TOKEN
# Optional: GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, TIMEOUT (seconds, default 1800)
set -euo pipefail

ISSUE_NUM="${1:?Usage: $0 <issue-number> [extra-instructions]}"
EXTRA="${2:-}"
TIMEOUT="${TIMEOUT:-1800}"

# Validate required env vars
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}"
: "${GH_TOKEN:?Set GH_TOKEN}"

PROMPT="You are working on the helmlog project (github.com/weaties/helmlog).

Your task: fix GitHub issue #${ISSUE_NUM}.

Workflow:
1. Read the issue with: gh issue view ${ISSUE_NUM}
2. Read CLAUDE.md for project conventions
3. Create a feature branch off main
4. Follow TDD — write a failing test first, then implement
5. Run the full check suite: uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/
6. If the change touches federation/co-op code, also run: uv run pytest tests/integration/ -v
7. Commit with a descriptive message
8. Push the branch and create a PR with gh pr create
9. Comment on the issue with the PR link

${EXTRA}"

# Use a unique container name so multiple can run in parallel
CONTAINER_NAME="claude-dev-issue-${ISSUE_NUM}-$$"

echo "▸ Starting container ${CONTAINER_NAME} (timeout: ${TIMEOUT}s)"
echo "▸ Working on issue #${ISSUE_NUM}"

docker run --rm \
    --name "$CONTAINER_NAME" \
    --stop-timeout 10 \
    -e ANTHROPIC_API_KEY \
    -e GH_TOKEN \
    -e GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-Claude Code Bot}" \
    -e GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-noreply@anthropic.com}" \
    --tmpfs /tmp:size=2G \
    helmlog-claude-dev \
    "$PROMPT" &

PID=$!

# Enforce timeout
(
    sleep "$TIMEOUT"
    echo "▸ Timeout (${TIMEOUT}s) reached — stopping container"
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
) &
TIMER_PID=$!

# Wait for container, then clean up timer
wait "$PID" 2>/dev/null
EXIT_CODE=$?
kill "$TIMER_PID" 2>/dev/null || true

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "▸ Done — check GitHub for the PR"
else
    echo "▸ Container exited with code ${EXIT_CODE}"
fi

exit "$EXIT_CODE"
