# Claude Code dev container — autonomous issue→PR workflow.
#
# Runs Claude Code in --dangerously-skip-permissions mode inside a
# disposable container. All credentials are passed at runtime via env vars.
#
# Build:
#   docker build -t helmlog-claude-dev docker/claude-dev/
#
# Run:
#   ./docker/claude-dev/run-issue.sh 42

FROM python:3.12-slim AS base

ARG DEBIAN_FRONTEND=noninteractive

# ── System deps ──────────────────────────────────────────────────────────────

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates openssh-client \
        # helmlog native deps
        portaudio19-dev libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*

# ── GitHub CLI ───────────────────────────────────────────────────────────────

RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# ── uv ───────────────────────────────────────────────────────────────────────

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ── Claude Code CLI ──────────────────────────────────────────────────────────

RUN npm install -g @anthropic-ai/claude-code || \
    (curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
     && apt-get install -y nodejs \
     && npm install -g @anthropic-ai/claude-code)

# ── Non-root user ────────────────────────────────────────────────────────────
# Claude Code refuses --dangerously-skip-permissions as root.

RUN useradd -m -s /bin/bash claude && mkdir -p /workspace && chown claude:claude /workspace

COPY --chmod=755 entrypoint.sh /entrypoint.sh

# ── Workspace ────────────────────────────────────────────────────────────────

WORKDIR /workspace
USER claude

ENTRYPOINT ["/entrypoint.sh"]
