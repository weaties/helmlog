# Triton² timer-PGN capture — findings + dockside procedure

On-water validation of the B&G timer feature (#789) on Corvo / Triton². The
`/admin/pgn-audit` sniffer ran during a race night (2026-06-10, 23:27–02:27 UTC)
and captured 138 timer-PGN observations. This doc records what we learned and
the controlled dockside capture needed to finish the SET-duration decode.

## What the race night showed

### PGN 130850 (Start/Stop/Reset/Nearest-Minute) — fully decoded ✅

Byte layout is identical to the Simrad reference (`41 9F FF FF 01 17 <cmd> 00`,
cmd `3D`/`3E`/`3F`/`40`). All 16 frames decoded; source address `0x07`. The
night's control sequence (≈4 race starts):

```
23:27 start/stop (practice) · 23:29 start · 00:39 start ·
01:23 start + nearest_minute(sync) · 02:23 start
```

This path needs **no decoder changes**.

### PGN 130845 (Set duration) — different Triton² layout ⚠️

122 frames, 0 decoded against the Simrad layout. Distinct payloads captured
(SA = source address):

| SA | raw payload | n | reading |
|----|-------------|---|---------|
| 07 | `41 9f ff ff ff ff 07 c0 00 01 00 ff ff ff` | 1 | **SET command** (disc `07 c0 00 01`) |
| 07 | `41 9f ff ff ff ff 07 c0 00 01 01 ff ff ff` | 1 | **SET command** |
| 07 | `41 9f 23 ff ff ff 06 00 00 00 ff ff ff ff` | 48 | state/heartbeat (byte[6]=0x06) |
| 07 | `41 9f 80 ff ff ff 00 00 00 00 ff ff ff ff` | 12 | state (byte[6]=0x00) |
| 07 | `41 9f 80 ff ff ff 31 00 00 00 ff ff ff ff` | 12 | state (byte[6]=0x31=49) |
| 07 | `41 9f 80 ff ff ff 39 00 00 00 ff ff ff ff` | 12 | state (byte[6]=0x39=57) |
| 128 | `41 9f ff ff ff ff 00 00 00 02 23 06 00 00` | 12 | display echo |
| 128 | `41 9f ff ff ff ff 31 00 00 02 00 00 00 00` | 12 | display echo |
| 128 | `41 9f ff ff ff ff 39 00 00 02 8c fa 00 00` | 12 | display echo |

**Findings:**
1. The SET command uses discriminator **`07 c0 00 01`** vs the Simrad
   **`07 42 00 01`** — only byte[7] differs (vendor variant). The decoder now
   keys on byte[6]==0x07 + byte[8:10]==00 01 and ignores byte[7], so both
   decode.
2. Most 130845 traffic is **live state/countdown broadcasts** (byte[6] != 0x07),
   correctly ignored — same as the Simrad running-state broadcast.
3. **Open:** the duration *value* position is unconfirmed. The two captured SET
   frames carried byte[10] = 0x00 / 0x01, which don't match plausible start
   lengths, and there were only 2 across ~4 starts (so they read more like
   occasional manual adjustments than per-start sets). The decoder reads
   byte[10] as minutes for now, **gated** on the capture below.

## Dockside controlled capture (≈5 min, no sailing)

Goal: set a sequence of **known** durations on the Triton² and capture the
130845 frames so we can pin the duration byte.

1. **Confirm the sniffer is live:** open `/admin/pgn-audit` (admin) — the page
   should not show the "OFF" banner. (Or on the Pi: `sudo journalctl -u helmlog
   --since "5 min ago" | grep "PGN audit"` shows `listening read-only on can0`.)
2. **Note the wall-clock start** (so frames can be correlated by time).
3. On the Triton², change the **start-timer value** to each of these in order,
   pausing ~5 s between each so frames land distinctly:
   **5:00 → 3:00 → 2:00 → 6:00 → 1:00 → 4:00**.
   Write down the order + rough times. (Use the same control you'd use to set a
   start — whatever changes the countdown's configured length.)
4. If the value is set via a dedicated "set" action vs. up/down nudges, do both
   ways once each — they may emit different frames.
5. Optionally press Start then Stop once, to re-confirm 130850 still flows.
6. **Report back** — ping me, or send the output of:
   ```bash
   sqlite3 ~/helmlog/data/logger.db \
     "SELECT observed_at, source_addr, raw_hex FROM pgn_audit
       WHERE pgn=130845 AND observed_at > '<your start time ISO>'
       ORDER BY observed_at;"
   ```

I'll diff the captured payloads against the known 5/3/2/6/1/4 sequence, identify
which byte (or 16-bit field) tracks the duration and its unit, then finalize the
decoder and lift the gate on this PR.
