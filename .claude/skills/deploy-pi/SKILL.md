---
name: deploy-pi
description: Reference for deploying to the Raspberry Pi
disable-model-invocation: true
---

# Deploying to the Raspberry Pi

## Quick Deploy (after PR merges to main)

```bash
ssh weaties@corvopi
cd ~/j105-logger
./scripts/deploy.sh
```

The script: pulls `main`, syncs Python deps, re-applies Tailscale Funnel
routes, updates `PUBLIC_URL` in `.env`, restarts `j105-logger`, prints status.

## Full Setup (if systemd units or apt packages changed)

```bash
ssh weaties@corvopi
cd ~/j105-logger
./scripts/setup.sh && sudo systemctl daemon-reload && sudo systemctl restart j105-logger
```

## Service Architecture

The service runs as a dedicated `j105logger` system account (not `weaties`):

- **systemd unit**: `User=j105logger`, `UV_CACHE_DIR=/var/cache/j105-logger`, `--no-sync`
- **data/**: owned by `j105logger:j105logger`; rest of project tree is read-only
- **.env**: `chmod 600 weaties:weaties`; systemd reads as root before dropping privileges
- **sudo**: `weaties` has scoped access via `/etc/sudoers.d/j105-logger-allowed`

## Networking

- **Signal K**: `*:3000` — exposed via Tailscale Funnel at `/signalk/`
- **InfluxDB**: `127.0.0.1:8086` only
- **Grafana**: `127.0.0.1:3001` only
- **j105-logger web**: `0.0.0.0:3002`
- **Two public ingress paths**:
  - Tailscale Funnel (path stripping built-in)
  - Cloudflare Tunnel via nginx on `127.0.0.1:8080` (strips `/grafana/` and `/signalk/` prefixes)
- **nginx config**: `/etc/nginx/conf.d/cloudflare-tunnel.conf` (managed by `deploy.sh`)

## Auth

- **Grafana**: anonymous disabled (`GF_AUTH_ANONYMOUS_ENABLED=false` via systemd)
- **Signal K**: `@signalk/sk-simple-token-security`; admin password in `~/.signalk-admin-pass.txt`

## Service Management

```bash
# Check status
sudo systemctl status j105-logger

# View logs
sudo journalctl -u j105-logger -f

# Restart
sudo systemctl restart j105-logger

# Rollback to previous commit
git log --oneline -5
git checkout <previous-commit>
./scripts/deploy.sh
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Service won't start | Check `journalctl -u j105-logger -n 50` for errors |
| `uv` not found | Ensure `UV_CACHE_DIR` is set in systemd unit |
| Permission denied on `data/` | Run `sudo chown -R j105logger:j105logger data/` |
| Signal K unreachable | Check `systemctl status signalk-server` |
| Web UI 502 | Service crashed — `sudo systemctl restart j105-logger` |
