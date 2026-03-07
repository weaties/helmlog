# Security Audit — corvopi (HelmLog Raspberry Pi)
**Date:** 2026-03-01
**Auditor:** Internal self-audit (Claude Code)
**Scope:** All software running on `corvopi` (Raspberry Pi 5, Debian 13 Trixie, kernel 6.12.47)

---

## Executive Summary

The device has **one critical** finding that requires immediate action: the application is
deployed to the public internet via Tailscale Funnel with authentication completely disabled
(`AUTH_DISABLED=true`), exposing all race data, crew information, GPS tracks, audio recordings,
and photo notes — plus Signal K live instrument data — to anyone on the internet without a
password.

Beyond that root issue, there are several high and medium severity misconfigurations that would
remain problematic even after authentication is restored.

---

## Finding Index

| ID | Severity | Title | Issue |
|----|----------|-------|-------|
| F-01 | **CRITICAL** | Public internet exposure with `AUTH_DISABLED=true` | [#102](https://github.com/weaties/helmlog/issues/102) |
| F-02 | **HIGH** | `weaties` has passwordless sudo for all commands | [#103](https://github.com/weaties/helmlog/issues/103) |
| F-03 | **HIGH** | `.env` file is world-readable and contains live credentials | [#104](https://github.com/weaties/helmlog/issues/104) |
| F-04 | **HIGH** | WiFi PSK stored in plaintext in NetworkManager config | [#105](https://github.com/weaties/helmlog/issues/105) |
| F-05 | **HIGH** | Signal K has no authentication configured | [#106](https://github.com/weaties/helmlog/issues/106) |
| F-06 | **HIGH** | Grafana anonymous access enabled; publicly exposed | [#107](https://github.com/weaties/helmlog/issues/107) |
| F-07 | **MEDIUM** | InfluxDB / Grafana / Signal K bind to all interfaces | [#108](https://github.com/weaties/helmlog/issues/108) |
| F-08 | **MEDIUM** | `/notes/` photo endpoint always public (auth bypass) | [#109](https://github.com/weaties/helmlog/issues/109) |
| F-09 | **MEDIUM** | No rate limiting or brute-force protection | [#110](https://github.com/weaties/helmlog/issues/110) |
| F-10 | **MEDIUM** | SSH: X11 forwarding enabled, legacy RSA key accepted | [#111](https://github.com/weaties/helmlog/issues/111) |
| F-11 | **MEDIUM** | `~/.ssh/config` permissions too permissive | [#112](https://github.com/weaties/helmlog/issues/112) |
| F-12 | **MEDIUM** | No automatic security updates; kernel and libnss3 outdated | [#113](https://github.com/weaties/helmlog/issues/113) |
| F-13 | **MEDIUM** | Multiple unnecessary services running and exposed | [#114](https://github.com/weaties/helmlog/issues/114) |
| F-14 | **LOW** | LightDM auto-login: physical access = immediate desktop | — |
| F-15 | **LOW** | NMEA TCP (10110) and SK WebSocket (8375) on all interfaces | — |
| F-16 | **INFO** | Signal K server-side error exposed in HTTP response body | — |

---

## Detailed Findings

### F-01 — CRITICAL: Public internet exposure with AUTH_DISABLED=true · [#102](https://github.com/weaties/helmlog/issues/102)

**Evidence:**
```
# .env
AUTH_DISABLED=true
PUBLIC_URL=https://corvopi.taileb1513.ts.net

# Tailscale Funnel (confirmed active)
https://corvopi.taileb1513.ts.net (Funnel on)
|-- /         proxy http://127.0.0.1:3002   ← helmlog (auth disabled)
|-- /grafana/ proxy http://127.0.0.1:3001   ← Grafana (anon Viewer)
|-- /signalk/ proxy http://127.0.0.1:3000   ← Signal K (no auth configured)

# Confirmed unauthenticated access from the public internet:
$ curl https://corvopi.taileb1513.ts.net/api/sessions?limit=1
{"total": 83, "sessions": [{"name": "20260301-On Catherine's phone-2", ...}]}
```

**Impact:** Any person on the internet — without credentials — can:
- Read all 83 race sessions including crew names, start/end times, GPS tracks
- Download WAV audio recordings of all sessions
- Read all race notes and photo notes
- Trigger race start/stop via POST API
- Read and modify race results and boat registry
- Read live instrument data (wind, speed, position) from Signal K
- View all Grafana dashboards (sailing metrics, system health)

**Remediation:**
1. **Immediately:** Remove `AUTH_DISABLED=true` from `.env` and restart the service.
2. Generate invite tokens and set up proper user accounts.
3. Keep Tailscale Funnel only if authentication is active; otherwise restrict to Tailscale VPN only.

---

### F-02 — HIGH: Passwordless sudo for all commands · [#103](https://github.com/weaties/helmlog/issues/103)

**Evidence:**
```
# /etc/sudoers (effective)
weaties  ALL=(ALL) NOPASSWD: ALL   ← appears twice
weaties  ALL=(ALL) NOPASSWD: /usr/bin/rsync
```

**Impact:** Any process running as `weaties` (e.g., a compromised helmlog, Signal K plugin,
or SSH session obtained via stolen key) can gain full root without a password. This is a
Raspberry Pi default that should be hardened in production.

**Remediation:**
- Remove `NOPASSWD: ALL`. Set a strong sudo password for weaties.
- If passwordless sudo is required for specific operations (deploy script, CAN interface
  setup), scope it to those specific commands only:
  ```
  weaties ALL=(root) NOPASSWD: /bin/systemctl restart helmlog, /sbin/ip link set can0 up
  ```

---

### F-03 — HIGH: `.env` file is world-readable and contains live credentials · [#104](https://github.com/weaties/helmlog/issues/104)

**Evidence:**
```
-rw-rw-r-- 1 weaties weaties 293 Mar  1 02:19 /home/weaties/helmlog/.env

# Contents include:
INFLUX_TOKEN=95VqjUa38SeBwURyK1XsVB5_MfSq8dE3dDuWIW-2EC9zoiQuwhcTFFnPE_b4YZApejxsE1TaHktcfgRxXhYLXw==
AUTH_DISABLED=true
```

**Impact:** Any local user or process that can read `/home/weaties/helmlog/.env` gets the
InfluxDB admin token, which grants full read/write access to all time-series data.

**Remediation:**
```bash
chmod 600 /home/weaties/helmlog/.env
```
Consider rotating the InfluxDB token since it has been exposed.

---

### F-04 — HIGH: WiFi PSK stored in plaintext · [#105](https://github.com/weaties/helmlog/issues/105)

**Evidence:**
```
# /etc/NetworkManager/system-connections/Daniel's iPhone.nmconnection
[wifi-security]
key-mgmt=wpa-psk
psk=Comai2015
```

**Impact:** Anyone with local access (or any process running as root) can read the WiFi
pre-shared key. This is the hotspot password for the iPhone being used as a data connection.

**Remediation:**
- This is a NetworkManager default behaviour; the file is only root-readable by default, but
  since `weaties` has NOPASSWD sudo, it is trivially readable.
- Fixing F-02 (sudo) is the primary mitigation.
- For defence in depth, consider using a dedicated hotspot SSID for the Pi that is isolated
  from other devices, or rotate the PSK regularly.

---

### F-05 — HIGH: Signal K has no authentication configured · [#106](https://github.com/weaties/helmlog/issues/106)

**Evidence:**
```bash
$ ls ~/.signalk/security.json
# → No security.json exists

$ curl http://localhost:3000/signalk/v1/api/vessels/self
# → returns full vessel data: uuid, name, navigation, performance...
```

**Impact:** Signal K is running with no security model at all. Anyone who can reach port 3000
(local network, or via Tailscale Funnel at `/signalk/`) can read all NMEA data and submit
access requests. Signal K's `set-system-time` plugin is also enabled, meaning an unauthenticated
actor on the local network could potentially request time-setting via Signal K requests.

**Remediation:**
1. Navigate to `http://corvopi:3000` → Admin → Security → Enable security.
2. Create an admin user and enable access control.
3. Bind Signal K to localhost only (see F-07).

---

### F-06 — HIGH: Grafana anonymous access enabled and publicly exposed · [#107](https://github.com/weaties/helmlog/issues/107)

**Evidence:**
```ini
# /etc/grafana/grafana.ini
[auth.anonymous]
enabled = true
org_role = Viewer

disable_login_form = true   ← cannot log in as admin via web UI
;secret_key = CHANGE_ME_TO_A_RANDOM_SECRET  ← not changed from default placeholder
```

**Impact:**
- All Grafana dashboards (sailing metrics, system health) are readable by anyone via
  `https://corvopi.taileb1513.ts.net/grafana/`.
- `disable_login_form = true` means there is no way to log into Grafana as an admin through
  the web interface — the admin account is effectively inaccessible.
- The `secret_key` comment shows the default placeholder was never replaced, meaning session
  tokens are signed with a predictable or empty key.

**Remediation:**
1. Set `disable_login_form = false` and configure a strong admin password.
2. Generate and set a random `secret_key`.
3. Either disable anonymous access or restrict Grafana to Tailscale-VPN-only (remove from Funnel).

---

### F-07 — MEDIUM: Data services bind to all network interfaces · [#108](https://github.com/weaties/helmlog/issues/108)

**Evidence:**
```
# ss -tlnp
*:8086    ← InfluxDB  (all interfaces — local WiFi reachable)
*:3001    ← Grafana   (all interfaces — local WiFi reachable)
*:3000    ← Signal K  (all interfaces — local WiFi + Tailscale Funnel)
0.0.0.0:3002  ← helmlog (all interfaces)
*:10110   ← NMEA TCP  (all interfaces)
*:8375    ← SK WebSocket (all interfaces)
```

**Impact:** Any device on the same WiFi network as the Pi (172.20.10.0/28 hotspot subnet) can
directly reach InfluxDB, Grafana, Signal K, and NMEA TCP without going through Tailscale.
InfluxDB requires a token (auth works), but Grafana allows anonymous Viewer access and Signal K
has no auth.

**Remediation:**
- Bind services to `127.0.0.1` / `::1` (localhost) where they are only needed internally:
  - InfluxDB: `[http] bind-address = "127.0.0.1:8086"`
  - Grafana: `[server] http_addr = 127.0.0.1`
  - Signal K: configure to bind to `127.0.0.1` (settings.json `ssl: false`, bind option)
- Use nftables/iptables to block inbound connections to these ports from non-Tailscale interfaces.

---

### F-08 — MEDIUM: `/notes/` photo endpoint always bypasses authentication · [#109](https://github.com/weaties/helmlog/issues/109)

**Evidence:**
```python
# src/logger/auth.py (auth middleware)
_PUBLIC_PATHS = {"/login", "/logout", "/healthz"}

if _is_auth_disabled() or path in _PUBLIC_PATHS or path.startswith("/notes/"):
    return await call_next(request)  # no auth check
```

**Impact:** Even when `AUTH_DISABLED=false`, the `/notes/{path}` endpoint that serves uploaded
photo notes is unconditionally public. An attacker who knows (or enumerates) a note path can
download photos without authentication. Note paths are structured as
`/notes/{session_id}/{timestamp}_{uuid}.jpg`, which are guessable given knowledge of session IDs.

**Remediation:**
Remove `/notes/` from the middleware bypass. The `serve_note_photo` endpoint already has
path-traversal protection; it just needs an auth dependency added:

```python
# web.py — add require_auth to the /notes/ route
@app.get("/notes/{path:path}")
async def serve_note_photo(
    path: str,
    request: Request,
    _user: dict = Depends(require_auth("viewer")),  # add this
) -> Response:
    ...

# And remove the path.startswith("/notes/") bypass from the middleware
```

---

### F-09 — MEDIUM: No rate limiting or brute-force protection · [#110](https://github.com/weaties/helmlog/issues/110)

**Evidence:**
- `fail2ban` is not installed.
- No rate limiting middleware in `web.py` (no `slowapi`, no FastAPI rate limiter).
- SSH uses OpenSSH default limits only (`MaxAuthTries 6`).
- No `unattended-upgrades` for automatic security patching.

**Impact:** An attacker can make unlimited requests to:
- The SSH port (brute-force key type attacks, protocol fingerprinting)
- The HelmLog login endpoint (brute-force invite tokens)
- All API endpoints (scraping, DoS)

**Remediation:**
```bash
sudo apt install fail2ban
# Configure jails for sshd and the helmlog uvicorn log
```
Add rate limiting to the login endpoint in `web.py` using `slowapi`.

---

### F-10 — MEDIUM: SSH X11 forwarding enabled; legacy RSA key in authorized_keys · [#111](https://github.com/weaties/helmlog/issues/111)

**Evidence:**
```
# sshd effective config
x11forwarding yes
allowagentforwarding yes

# ~/.ssh/authorized_keys
ssh-rsa AAAA...  weaties+13inch-mbp@gmail.com   ← RSA-2048 (legacy)
ssh-ed25519 AAAA...                              ← Ed25519 (good)
```

**Impact:**
- X11 forwarding can allow a compromised SSH server to inject X11 events into a connecting
  client's display (MITM X11 attack). On a headless server this is rarely needed.
- RSA-2048 keys, while not yet broken, are weaker than Ed25519 and generate larger traffic.
  More importantly, if this key's private counterpart is ever compromised, it cannot be
  distinguished from a valid session.

**Remediation:**
```
# /etc/ssh/sshd_config.d/hardening.conf
X11Forwarding no
AllowAgentForwarding no
```
Remove the RSA key from `authorized_keys` once the Ed25519 key on the MacBook Pro is confirmed working.

---

### F-11 — MEDIUM: `~/.ssh/config` has incorrect permissions · [#112](https://github.com/weaties/helmlog/issues/112)

**Evidence:**
```
-rw-rw-r-- 1 weaties weaties 37 Feb 28 01:31 /home/weaties/.ssh/config
```
SSH config should be `600` (user read/write only). The group-writable bit means other members
of the `weaties` group (if any ever exist) could modify SSH connection settings.

**Remediation:**
```bash
chmod 600 ~/.ssh/config
```

---

### F-12 — MEDIUM: No automatic security updates; kernel and security packages outdated · [#113](https://github.com/weaties/helmlog/issues/113)

**Evidence:**
```
# apt list --upgradable (selected)
linux-image-rpi-2712   6.12.62  [upgradable from: 6.12.47]  ← 15 patch versions behind
libnss3                2:3.110-1+deb13u1 [upgradable from: 2:3.110-1]  ← security update
influxdb2              2.8.0    [upgradable from: 2.7.11]

# unattended-upgrades: not installed
```

**Impact:** CVEs fixed in kernel 6.12.48–6.12.62 and in libnss3 (used by Chromium, Firefox,
NSS-based TLS) are unpatched. Without unattended-upgrades, security fixes accumulate silently.

**Remediation:**
```bash
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
sudo apt upgrade linux-image-rpi-2712 libnss3
```

---

### F-13 — MEDIUM: Unnecessary services running and network-exposed · [#114](https://github.com/weaties/helmlog/issues/114)

**Evidence (running services not required for sailing logger function):**
| Service | Port | Risk |
|---------|------|------|
| CUPS printing | 127.0.0.1:631 | Attack surface; no printer on a sailboat |
| cups-browsed | — | Discovers and adds network printers automatically |
| rpcbind | 0.0.0.0:111 + *:111 | Required by NFS; NFS not in use |
| nfs-blkmap | — | NFS block layout; not in use |
| avahi-daemon | UDP 5353 mDNS | Broadcasts device info on local network |
| ModemManager | — | USB modem management; not required |
| bluetooth | — | Not in use; attack surface |
| lightdm | — | Full GUI desktop manager on a headless server |

**Remediation:**
```bash
sudo systemctl disable --now cups cups-browsed rpcbind nfs-blkmap avahi-daemon ModemManager bluetooth
# Keep lightdm only if the desktop is used intentionally via wayvnc
```

---

### F-14 — LOW: LightDM auto-login grants immediate desktop access

**Evidence:**
```ini
# /etc/lightdm/lightdm.conf
autologin-user=weaties
autologin-session=rpd-labwc
```

**Impact:** Physical access to the Pi (e.g., if the boat is boarded) provides immediate desktop
access as `weaties` with full sudo. No lock screen or password prompt.

**Remediation:** Acceptable for a boat embedded system if physical security is adequate.
For defence in depth, disable autologin and require a PIN or password for the display session,
or disable LightDM entirely if the desktop is not used.

---

### F-15 — LOW: NMEA TCP and Signal K WebSocket exposed on all interfaces

**Evidence:**
```
*:10110   NMEA TCP stream (all interfaces)
*:8375    Signal K WebSocket (all interfaces)
```

**Impact:** Anyone on the local WiFi can connect to port 10110 and receive a raw NMEA 0183
stream, or subscribe to the Signal K WebSocket for live data. This is information disclosure
on a local marina network.

**Remediation:** Bind NMEA TCP and SK WebSocket to localhost or the Tailscale interface
(`100.122.21.5`) only. Adjust in Signal K settings.

---

### F-16 — INFO: Signal K stack traces returned in HTTP responses

**Evidence:**
```
$ curl -X POST http://localhost:3000/signalk/v1/access/requests ...
{"error": "TypeError: Cannot read properties of undefined (reading 'then')\n
  at /usr/lib/node_modules/signalk-server/dist/serverroutes.js:370:13 ..."}
```

**Impact:** Internal file paths, library names, and line numbers are revealed. Useful to an
attacker for fingerprinting the exact Signal K version and identifying known CVEs.

**Remediation:** Update Signal K server to the latest version. In Node.js production
deployments, set `NODE_ENV=production` to suppress stack traces in error responses.

---

## Risk Summary Matrix

```
CRITICAL  ██████████  F-01: Public internet + AUTH_DISABLED=true
HIGH      ████████    F-02: NOPASSWD sudo
HIGH      ████████    F-03: World-readable .env with credentials
HIGH      ████████    F-04: WiFi PSK in plaintext
HIGH      ████████    F-05: Signal K no auth
HIGH      ████████    F-06: Grafana anon access + publicly exposed
MEDIUM    ██████      F-07: Services on all interfaces
MEDIUM    ██████      F-08: /notes/ auth bypass
MEDIUM    ██████      F-09: No rate limiting / fail2ban
MEDIUM    ██████      F-10: X11 forwarding + legacy RSA key
MEDIUM    ██████      F-11: ~/.ssh/config permissions
MEDIUM    ██████      F-12: Outdated kernel + no auto-updates
MEDIUM    ██████      F-13: Unnecessary services
LOW       ████        F-14: LightDM autologin
LOW       ████        F-15: NMEA/WS on all interfaces
INFO      ██          F-16: SK stack traces in responses
```

---

## Immediate Action Items

1. **Remove `AUTH_DISABLED=true` from `.env` → restart helmlog** (F-01)
2. **`chmod 600 ~/.env ~/.ssh/config`** (F-03, F-11)
3. **Enable Signal K security** (F-05)
4. **Set a Grafana admin password; re-enable login form; set secret_key** (F-06)
5. **`sudo apt upgrade linux-image-rpi-2712 libnss3`** (F-12)
6. **Fix `/notes/` auth bypass in web.py** (F-08)
