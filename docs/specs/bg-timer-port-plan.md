# B&G / Simrad Race Timer — curated port plan

Plan for bringing the `MarkPre3802/helmlog` start-timer feature into our
instance as clean, reviewable PRs — **not** a merge of the fork's 55-commit
history. Status: **gated on hardware validation** (see Phase 0). This doc and
`scripts/verify_timer_pgns.py` are the only things on the validation branch;
no shipped module is touched yet.

Source of truth for the feature behaviour: the fork's
`docs/specs/simradtimerintegration.md` (will be ported in Phase 3).

## What the feature does (one paragraph)

Adds bi-directional control of a B&G/Simrad start timer over NMEA 2000. A
read-only bridge sniffs two proprietary Fast Packet PGNs off `can0`
(130845 SET duration, 130850 START/STOP/RESET/NEAREST-MINUTE) and POSTs them
to a new `/api/internal/timer-event` endpoint; HelmLog can also write those
PGNs back to the instruments via a new `can_writer.py`. A rewritten
`/race-start` page and a `simrad_timer_state` table give the helm a timer that
stays in sync with the display, including fully headless operation (run a race
from the instruments with no browser open).

## Phase 0 — Hardware validation (this branch, deploy tonight)

> **Now delivered as a web feature** (`/admin/pgn-audit`), not just the CLI
> script — see `docs/specs/pgn-audit.md`. The decoder is ported into
> `nmea2000.py`; an opt-in read-only sniffer (`PGN_AUDIT_ENABLED=true`, wired in
> `main.py`) co-reads `can0` and writes observations to the `pgn_audit` table
> (migration 88); the admin page polls a verdict. The CLI script remains as an
> SSH fallback. Validate from a phone on the boat — no command line needed.

**Why first:** the fork's byte layout was reverse-engineered on a *different*
boat (a Simrad system on "CreativePi"). Our boat is Corvo on B&G **Triton2**.
Same vendor (Navico, mfr code `0x9F41`), so the framing is *likely* identical —
but unverified. If Triton2 does not emit a controllable timer over N2K, the
whole port is dead code for us.

- `scripts/verify_timer_pgns.py` — read-only listener. Never transmits, safe on
  the live bus alongside Signal K. Decodes 130845/130850 and, crucially, logs
  the **raw reassembled payload of every target-PGN frame even when it does not
  decode**, so a differing Triton2 layout can be adapted from the capture
  without a second trip to the water.
- `tests/test_verify_timer_pgns.py` — locks the expected byte layout so an
  on-water surprise shows up as a decoder mismatch, not a silent parse.

Run on corvopi-live, operate the Triton2 timer, send back the verdict block:

```bash
uv run python scripts/verify_timer_pgns.py --self-test                 # prove the tool first
uv run python scripts/verify_timer_pgns.py --json-out /tmp/pgns.json   # then on the water
```

Outcomes:
- **PASS** (both PGNs decode) → proceed to Phase 1.
- **PARTIAL** (frames seen, don't decode) → send the JSON; we adapt the decoder
  in Phase 1 from the captured raw bytes, then proceed.
- **FAIL** (no frames) → stop. Either Triton2 has no N2K-controllable timer, or
  `can0` is not the interface Signal K uses (it may read a serial gateway). Log
  the result and shelve the port.

> **Assumption to confirm during this run:** corvopi-live exposes `can0` as a
> socketcan interface we can co-read. Our stack is Signal-K-first; if SK ingests
> N2K via a serial gateway (Actisense/YDEN) rather than socketcan, there is no
> `can0` to sniff and we capture at the gateway instead. The script's preflight
> prints exactly this guidance if the interface won't open.

## Phase 1 — Decoder + read path (port, behind validation)

- Port `FastPacketBuffer`, `SimradTimerRecord`, the 130845/130850 decoders, and
  the PGN constants into `src/helmlog/nmea2000.py` (adapting the layout if the
  Phase 0 capture differed). Bring the fork's `tests/test_nmea2000.py`
  additions. **`nmea2000.py` is domain-critical** — `/domain` + full unit
  coverage, no hand-rolled byte parsing beyond what the capture proves.
- Port `scripts/simrad_timer_sk.py` (the bridge) and replace the inline decoder
  in `verify_timer_pgns.py` with the real `helmlog.nmea2000` import, or retire
  the script once the bridge supersedes it.

## Phase 2 — Storage (critical tier)

- Port the `simrad_timer_state` table **but renumber the migrations**: the fork
  uses 86 & 87; our `main` ships migration **86** (per-race crew, #761) and
  **87** (GPS-clock provenance, #794), and this branch takes **88** (`pgn_audit`).
  Land the timer-state table as **89 (create) + 90 (add `rolling_timer_on`)** and
  bump `_CURRENT_VERSION = 90`.
  *(A single squashed 89 is fine too — the split only mirrors the fork's history.)*
- Port `get_simrad_timer_state` / `upsert_simrad_timer_state`.
- `storage.py` migrations are **critical tier** → TDD + `/spec` + migration test
  proving a v86 DB upgrades cleanly.

## Phase 3 — Routes, write path, UI

- Port `simrad_timer.py` (pure state functions — already well tested in the fork).
- Port `can_writer.py` (CANWriter), wired in `main.py` and injected via
  `app.state.can_writer`; keep the hardware-isolation boundary (imported only by
  `main.py`). **This is the first code that writes to the instrument bus** — call
  it out explicitly in review.
- Port the `/api/internal/timer-event` handler, the `/race-start` rewrite
  (`race_start.py`, `race_start.js`, `race_start.html`), and the
  `simradtimerintegration.md` spec.
- **Security hardening (see Open decisions):** make `HELMLOG_TIMER_TOKEN`
  mandatory — refuse to serve the endpoint without it — rather than the fork's
  optional default.
- Port the fork's `tests/test_simrad_timer*.py`; reconcile the deleted
  `test_race_start_sim.py` against our current race-start FSM tests.
- `/data-license` review: the endpoint **creates and ends race rows**, which
  touches the protest-firewall / session-integrity surface.

## Explicitly left behind (not ported)

- **Local Leaflet vendoring** (`static/leaflet.css`, `static/leaflet.js`) and the
  **`.gitattributes`** addition — unrelated to the timer. If we want to drop the
  Leaflet CDN dependency, that is its own PR with its own rationale.
- The fork's `scripts/can_monitor.py` / `scripts/send_timer.py` dev aids — useful
  but optional; revisit in Phase 3 if handy. `send_timer.py` **transmits** to the
  bus, so it is not part of the read-only validation branch.
- The fork's ideation entry IDX-039 (GoPro GPS-time auto-sync) — unrelated.

## Open decisions (intuited here, not discussed in the source repo)

1. **Auth must fail closed.** The fork exempts `/api/internal/timer-event` from
   auth and leaves `HELMLOG_TIMER_TOKEN` optional (blank = no auth). Unauthenticated,
   it can create/end races, and there's no replay/idempotency guard. We require
   the token before exposing the endpoint.
2. **Migration renumber 86 → 87/88** (above). Hard blocker against a raw merge.
3. **Address-claim is fire-and-forget.** `CANWriter` claims SA `0x7E` but never
   defends it (no response to PGN 60928 contention) and doesn't verify it's free.
   Confirm `0x7E` is unused on our network before enabling the write path.
4. **`updated_at` stores the NMEA frame timestamp, not wall clock** — defensible
   but a deviation from our usual wall-clock `updated_at`; decide on port.
5. **Duplicate-race race condition:** two near-simultaneous `running` events can
   both see no open race and create two. Single-bridge makes it unlikely; add a
   guard if we ever run redundant bridges.

## Source-repo references

- Fork PRs: #1 (publish SET duration), #2 (pass all args to `start_race`),
  #5 (control B&G timer via CAN from web UI), #6 (regression-test fixes).
- Fork spec: `docs/specs/simradtimerintegration.md`.
- Fork merge base with our `main`: `c70c1e3`; fork HEAD reviewed: `6cb45276`.
