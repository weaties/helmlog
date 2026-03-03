# Corvo Hotspot & Network Setup

Raspberry Pi as a cellular-backed WiFi hotspot with split-horizon DNS so
`corvo.live.saillog.io` resolves locally on the hotspot and publicly everywhere else.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Raspberry Pi (corvo)                                   │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐            │
│  │ hostapd  │  │ dnsmasq  │  │  nginx     │            │
│  │ (wlan0)  │  │ DHCP+DNS │  │  TLS proxy │            │
│  └──────────┘  └──────────┘  └────────────┘            │
│        │              │              │                   │
│        └──────────────┴──────────────┘                  │
│                       │                                  │
│  ┌──────────┐   ┌──────────┐   ┌───────────┐           │
│  │ usb0     │   │ tailscale│   │ web app   │           │
│  │ (modem)  │   │ (tailnet)│   │ :3000     │           │
│  └──────────┘   └──────────┘   └───────────┘           │
└─────────────────────────────────────────────────────────┘

Hotspot clients:  corvo.live.saillog.io → 192.168.4.1 (local, via dnsmasq)
Remote users:     corvo.live.saillog.io → Cloudflare → modem public IP → Pi
```

---

## Step 1 — USB Modem

Plug it in, check what interface it presents:

```bash
ip link  # Look for usb0, wwan0, or similar
```

If it needs ModemManager:

```bash
sudo apt install modemmanager
mmcli -L
mmcli -m 0
sudo nmcli con add type gsm ifname '*' con-name cellular apn <YOUR_APN>
sudo nmcli con up cellular
```

Replace `<YOUR_APN>` with your carrier's APN (`hologram`, `fast.t-mobile.com`,
`broadband`, etc.).

Verify: `ping -I usb0 8.8.8.8`

> The modem interface name `usb0` is used throughout. If yours is `wwan0` or
> similar, substitute everywhere.

---

## Step 2 — Static IP on wlan0

Create `/etc/network/interfaces.d/wlan0`:

```
auto wlan0
iface wlan0 inet static
    address 192.168.4.1
    netmask 255.255.255.0
```

Or for dhcpcd (older Pi OS), add to `/etc/dhcpcd.conf`:

```
interface wlan0
    static ip_address=192.168.4.1/24
    nohook wpa_supplicant
```

Keep NetworkManager off wlan0. Create `/etc/NetworkManager/conf.d/unmanaged.conf`:

```ini
[keyfile]
unmanaged-devices=interface-name:wlan0
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
dnsmasq resolves `corvo.live.saillog.io` to the Pi's local IP, so web app traffic
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
# Everyone else hits Cloudflare normally.
# ============================================
address=/corvo.live.saillog.io/192.168.4.1

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

```bash
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/90-hotspot.conf
sudo sysctl -p /etc/sysctl.d/90-hotspot.conf

sudo apt install iptables-persistent
sudo iptables -t nat -A POSTROUTING -o usb0 -j MASQUERADE
sudo iptables -A FORWARD -i wlan0 -o usb0 -j ACCEPT
sudo iptables -A FORWARD -i usb0 -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo netfilter-persistent save
```

---

## Step 6 — TLS Certificate (Let's Encrypt + Cloudflare DNS-01)

DNS-01 challenges work even when the Pi isn't publicly reachable on port 80.
Certbot creates a TXT record via the Cloudflare API to prove ownership.

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
  -d corvo.live.saillog.io \
  --preferred-challenges dns-01
```

Verify auto-renewal: `sudo systemctl status certbot.timer`

---

## Step 7 — nginx Reverse Proxy

```bash
sudo apt install nginx
```

Create `/etc/nginx/sites-available/corvo`:

```nginx
server {
    listen 80;
    server_name corvo.live.saillog.io;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name corvo.live.saillog.io;

    ssl_certificate     /etc/letsencrypt/live/corvo.live.saillog.io/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/corvo.live.saillog.io/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location / {
        proxy_pass http://127.0.0.1:3000;
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
sudo ln -s /etc/nginx/sites-available/corvo /etc/nginx/sites-enabled/
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

---

## Step 8 — Cloudflare DNS (Public Side)

In the Cloudflare dashboard for `saillog.io`, add an **A record**:
`corvo.live` → modem's public IP. Use **DNS only** (grey cloud) for direct
connections or **Proxied** (orange cloud) for DDoS protection.

### Dynamic DNS Script

Most cellular IPs are dynamic. Create `/usr/local/bin/cloudflare-ddns.sh`:

```bash
#!/bin/bash
CF_API_TOKEN="YOUR_CLOUDFLARE_API_TOKEN"
ZONE_ID="YOUR_ZONE_ID"
RECORD_NAME="corvo.live.saillog.io"
MODEM_IF="usb0"

CURRENT_IP=$(curl -s --interface "$MODEM_IF" https://api.ipify.org)
[ -z "$CURRENT_IP" ] && echo "$(date): No IP" >&2 && exit 1

CACHE_FILE="/tmp/ddns-last-ip"
LAST_IP=$(cat "$CACHE_FILE" 2>/dev/null)
[ "$CURRENT_IP" = "$LAST_IP" ] && exit 0

RECORD_ID=$(curl -s -X GET \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records?name=${RECORD_NAME}" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" | jq -r '.result[0].id')

[ -z "$RECORD_ID" ] || [ "$RECORD_ID" = "null" ] && echo "$(date): No record ID" >&2 && exit 1

RESULT=$(curl -s -X PUT \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records/${RECORD_ID}" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data "{\"type\":\"A\",\"name\":\"${RECORD_NAME}\",\"content\":\"${CURRENT_IP}\",\"ttl\":300}")

if [ "$(echo "$RESULT" | jq -r '.success')" = "true" ]; then
    echo "$CURRENT_IP" > "$CACHE_FILE"
    echo "$(date): Updated to ${CURRENT_IP}"
else
    echo "$(date): Failed: $RESULT" >&2 && exit 1
fi
```

```bash
sudo chmod +x /usr/local/bin/cloudflare-ddns.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/cloudflare-ddns.sh >> /var/log/ddns.log 2>&1") | crontab -
```

---

## Step 9 — Tailscale Considerations

Tailscale won't interfere — it uses its own `tailscale0` interface and routing
table. SSH access via the Tailscale IP works regardless of hotspot/modem state.
The web app is also reachable at `http://<tailscale-ip>:3000` with no extra config.

If Tailscale DNS overrides local resolution:

```bash
sudo tailscale set --accept-dns=false
```

---

## Verification Checklist

```bash
ip addr show usb0 && ping -I usb0 8.8.8.8          # modem works
sudo systemctl status hostapd                        # AP running
sudo systemctl status dnsmasq                        # DHCP+DNS running
dig @192.168.4.1 corvo.live.saillog.io               # → 192.168.4.1
dig @8.8.8.8 corvo.live.saillog.io                   # → modem public IP
sudo certbot certificates                            # cert valid
curl https://corvo.live.saillog.io                   # nginx proxying
tailscale status                                     # tailscale up
sudo iptables -t nat -L -v                           # NAT rules present
```

---

## Troubleshooting

| Problem | Check |
|---|---|
| No internet for hotspot clients | `iptables -t nat -L` for MASQUERADE; `sysctl net.ipv4.ip_forward` = 1 |
| Domain doesn't resolve locally | `dig @192.168.4.1 corvo.live.saillog.io` should return `192.168.4.1` |
| TLS errors on hotspot | `certbot certificates`; nginx paths match cert location? |
| Modem won't connect | `mmcli -m 0` for signal/status; verify APN |
| hostapd fails | `journalctl -u hostapd` — driver/channel conflict |
| dnsmasq won't start | `ss -tlnp | grep :53` — port 53 conflict (systemd-resolved?) |
| DDNS not updating | `/var/log/ddns.log`; verify token + zone ID |

---

## File Reference

| File | Purpose |
|---|---|
| `/etc/hostapd/hostapd.conf` | WiFi AP config |
| `/etc/dnsmasq.d/hotspot.conf` | DHCP + split-horizon DNS |
| `/etc/sysctl.d/90-hotspot.conf` | IP forwarding |
| `/etc/nginx/sites-available/corvo` | TLS + reverse proxy |
| `/etc/letsencrypt/cloudflare.ini` | Cloudflare API creds for certbot |
| `/etc/letsencrypt/renewal-hooks/post/reload-nginx.sh` | Reload nginx on cert renewal |
| `/usr/local/bin/cloudflare-ddns.sh` | Dynamic DNS updater |
