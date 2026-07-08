---
name: deploy-pi
description: Reference for deploying to the Raspberry Pi
disable-model-invocation: true
---

# Deploying to the Raspberry Pi

## Deploy (preferred: the web Deploy admin)

Deploys are driven from the **Deploy admin page** — `/admin/deployment`
(nav → "Deploy") — **not by hand on the Pi**. The Pi runs an evergreen
auto-deployer that polls the tracked branch (~every 300 s) and deploys when it
changes.

- **Point the Pi at a branch:** in the CONFIGURATION panel set **Track branch**
  (and **Mode**), then Save. In **evergreen** mode it auto-deploys within one
  poll interval whenever that branch advances. The CURRENT VERSION panel shows
  the running/tracking sha and last-deploy status; the button reads "Up to
  date" when nothing is pending and offers a deploy when the branch is ahead.
- **Promote `main → stage → live`:** use the PROMOTION PIPELINE on the same page
  (the `main → stage` / `stage → live` links) — promotions run via GitHub
  Actions, not manual git. The `main → stage` gate needs a `RELEASES.md` entry
  (see `/release-notes`); `stage → live` has no gate.

So "deploy #N to corvopi-live" = set Track branch to the merged branch (usually
`main`) on the Deploy page; switching branches is a config change there, not an
ssh session.

## Manual deploy (fallback — only when the web Deploy admin is unreachable)

```bash
ssh <pi-user>@<pi-host>
cd ~/helmlog
./scripts/deploy.sh
```

The script: pulls the tracked branch, syncs Python deps, re-applies Tailscale
Funnel routes, updates `PUBLIC_URL` in `.env`, restarts `helmlog`, prints status.

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
