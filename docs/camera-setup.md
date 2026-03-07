# Insta360 X4 Camera Setup

How to connect one or more Insta360 X4 cameras to the Raspberry Pi for
automated race recording.

---

## How it works

The X4 runs as a **WiFi access point** — it broadcasts its own hotspot and
clients connect to it.  It cannot join an existing WiFi network (no STA mode).

The Pi connects to each camera's hotspot via a dedicated WiFi interface and
controls recording through the camera's **Open Spherical Camera (OSC)** HTTP
API on port 80.  When a race starts in the web UI, cameras start recording
automatically; when the race ends, they stop.

```
┌────────────┐  WiFi (5 GHz)  ┌─────────────┐
│  Insta360   │◄──────────────│  Pi wlan1    │
│  X4 (main)  │  192.168.42.x │  (USB dongle)│
└────────────┘                └──────┬───────┘
                                     │
                              ┌──────┴───────┐
                              │  Raspberry Pi │
                              │              │
                              └──────┬───────┘
                                     │ Ethernet / wlan0
                              ┌──────┴───────┐
                              │  Boat LAN    │
                              │  (Tailscale) │
                              └──────────────┘
```

---

## Prerequisites

1. **Activate the camera** using the official Insta360 app at least once.
   The OSC API will not respond until activation is complete.
2. **Note the WiFi password.** On the X4 go to Settings → Wi-Fi Settings →
   Password.  The SSID looks like `Insta360 X4 XXXXXX.OSC`.
3. **A 5 GHz-capable USB WiFi adapter** for the Pi (the X4 broadcasts 5 GHz
   by default).  The Pi's built-in WiFi can work if you don't need it for
   the boat LAN.

---

## Single-camera setup

This is the simplest configuration: one camera, one WiFi interface.

### 1. Connect the Pi to the camera's hotspot

If the Pi uses Ethernet for the boat LAN, you can use the built-in WiFi
(`wlan0`) for the camera:

```bash
# On the Pi
sudo nmcli dev wifi connect "Insta360 X4 XXXXXX.OSC" \
    password "YOUR_CAMERA_PASSWORD" \
    ifname wlan0
```

Or with a USB WiFi adapter (`wlan1`):

```bash
sudo nmcli dev wifi connect "Insta360 X4 XXXXXX.OSC" \
    password "YOUR_CAMERA_PASSWORD" \
    ifname wlan1
```

The camera assigns the Pi an IP on `192.168.42.x` and is reachable at
`192.168.42.1`.

### 2. Verify connectivity

```bash
# Quick ping
ping -c 3 192.168.42.1

# Check the OSC API responds
curl -s http://192.168.42.1/osc/info \
    -H "Accept: application/json" \
    -H "X-XSRF-Protected: 1" | python3 -m json.tool
```

You should see JSON with `manufacturer`, `model`, `serialNumber`, etc.

### 3. Configure the logger

Add to `.env` on the Pi:

```bash
CAMERAS=main:192.168.42.1
CAMERA_START_TIMEOUT=10
```

### 4. Test with the CLI

```bash
helmlog list-cameras
```

This pings each configured camera and shows its recording status.

### 5. Test recording

Start a race from the web UI — the camera should begin recording.  Check the
admin cameras page at `/admin/cameras` to see live status.

---

## Multi-camera setup

Each X4 is its own AP on the same `192.168.42.0/24` subnet, so you **cannot**
connect to two cameras from one WiFi interface — the IPs would conflict.

Options:

### Option A: One USB WiFi adapter per camera (recommended)

Each adapter connects to a different camera.  Use network namespaces to
isolate the overlapping `192.168.42.x` subnets.

```bash
# Create a namespace for the second camera
sudo ip netns add cam-starboard
sudo ip link set wlan2 netns cam-starboard
sudo ip netns exec cam-starboard \
    nmcli dev wifi connect "Insta360 X4 YYYYYY.OSC" \
    password "SECOND_CAMERA_PASSWORD" \
    ifname wlan2
```

The logger would then reach the second camera via the namespace.  This
requires custom routing — see the "Advanced: network namespaces" section
below.

### Option B: Sequential connection (simpler, slower)

Connect to one camera at a time, start recording, disconnect, connect to the
next.  This adds latency between camera starts (several seconds per camera)
but avoids the namespace complexity.

> **Note:** The current `cameras.py` implementation sends start commands in
> parallel via `asyncio.gather`, which assumes all cameras are reachable
> simultaneously.  Option B would require code changes to connect/start
> sequentially.

### Option C: USB Ethernet adapters

If the cameras supported Ethernet (they don't natively), you could use
USB-to-Ethernet adapters.  Not applicable to the X4.

---

## Required HTTP headers

The X4's OSC API requires these headers on every request:

| Header | Value |
|---|---|
| `Content-Type` | `application/json;charset=utf-8` |
| `Accept` | `application/json` |
| `X-XSRF-Protected` | `1` |

The `X-XSRF-Protected` header is mandatory — requests without it may be
silently rejected.  The logger adds this automatically.

---

## Useful OSC commands

All commands go to `POST http://192.168.42.1/osc/commands/execute`.

**Check camera info** (GET, no body):
```bash
curl -s http://192.168.42.1/osc/info \
    -H "X-XSRF-Protected: 1"
```

**Check battery and card status:**
```bash
curl -s -X POST http://192.168.42.1/osc/state \
    -H "Content-Type: application/json;charset=utf-8" \
    -H "X-XSRF-Protected: 1"
```

**Set video mode** (required before first recording):
```bash
curl -s -X POST http://192.168.42.1/osc/commands/execute \
    -H "Content-Type: application/json;charset=utf-8" \
    -H "X-XSRF-Protected: 1" \
    -d '{"name":"camera.setOptions","parameters":{"options":{"captureMode":"video"}}}'
```

**Start recording:**
```bash
curl -s -X POST http://192.168.42.1/osc/commands/execute \
    -H "Content-Type: application/json;charset=utf-8" \
    -H "X-XSRF-Protected: 1" \
    -d '{"name":"camera.startCapture"}'
```

**Stop recording:**
```bash
curl -s -X POST http://192.168.42.1/osc/commands/execute \
    -H "Content-Type: application/json;charset=utf-8" \
    -H "X-XSRF-Protected: 1" \
    -d '{"name":"camera.stopCapture"}'
```

**Check recording status:**
```bash
curl -s -X POST http://192.168.42.1/osc/commands/execute \
    -H "Content-Type: application/json;charset=utf-8" \
    -H "X-XSRF-Protected: 1" \
    -d '{"name":"camera.getOptions","parameters":{"optionNames":["captureStatus"]}}'
```

Response includes `"captureStatus": "shooting"` (recording) or `"idle"`.

---

## Important quirks

- **Commands must be sequential.** Never send a second OSC command before the
  first one responds.  The camera cannot handle concurrent requests.
- **5 GHz WiFi only** (by default).  The X4 falls back to 2.4 GHz only if
  5 GHz is unavailable.  Make sure your USB WiFi adapter supports 5 GHz.
- **WiFi range is limited.**  The camera's built-in antenna is not designed
  for long range.  On a J/105 this is fine — mount the camera and Pi within
  a few meters of each other.
- **No STA mode.**  The camera will not join your boat's WiFi network.  The
  Pi must connect to the camera's hotspot.
- **Set video mode first.**  If the camera is in photo or timelapse mode,
  `startCapture` will capture in that mode.  The logger does not currently
  set the capture mode — make sure the camera is set to video mode before
  the first race.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `list-cameras` shows "unreachable" | Check WiFi connection: `iwconfig wlan1`. Reconnect to camera hotspot. |
| `Connection refused` errors | Camera may be off or not activated. Power cycle and retry. |
| Recording starts but no video file | Check SD card: `curl -X POST http://192.168.42.1/osc/state -H "X-XSRF-Protected: 1"` — look for `_cardState: "pass"`. |
| Slow response (>5s) | Camera may be processing. Increase `CAMERA_START_TIMEOUT`. |
| WiFi drops during race | Consider a higher-gain USB WiFi adapter or shorter cable run to the camera. |

---

## References

- [Insta360 OSC API (GitHub)](https://github.com/Insta360Develop/Insta360_OSC)
- [Insta360 Developer Portal](https://onlinemanual.insta360.com/developer/en-us/resource/integration)
- [Open Spherical Camera API spec](https://developers.google.com/streetview/open-spherical-camera)
