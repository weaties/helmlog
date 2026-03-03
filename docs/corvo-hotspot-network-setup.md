# Corvo Hotspot & Network Setup

Raspberry Pi as a cellular-backed WiFi hotspot using iPhone USB tethering, with
split-horizon DNS so `corvo.saillog.io` resolves locally on the hotspot and
publicly via Cloudflare Tunnel everywhere else.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Raspberry Pi (corvo)                                        │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌─────────────────────────┐    │
│  │ hostapd  │  │ dnsmasq  │  │  nginx                  │    │
│  │ (wlan0)  │  │ DHCP+DNS │  │  192.168.4.1:443 (TLS)  │    │
│  └──────────┘  └──────────┘  │  127.0.0.1:8080 (CF)    │    │
│        │              │      └─────────────────────────┘    │
│        └──────────────┴──────────────┤                       │
│                                      │                       │
│  ┌──────────┐   ┌───────────┐   ┌────┴──────────────────┐   │
│  │ eth1     │   │cloudflared│   │ /grafana/ → :3001     │   │
│  │ (iPhone) │   │ → :8080   │   │ /signalk/ → :3000     │   │
│  └──────────┘   └───────────┘   │ /         → :3002     │   │
│        │                        └───────────────────────┘   │
│  ┌──────────┐                                                │
│  │ tailscale│  Funnel strips prefixes directly:              │
│  │ (tailnet)│  /grafana/ → :3001, /signalk/ → :3000          │
│  └──────────┘                                                │
└──────────────────────────────────────────────────────────────┘

Hotspot clients:  corvo.live.saillog.io → 192.168.4.1 (local, via dnsmasq)
                    → nginx strips /grafana/, /signalk/ prefixes → backends
Remote users:     corvo.saillog.io → Cloudflare edge → tunnel → nginx :8080
                    → nginx strips /grafana/, /signalk/ prefixes → backends
Tailscale users:  corvopi.<tailnet>.ts.net → Funnel strips prefixes → backends
```

> **Why Cloudflare Tunnel?** Mint Mobile (T-Mobile MVNO) uses CGNAT — inbound
> connections to the cellular IP are blocked. The tunnel creates an outbound
> connection from the Pi to Cloudflare's edge, so no inbound ports are needed.

---

## Prerequisites

```bash
sudo apt install usbmuxd libimobiledevice-utils
```

These are needed for the Pi to communicate with the iPhone over USB.

---

## Step 1 — iPhone USB Tethering

Plug the iPhone into the Pi via a **data-capable** USB cable (not charge-only).

1. On the iPhone: **Settings → Personal Hotspot → On**
2. Tap **Trust This Computer** when prompted
3. Verify the interface:

```bash
ip link          # look for eth1
ip addr show eth1  # should show 172.20.10.x/28
ping -I eth1 8.8.8.8
```

> The iPhone presents as `eth1` via the `ipheth` kernel driver (not `usb0`).
> If `idevicepair pair` says "No device found", try a different cable —
> many charging cables don't support data.

The iPhone stays plugged in and charges from the Pi's USB port.

---

## Step 2 — Static IP on wlan0

NetworkManager manages the Pi's network by default. Tell it to leave `wlan0`
alone so hostapd can manage it instead.

Create `/etc/NetworkManager/conf.d/unmanaged-wlan0.conf`:

```ini
[keyfile]
unmanaged-devices=interface-name:wlan0
```

Set a static IP via systemd-networkd. Create `/etc/systemd/network/10-wlan0-hotspot.network`:

```ini
[Match]
Name=wlan0

[Network]
Address=192.168.4.1/24
```

```bash
sudo systemctl enable systemd-networkd
sudo systemctl restart NetworkManager
sudo systemctl restart systemd-networkd
```

---

## Step 3 — hostapd (WiFi Access Point)

```bash
sudo apt install hostapd
sudo systemctl unmask hostapd
sudo systemctl enable hostapd
```

Create `/etc/hostapd/hostapd.conf`:

```ini
interface=wlan0
driver=nl80211
ssid=Corvo
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=YOUR_WIFI_PASSWORD
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
```

> 2.4 GHz (`hw_mode=g`) has better range — matters on a boat. Change `ssid` and
> `wpa_passphrase`.

```bash
echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' | sudo tee /etc/default/hostapd
```

---

## Step 4 — dnsmasq (DHCP + Split-Horizon DNS)

This is the key piece. Hotspot clients get the Pi as their DNS server via DHCP.
dnsmasq resolves `corvo.saillog.io` to the Pi's local IP, so web app traffic
stays on the LAN. No client modification needed.

```bash
sudo apt install dnsmasq
```

Create `/etc/dnsmasq.d/hotspot.conf`:

```ini
# Only listen on the hotspot interface
interface=wlan0
bind-interfaces

# DHCP range for hotspot clients
dhcp-range=192.168.4.10,192.168.4.150,255.255.255.0,24h

# ============================================
# SPLIT-HORIZON DNS
# Hotspot clients resolve this to the Pi.
# Everyone else hits Cloudflare Tunnel.
# ============================================
address=/corvo.saillog.io/192.168.4.1

# Upstream DNS for everything else
server=8.8.8.8
server=8.8.4.4
```

Disable systemd-resolved if it's holding port 53:

```bash
sudo systemctl disable systemd-resolved
sudo systemctl stop systemd-resolved
sudo rm /etc/resolv.conf
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq
```

---

## Step 5 — NAT & IP Forwarding

NAT masquerades hotspot traffic through the iPhone tethering interface (`eth1`).

```bash
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/90-hotspot.conf
sudo sysctl -p /etc/sysctl.d/90-hotspot.conf

sudo apt install iptables-persistent
sudo iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE
sudo iptables -A FORWARD -i wlan0 -o eth1 -j ACCEPT
sudo iptables -A FORWARD -i eth1 -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo netfilter-persistent save
```

---

## Step 6 — TLS Certificate (Let's Encrypt + Cloudflare DNS-01)

> **Optional** — only needed if you want `https://corvo.saillog.io` to work
> with a real TLS certificate. For hotspot-only use, the j105-logger web UI at
> `http://192.168.4.1:3002` works without TLS.

DNS-01 challenges work even when the Pi isn't publicly reachable on port 80.
Certbot creates a TXT record via the Cloudflare API to prove ownership.
This cert is used by nginx for hotspot clients hitting the Pi directly.

```bash
sudo apt install certbot python3-certbot-dns-cloudflare
```

Create `/etc/letsencrypt/cloudflare.ini`:

```ini
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
```

```bash
sudo chmod 600 /etc/letsencrypt/cloudflare.ini
```

Create the token at https://dash.cloudflare.com/profile/api-tokens with
**Zone → DNS → Edit** scoped to `saillog.io`.

```bash
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d corvo.saillog.io \
  --preferred-challenges dns-01 \
  --non-interactive \
  --agree-tos \
  -m weaties@gmail.com
```

Verify auto-renewal: `sudo systemctl status certbot.timer`

---

## Step 7 — nginx Reverse Proxy

nginx serves two roles:

1. **TLS reverse proxy for hotspot clients** — serves `corvo.live.saillog.io`
   on the hotspot IP (`192.168.4.1`) with path-based routing to Grafana and
   Signal K.
2. **Path-routing proxy for Cloudflare Tunnel** — listens on `127.0.0.1:8080`
   and strips `/grafana/` and `/signalk/` prefixes before proxying to the
   respective backend services. cloudflared routes all `corvo.saillog.io`
   traffic here (see Step 8).

```bash
sudo apt install nginx
```

### Hotspot site — `/etc/nginx/sites-available/corvo`

```nginx
server {
    listen 192.168.4.1:80;
    server_name corvo.live.saillog.io;
    return 301 https://$host$request_uri;
}

server {
    listen 192.168.4.1:443 ssl;
    server_name corvo.live.saillog.io;

    ssl_certificate     /etc/letsencrypt/live/corvo.live.saillog.io/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/corvo.live.saillog.io/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location /grafana/ {
        proxy_pass http://127.0.0.1:3001/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /signalk/ {
        proxy_pass http://127.0.0.1:3000/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:3002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Cloudflare Tunnel proxy — `/etc/nginx/conf.d/cloudflare-tunnel.conf`

This config is written automatically by `deploy.sh` on every deploy.

```nginx
server {
    listen 127.0.0.1:8080;

    location /grafana/ {
        proxy_pass http://127.0.0.1:3001/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /signalk/ {
        proxy_pass http://127.0.0.1:3000/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:3002;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -sf /etc/nginx/sites-available/corvo /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx
```

Auto-reload after cert renewal — create
`/etc/letsencrypt/renewal-hooks/post/reload-nginx.sh`:

```bash
#!/bin/bash
systemctl reload nginx
```

```bash
sudo chmod +x /etc/letsencrypt/renewal-hooks/post/reload-nginx.sh
```

> **Why bind to `192.168.4.1` only?** Tailscale Funnel already binds to
> port 443 on the Tailscale IP. Binding nginx to the hotspot IP avoids
> the conflict.

---

## Step 8 — Cloudflare Tunnel (Public Access)

Cloudflare Tunnel creates an outbound connection from the Pi to Cloudflare's
edge network. This bypasses CGNAT — no inbound ports needed on the cellular
connection.

### Install cloudflared

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb \
  -o /tmp/cloudflared.deb && sudo dpkg -i /tmp/cloudflared.deb
```

### Authenticate and create the tunnel

```bash
cloudflared tunnel login
# Opens a URL — authorize for saillog.io in your browser

cloudflared tunnel create corvo
# Note the tunnel ID (e.g., 0e036eca-49cc-41f1-9cdb-f2f3fd340fe3)
```

### Configure the tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: YOUR_TUNNEL_ID
credentials-file: /home/weaties/.cloudflared/YOUR_TUNNEL_ID.json
protocol: http2

ingress:
  - hostname: corvo.saillog.io
    service: http://127.0.0.1:8080
  - service: http_status:404
```

Traffic routes through **nginx on port 8080** which handles path-based routing:
`/grafana/` → Grafana (3001), `/signalk/` → Signal K (3000), everything else →
j105-logger (3002). This is needed because cloudflared passes request paths
through unchanged (unlike Tailscale Funnel which strips path prefixes), and
Grafana doesn't serve from the `/grafana/` sub-path. The nginx proxy strips the
prefix before forwarding. See Step 7 for the nginx config.

`deploy.sh` manages this config automatically — it reads the tunnel ID and
credentials from the existing config, writes the updated ingress rules to both
`/etc/cloudflared/config.yml` (used by the systemd service) and
`~/.cloudflared/config.yml` (used for manual runs), then restarts cloudflared.

> **Why `protocol: http2`?** Cellular carriers (T-Mobile/Mint) often
> throttle or block UDP, which breaks the default QUIC protocol.
> HTTP/2 over TCP works reliably on cellular connections.

### Route DNS and install as service

```bash
# Create the CNAME record in Cloudflare
cloudflared tunnel route dns corvo corvo.saillog.io

# Install as a system service
sudo cloudflared --config /home/weaties/.cloudflared/config.yml service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

> **Note on subdomain depth:** `corvo.saillog.io` (one level deep) is covered
> by Cloudflare's free Universal SSL certificate (`*.saillog.io`). A deeper
> subdomain like `corvo.live.saillog.io` would require Cloudflare's paid
> Advanced Certificate Manager ($10/mo).

---

## Step 9 — Tailscale Considerations

Tailscale won't interfere — it uses its own `tailscale0` interface and routing
table. SSH access via the Tailscale IP works regardless of hotspot/modem state.
The web app is also reachable at the Tailscale Funnel URL with no extra config.

If Tailscale DNS overrides local resolution:

```bash
sudo tailscale set --accept-dns=false
```

---

## Automation

Steps 1–8 are automated in `scripts/setup.sh` (section a.2). The script:
- Installs `usbmuxd`, `libimobiledevice-utils`, `hostapd`, `dnsmasq`, `iptables-persistent`, `nginx`, `certbot`, `cloudflared`
- Configures NetworkManager to leave wlan0 unmanaged
- Sets the static IP via systemd-networkd
- Writes the dnsmasq hotspot config
- Enables IP forwarding and NAT rules
- Configures nginx TLS reverse proxy for hotspot clients
- Installs cloudflared for Cloudflare Tunnel public access
- Enables all services for boot

The only manual steps are:
- Creating `/etc/hostapd/hostapd.conf` with your chosen SSID and password (setup.sh warns if it's missing)
- Running `cloudflared tunnel login` and `cloudflared tunnel create corvo` (interactive auth required)
- Creating the initial TLS certificate via certbot (setup.sh warns if missing)

---

## Verification Checklist

```bash
ip addr show eth1 && ping -I eth1 8.8.8.8          # iPhone tethering works
sudo systemctl status hostapd                        # AP running
sudo systemctl status dnsmasq                        # DHCP+DNS running
nslookup corvo.saillog.io 192.168.4.1               # → 192.168.4.1
sudo certbot certificates                            # cert valid
curl -k https://192.168.4.1                          # nginx proxying locally
sudo systemctl status cloudflared                    # tunnel running
curl https://corvo.saillog.io                        # tunnel proxying publicly
tailscale status                                     # tailscale up
```

---

## Troubleshooting

| Problem | Check |
|---|---|
| No `eth1` interface | Unplug/replug iPhone; tap Trust; check cable supports data (`lsusb \| grep Apple`) |
| No internet for hotspot clients | `iptables -t nat -L` for MASQUERADE on `eth1`; `sysctl net.ipv4.ip_forward` = 1 |
| Domain doesn't resolve locally | `nslookup corvo.saillog.io 192.168.4.1` should return `192.168.4.1` |
| TLS errors on hotspot | `certbot certificates`; nginx paths match cert location? |
| Tunnel won't connect (QUIC timeout) | Set `protocol: http2` in `~/.cloudflared/config.yml` |
| Tunnel connected but TLS error publicly | Ensure hostname is one level deep (`*.saillog.io`); check Cloudflare SSL mode = Full |
| hostapd fails | `journalctl -u hostapd` — driver/channel conflict |
| dnsmasq won't start | `ss -tlnp \| grep :53` — port 53 conflict (systemd-resolved?) |
| nginx won't start (port 443 conflict) | Bind to `192.168.4.1:443` only — Tailscale Funnel holds `443` on the Tailscale IP |

---

## File Reference

| File | Purpose |
|---|---|
| `/etc/hostapd/hostapd.conf` | WiFi AP config (SSID, password, channel) |
| `/etc/default/hostapd` | Points to hostapd.conf |
| `/etc/dnsmasq.d/hotspot.conf` | DHCP + split-horizon DNS |
| `/etc/sysctl.d/90-hotspot.conf` | IP forwarding |
| `/etc/nginx/sites-available/corvo` | TLS + reverse proxy (hotspot clients) |
| `/etc/nginx/conf.d/cloudflare-tunnel.conf` | Path-routing proxy for Cloudflare Tunnel (managed by deploy.sh) |
| `/etc/letsencrypt/cloudflare.ini` | Cloudflare API creds for certbot |
| `/etc/letsencrypt/renewal-hooks/post/reload-nginx.sh` | Reload nginx on cert renewal |
| `~/.cloudflared/config.yml` | Cloudflare Tunnel config |
| `~/.cloudflared/cert.pem` | Cloudflare Tunnel auth cert |
| `~/.cloudflared/<tunnel-id>.json` | Tunnel credentials |
| `/etc/systemd/system/cloudflared.service` | Tunnel systemd service (auto-created) |
