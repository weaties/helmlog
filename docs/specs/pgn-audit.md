# PGN audit — web-accessible instrument timer-PGN validator

Structured spec for the critical-tier change that surfaces B&G timer-PGN
reception in the web UI, so Triton2 hardware support can be confirmed from a
phone on the boat without an SSH/CLI session. Tracking issue #789, Phase 0b
(supersedes the CLI-only `scripts/verify_timer_pgns.py`).

## Goal & non-goals

- **Goal:** prove (or disprove) that our instruments transmit PGN 130845 (Set
  Timer) and 130850 (Start/Stop/Reset/Nearest-Minute) on the bus, viewable
  live at `/admin/pgn-audit`, with the raw bytes captured for any frame that
  doesn't decode (Triton2 layout may differ from the fork's Simrad capture).
- **Non-goal:** controlling the timer or writing to the bus. This is read-only
  observation. The write path (`can_writer.py`) is a later phase.

## Architecture (honours hardware isolation)

```
can0 ──► pgn_audit sniffer task (main.py edge, flag-gated, READ-ONLY)
              │ decode via helmlog.nmea2000
              ▼
        storage.record_pgn_observation()  ──►  pgn_audit table (SQLite)
              ▲
        web route /api/pgn-audit/state reads the table ──► /admin/pgn-audit page (polls)
```

The web layer never opens the bus; it reads SQLite, per the project rule that
web/export code reads from storage and only `main.py` imports hardware modules.

## Config

`PGN_AUDIT_ENABLED` (env / settings, default `false`). When true, `main.py`
starts the sniffer task. Off by default so production boats are unaffected;
the audit is a deliberate opt-in for a validation session. `PGN_AUDIT_CHANNEL`
(default `can0`) selects the socketcan interface.

## Storage — `pgn_audit` table (migration 88)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `observed_at` | TEXT NOT NULL | UTC ISO; CAN frame timestamp |
| `pgn` | INTEGER NOT NULL | 130845 or 130850 |
| `source_addr` | INTEGER NOT NULL | N2K source address |
| `action` | TEXT | decoded: set/start/stop/reset/nearest_minute; NULL if undecoded |
| `minutes` | INTEGER | populated only for `set` |
| `decoded` | INTEGER NOT NULL | 1 decoded, 0 raw-only |
| `raw_hex` | TEXT NOT NULL | reassembled payload hex — lets us adapt a differing layout |

Index on `observed_at`. Append-only, **pruned to the most recent
`_PGN_AUDIT_MAX_ROWS` (2000)** on insert so the table can't grow without bound
on the Pi.

## Verdict decision table

Computed by the pure `verdict_from_summary()` from per-PGN counts.

| 130845 decoded | 130850 decoded | any frames seen | Verdict | Meaning |
|:---:|:---:|:---:|---|---|
| ≥1 | ≥1 | — | **PASS** | Both PGNs seen and decoded — feature viable |
| ≥1 | 0 | — | **PARTIAL** | Saw Set but never Start/Stop (or vice-versa) |
| 0 | ≥1 | — | **PARTIAL** | … |
| 0 | 0 | ≥1 | **PARTIAL** | Frames arrived but none decoded — Triton2 layout differs; use `raw_hex` |
| 0 | 0 | 0 | **FAIL** | No target frames — no N2K-controllable timer, or wrong interface |

## Sniffer lifecycle

1. On start (flag on): open `PGN_AUDIT_CHANNEL` socketcan read-only. If it
   can't open, log a warning and the task exits cleanly (audit unavailable;
   the rest of the logger is unaffected).
2. Per frame: `extract_pgn`; ignore non-target PGNs; `FastPacketBuffer.feed`;
   on a complete payload, `decode()` and `record_pgn_observation()` (decoded or
   raw-only). The per-frame step is the pure `process_frame()` — unit-tested
   without a bus.
3. On cancel (shutdown): close the bus. Never writes to the bus.

## Data-licensing review

`pgn_audit` stores raw NMEA 2000 frames from the boat's **own** instruments —
boat-local telemetry of the same class we already log, no PII, no biometric,
no cross-tenant/co-op sharing, no export path. The page is **admin-only**. No
`docs/data-licensing.md` clause applies; nothing here is shared or embargoed.
Reviewed against the policy: no concerns.

## Security

The audit is read-only and admin-gated (`require_auth("admin")`). It does not
introduce the unauthenticated `/api/internal/timer-event` endpoint — that is a
separate concern for the write-path phase, where the token must be mandatory.
