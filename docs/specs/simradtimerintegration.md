# B&G / Simrad Race Timer Integration

## Overview

HelmLog integrates with B&G/Simrad NMEA 2000 instruments for bi-directional race timer control. The `simrad_timer_state` table is the single source of truth for all timer state — whether instruments are connected or not. The Start page (`/race-start`) operates in two modes depending on the state of the **Instrument Timer (B&G)** toggle.

---

## Technical integration

The bridge (`scripts/simrad_timer_sk.py`) reads Simrad/B&G NMEA 2000 CAN frames directly (PGN 130845 SET_TIMER, PGN 130850 START/STOP/RESET/NEAREST_MINUTE), decodes them, and POSTs events to `POST /api/internal/timer-event` on HelmLog, bypassing Signal K entirely. Signal K was initially considered as the transport but its queuing latency on a busy NMEA bus was 8+ seconds; direct HTTP is sub-second.

The endpoint is exempt from HelmLog's global auth middleware (`_PUBLIC_PATHS` in `web.py`) and supports optional bearer-token auth via `HELMLOG_TIMER_TOKEN` env var (leave blank on a private boat LAN).

The bridge publishes two path values in its POST body:
- `racing.startTimer.state` — values: `"running"` | `"stopped"` | `"reset"` | `"nearest-minute"`
- `racing.startTimer.duration` — integer seconds, written only on SET

HelmLog can also write to B&G instruments via `can_writer.py` (`CANWriter`), which claims NMEA 2000 source address `0x7E` on startup and sends PGN 130845 (SET duration) and PGN 130850 (START/STOP/RESET/NEAREST-MINUTE) using Fast Packet encoding. The `CANWriter` instance is created in `main.py` and injected into `app.state.can_writer`; route handlers access it from there.

---

## Timer state model

All timer state is stored in the `simrad_timer_state` table (singleton row, id=1):

| Column | Type | Description |
|---|---|---|
| `instrument_timer_on` | bool | B&G sync enabled |
| `duration_s` | int \| null | Current timer duration in seconds |
| `t0_utc` | datetime \| null | Moment the countdown reaches zero |
| `stopped_remaining_s` | float \| null | Frozen remaining seconds when stopped |
| `is_running` | bool | Timer is counting down |
| `rolling_timer_on` | bool | Auto-restart at 0:00 |

**Clock display logic** (client-side):
- Running + `t0_utc` set → live countdown: `remaining = t0_utc − now`
- Stopped + `stopped_remaining_s` set → show frozen value
- Otherwise → show `duration_s` or `--:--`

---

## Start page UI (`/race-start`)

### Button layout

| Button | Standalone behaviour | B&G (toggle ON) also sends |
|---|---|---|
| **Start** | Close any open race; create new race; start timer at `duration_s` | `start` (PGN 130850) |
| **Stop Race** | Stop timer; store `stopped_remaining_s`; end current race | `stop` (PGN 130850) |
| **Reset** | Reset `t0_utc` to `now + duration_s` (if running) or clear `stopped_remaining_s` (if stopped); does not change running state | `reset` (PGN 130850) |
| **Sync** | Snap running timer to nearest whole minute (rounds `remaining` to nearest 60 s and recomputes `t0_utc`) | `nearest-minute` (PGN 130850) |
| **Boat-end ping** | Record GPS position as boat-end of start line | — |
| **Pin-end ping** | Record GPS position as pin-end of start line | — |

### Set Start Value

- Full-width button below the main grid; **disabled while the timer is running**.
- Tap to enter edit mode: the clock display is replaced by a numeric input field showing the current duration in whole minutes.
- Input accepts whole minutes only (1–60). Fractions of a minute are not supported.
- Pressing **Confirm** (or Enter) calls `POST /api/race-start/set-duration` with `duration_s = minutes × 60`. This clears `t0_utc` and `stopped_remaining_s` — the new duration takes effect on the next Start.
- Pressing **Escape** or tapping outside cancels without changing state.
- B&G (toggle ON): also sends `SET duration` (PGN 130845) so B&G instruments are loaded with the same value.

### Rolling Timer switch

- Toggle switch below Set Start Value.
- When ON: the client detects `remaining ≤ 0` while `is_running = true` and automatically calls `POST /api/race-start/timer-reset`, restarting the countdown from the stored `duration_s`.
- Rolling restart is client-side only — no B&G CAN command is currently sent (pending CAN sequence capture).
- Toggle state persists in `rolling_timer_on`.

### Instrument Timer (B&G) panel

- Toggle switch at the bottom of the page.
- Controls whether inbound B&G events update HelmLog state and whether UI actions send outbound CAN commands.
- Status text shows the current B&G timer state (Running / Stopped / No data).
- No direct B&G control buttons — all B&G commands are sent automatically by the main buttons when the toggle is ON.

### Line metrics panel

Two-column layout:
- **Left**: Line bearing, Line length, Bias (label + value)
- **Right**: Distance to line, Time to line (large-format primary display)

Metrics are computed server-side from the latest GPS position, COG, SOG, and TWD after both line ends have been pinged.

---

## Instrument Timer toggle

**OFF (default on startup):** Standalone mode. Inbound B&G timer events are ignored. No outbound CAN commands are sent. All Start page buttons operate on `simrad_timer_state` only.

**ON:** Full bi-directional sync.
- Inbound B&G events drive HelmLog timer state (see B&G → HelmLog below).
- Start page button actions also send CAN commands to B&G instruments.
- All buttons remain enabled — the user can act from either side.

**Auto-ON:** The toggle switches ON automatically when the first `POST /api/internal/timer-event` is received from the bridge and no row exists yet (fresh install / first headless boot). If the user has explicitly set the toggle OFF, inbound events are ignored and the toggle is not re-enabled automatically.

The toggle state persists across reboots. `duration_s` and `stopped_remaining_s` also persist.

---

## Headless mode

HelmLog supports fully headless operation driven by B&G instruments — the race-start web page does not need to be open.

The bridge (`simrad_timer_sk.py`) and `POST /api/internal/timer-event` are server-side components. All inbound event processing — timer state updates, race creation, and race end — happens in the database regardless of whether any browser has the Start page open.

A helm can run an entire race using only B&G instruments with no interaction with HelmLog:

- B&G SET → `duration_s` stored in DB
- B&G START → race created (if none open), `t0_utc` computed and stored
- B&G STOP → race ended, `stopped_remaining_s` stored
- B&G RESET / NEAREST-MINUTE → timer state updated in DB

**Toggle in headless:** The toggle defaults OFF on process start. The first inbound B&G event auto-flips it ON server-side — no UI action required. Subsequent events are processed normally.

**UI reconnect:** When the Start page is opened during or after headless operation, it reads persisted state from the DB and displays the current timer and race.

**Outbound commands are UI-only:** HelmLog → B&G commands require a user action on the Start page. There is no headless outbound path — in pure headless mode B&G instruments are the sole source of commands.

---

## B&G → HelmLog (inbound events)

All calculations use the NMEA UTC time taken from the CAN hardware frame timestamp (not system clock). The bridge uses `msg.timestamp` from python-can's `can.Message`, set at hardware receive time.

**WHEN** a `racing.startTimer.duration` value is received, HelmLog SHALL set `duration_s` and persist it until a new SET command is issued. The duration is shown on the Start page as the timer value.

**WHEN** `racing.startTimer.state = "running"` is received:
- Compute `t0_utc = nmea_timestamp + X` where:
  - X = `stored_duration_s` if no prior STOP (fresh start)
  - X = `stopped_remaining_s` if resuming after a STOP
- Set `is_running = true`, clear `stopped_remaining_s`.
- If no race is open, create one named `<DATE TIME>` using the NMEA timestamp.

**WHEN** `racing.startTimer.state = "stopped"` is received, HelmLog SHALL stop the timer, persist `stopped_remaining_s = t0_utc − nmea_timestamp` (clamped to 0), set `is_running = false`, and end the current open race (set `end_utc` to the NMEA timestamp). If no race is in progress the finish is a no-op.

**WHEN** `racing.startTimer.state = "reset"` is received, HelmLog SHALL reset the timer to `duration_s` without changing running state — if running it continues to run from the new `t0_utc`; if stopped it remains stopped with `stopped_remaining_s` cleared.

**WHEN** `racing.startTimer.state = "nearest-minute"` is received, HelmLog SHALL move the timer to the nearest whole minute using the NMEA timestamp, without changing running state.

---

## HelmLog → B&G (outbound commands)

When the Instrument Timer toggle is **ON** and a CAN writer is available, the following Start page actions send CAN commands:

| Start page action | B&G command | PGN |
|---|---|---|
| Start | `start` | 130850 |
| Stop Race | `stop` | 130850 |
| Reset | `reset` | 130850 |
| Sync | `nearest-minute` | 130850 |
| Set Start Value (confirm) | `set` with `minutes = duration_s / 60` | 130845 |

When the toggle is OFF, no outbound commands are sent.

If the CAN writer is unavailable (no CAN hardware, failed to open), the outbound step is silently skipped — HelmLog continues to function as standalone.

---

## Feedback loop

When HelmLog sends a command to B&G (e.g. `stop` from Stop Race), the bridge will receive that CAN frame and echo it back as an inbound timer-event POST. All inbound handlers are idempotent so the echo is a harmless no-op.

---

## Open items / pending spec

- **Rolling Timer CAN sequence**: The NMEA 2000 sequence for looping the B&G timer at 0:00 has not been captured yet. Rolling Timer currently operates client-side only. Once the sequence is known, add the outbound CAN command to `POST /api/race-start/timer-reset`.
- **Scheduled start integration**: `routes/races.py` `_do_scheduled_start()` currently writes to the legacy FSM `race_start_state` table, which the Start page no longer reads. Needs updating to start `simrad_timer_state` instead.

---

## Implementation notes

**CAN bus reading**: `can.Notifier` + `can.AsyncBufferedReader` silently drop all messages in Python 3.10+ unless the running loop is passed explicitly. The bridge uses `loop.run_in_executor(None, bus.recv, 1.0)` — blocking recv in the thread pool — which avoids this fragility entirely.

**Fast Packet sequence counter**: `_fast_packet_frames()` in `can_writer.py` takes a `seq` parameter (3-bit, 0–7). `CANWriter` maintains a per-CAN-ID counter (`_seq` dict, incremented mod 8 on each send). B&G receivers de-duplicate transfers by sequence counter — a constant seq=0 causes every second SET to be silently dropped.

**PGN 130845 SET payload**: Bytes [11–13] after the minutes byte must be `0x00`, not `0xFF`. B&G ignores SET frames where those bytes are the NMEA 2000 "not available" sentinel.

**HTTP transport**: `HelmLogPublisher` uses `httpx.AsyncClient` with a persistent connection. Back-to-back events (e.g. SET immediately followed by START) share the same TCP connection rather than each paying connection-setup overhead.

**Service deployment**: `/etc/systemd/system/simrad-timer.service` is NOT updated by `git pull`. After any change to `scripts/simrad-timer.service`, copy it manually:
```
sudo cp ~/helmlog/scripts/simrad-timer.service /etc/systemd/system/simrad-timer.service
sudo systemctl daemon-reload && sudo systemctl restart simrad-timer
```
