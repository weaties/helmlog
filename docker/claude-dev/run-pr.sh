#!/usr/bin/env bash
# Work on an existing pull request — address review feedback, fix CI, iterate.
#
# Usage:
#   ./docker/claude-dev/run-pr.sh 123
#   ./docker/claude-dev/run-pr.sh 123 "Focus on the mypy errors in storage.py"
#   TIMEOUT=3600 ./docker/claude-dev/run-pr.sh 123
#
# Required env vars: ANTHROPIC_API_KEY, GH_TOKEN
# Optional: GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, TIMEOUT (seconds, default 1800)
set -euo pipefail

PR_NUM="${1:?Usage: $0 <pr-number> [extra-instructions]}"
EXTRA="${2:-}"
TIMEOUT="${TIMEOUT:-1800}"

# Validate required env vars
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}"
: "${GH_TOKEN:?Set GH_TOKEN}"

PROMPT="You are working on the helmlog project (github.com/weaties/helmlog).

Your task: address feedback and fix issues on PR #${PR_NUM}.

Workflow:
1. Read CLAUDE.md for project conventions
2. Review the PR: gh pr view ${PR_NUM}
3. Read review comments: gh pr view ${PR_NUM} --comments
4. Check CI status: gh pr checks ${PR_NUM}
5. Check out the PR branch: gh pr checkout ${PR_NUM}
6. Read the diff to understand current state: gh pr diff ${PR_NUM}
7. Address all review comments and fix any CI failures
8. Follow TDD — if fixing a bug, write a failing test first
9. Run the full check suite: uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/
10. If the change touches federation/co-op code, also run: uv run pytest tests/integration/ -v
11. Commit with a descriptive message referencing the review feedback
12. Push to the existing PR branch (do NOT create a new PR)

${EXTRA}"

CONTAINER_NAME="claude-dev-pr-${PR_NUM}-$$"

echo "▸ Starting container ${CONTAINER_NAME} (timeout: ${TIMEOUT}s)"
echo "▸ Working on PR #${PR_NUM}"

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
    echo "▸ Done — check the PR for new commits"
else
    echo "▸ Container exited with code ${EXIT_CODE}"
fi

exit "$EXIT_CODE"
