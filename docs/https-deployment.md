# HTTPS Deployment Guide

The HelmLog web interface (`WEB_PORT=3002` by default) must be served over
HTTPS before exposing it to the public internet.  The application itself does
not terminate TLS — choose one of the three approaches below.

---

## Option A — Caddy Reverse Proxy (simplest on Pi)

Caddy automatically obtains and renews a Let's Encrypt certificate.

### Install Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

### Configure (`/etc/caddy/Caddyfile`)

```
logger.yourboat.com {
    reverse_proxy localhost:3002
}
```

### Enable & start

```bash
sudo systemctl enable caddy
sudo systemctl start caddy
```

Caddy listens on ports 80 and 443.  Make sure both are open in your router /
firewall and that `logger.yourboat.com` has an A record pointing at the Pi's
public IP.

---

## Option B — Cloudflare Tunnel (zero port-forwarding)

Cloudflare Tunnel works behind CGNAT and requires no inbound firewall rules.

### Install cloudflared

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
```

### Create and configure the tunnel

```bash
# Authenticate (opens browser)
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create helmlog

# Create config at ~/.cloudflared/config.yml
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: helmlog
credentials-file: /home/pi/.cloudflared/<tunnel-id>.json
protocol: http2

ingress:
  - hostname: logger.yourboat.com
    service: http://localhost:8080
  - service: http_status:404
EOF

# Add DNS record
cloudflared tunnel route dns helmlog logger.yourboat.com

# Run as a service
sudo cloudflared service install
sudo systemctl start cloudflared
```

Traffic is routed through **nginx on port 8080** which handles path-based
routing — stripping `/grafana/` and `/signalk/` prefixes before proxying to
the respective backend services. `deploy.sh` writes the nginx config at
`/etc/nginx/conf.d/cloudflare-tunnel.conf` automatically. See the
[Corvo hotspot guide](corvo-hotspot-network-setup.md) for the full nginx config.

No router changes needed.  HTTPS is provided by Cloudflare.

---

## Option C — Tailscale Funnel (recommended — handled automatically by setup.sh)

Tailscale Funnel exposes the Pi to the public internet under a permanent
`ts.net` HTTPS URL — no separate domain or certificate needed.

**`setup.sh` and `deploy.sh` both configure this automatically** when Tailscale
is already authenticated on the Pi. The three public routes are:

| Path | Local service | Purpose |
|---|---|---|
| `/` | port 3002 | helmlog race marker |
| `/grafana/` | port 3001 | Grafana dashboards |
| `/signalk/` | port 3000 | Signal K explorer |

Run `tailscale funnel status` to confirm, or let `setup.sh` report it.

**Prerequisite (one-time per Pi):**

```bash
sudo tailscale set --operator=$USER
```

This is also done automatically by `setup.sh`, but if running the commands
manually for the first time, do this step first.

**To manually configure:**

```bash
sudo tailscale set --operator=$USER
tailscale funnel --bg 3002
tailscale funnel --bg --set-path /grafana/ 3001
tailscale funnel --bg --set-path /signalk/ 3000
```

Your logger is then accessible at `https://<pi-hostname>.<tailnet>.ts.net`. Run
`tailscale funnel status` to see the exact public URL.

> **Note**: Tailscale Funnel strips the path prefix when proxying — Grafana
> receives `/d/...` not `/grafana/d/...`. This is handled automatically by
> `setup.sh`/`deploy.sh` which write the correct `GF_SERVER_ROOT_URL` to
> Grafana's `port.conf`. **Do not** set `GF_SERVER_SERVE_FROM_SUB_PATH=true`
> — it causes redirect loops with this Funnel setup.

---

## Setting up the first admin user

After deploying, create the first admin account with the CLI:

```bash
helmlog create-admin --email you@example.com
```

Or set `ADMIN_EMAIL=you@example.com` in `.env` — on startup the logger will
auto-create an admin user with that email and print a one-time invite link to
the log.

---

## Auth environment variables

| Variable | Default | Description |
|---|---|---|
| `AUTH_DISABLED` | `false` | Set to `true` to skip auth entirely (Tailscale-only installs) |
| `AUTH_SESSION_TTL_DAYS` | `90` | How long session cookies stay valid |
| `ADMIN_EMAIL` | — | Auto-create admin user on startup |

> **Tip:** If you're running the logger exclusively over Tailscale and don't
> need public access, set `AUTH_DISABLED=true` in `.env` to restore the
> original zero-friction behaviour.
