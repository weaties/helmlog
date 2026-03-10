---
name: deploy-pi
description: Reference for deploying to the Raspberry Pi
disable-model-invocation: true
---

# Deploying to the Raspberry Pi

## Quick Deploy (after PR merges to main)

```bash
ssh <pi-user>@<pi-host>
cd ~/helmlog
./scripts/deploy.sh
```

The script: pulls `main`, syncs Python deps, re-applies Tailscale Funnel
routes, updates `PUBLIC_URL` in `.env`, restarts `helmlog`, prints status.

## Full Setup (if systemd units or apt packages changed)

```bash
ssh <pi-user>@<pi-host>
cd ~/helmlog
./scripts/setup.sh && sudo systemctl daemon-reload && sudo systemctl restart helmlog
```

## Service Architecture

The service runs as a dedicated `helmlog` system account (not the login user):

- **systemd unit**: `User=helmlog`, `UV_CACHE_DIR=/var/cache/helmlog`, `--no-sync`
- **data/**: owned by `helmlog:helmlog`; rest of project tree is read-only
- **.env**: `chmod 600 <pi-user>:<pi-user>`; systemd reads as root before dropping privileges
- **sudo**: `<pi-user>` has scoped access via `/etc/sudoers.d/helmlog-allowed`

## Networking

- **Signal K**: `127.0.0.1:3000` — exposed publicly via Tailscale Funnel at `/signalk/`
- **InfluxDB**: `127.0.0.1:8086` only
- **Grafana**: `127.0.0.1:3001` only
- **helmlog web**: `0.0.0.0:3002`
- **Public ingress**: Tailscale Funnel (path stripping built-in)

## Auth

- **Grafana**: anonymous disabled (`GF_AUTH_ANONYMOUS_ENABLED=false` via systemd)
- **Signal K**: `@signalk/sk-simple-token-security`; admin password in `~/.signalk-admin-pass.txt`

## Service Management

```bash
# Check status
sudo systemctl status helmlog

# View logs
sudo journalctl -u helmlog -f

# Restart
sudo systemctl restart helmlog

# Rollback to previous commit
git log --oneline -5
git checkout <previous-commit>
./scripts/deploy.sh
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Service won't start | Check `journalctl -u helmlog -n 50` for errors |
| `uv` not found | Ensure `UV_CACHE_DIR` is set in systemd unit |
| `ModuleNotFoundError` for a dep in pyproject.toml | Run `uv sync` — the venv is stale. The service uses `--no-sync` and trusts the venv. Never use `uv pip install` as a workaround |
| Permission denied on `data/` | Run `sudo chown -R helmlog:helmlog data/` |
| Signal K unreachable | Check `systemctl status signalk-server` |
| Web UI 502 | Service crashed — `sudo systemctl restart helmlog` |
