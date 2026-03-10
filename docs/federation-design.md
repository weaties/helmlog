# Helm Log — Federated Co-op Protocol Design

> Design document for decentralized data sharing between boats in a co-op,
> built on the existing Raspberry Pi + Tailscale + FastAPI architecture.

## Table of Contents

- [Core Principle](#core-principle)
- [1. Identity Model](#1-identity-model)
- [2. Membership Protocol](#2-membership-protocol)
- [3. Co-op API Endpoints](#3-co-op-api-endpoints)
- [4. Request Authentication](#4-request-authentication)
- [5. Data Flow Patterns](#5-data-flow-patterns)
- [6. Peer Caching (Optional)](#6-peer-caching-optional)
- [6.5. Processing Offload](#65-processing-offload)
- [6.6. Video and the Camera Pipeline](#66-video-and-the-camera-pipeline)
- [7. Per-Event Co-op Assignment](#7-per-event-co-op-assignment)
- [8. Current Model Computation](#8-current-model-computation)
- [8.5. Fleet Benchmark Computation](#85-fleet-benchmark-computation)
- [9. SQLite Schema Additions](#9-sqlite-schema-additions)
- [10. New Python Modules](#10-new-python-modules)
- [11. Peer Discovery](#11-peer-discovery)
- [12. What This Does NOT Require](#12-what-this-does-not-require)
- [13. Migration Path](#13-migration-path)
- [14. Protocol Versioning & Upgrades](#14-protocol-versioning--upgrades)
- [15. Security Assumptions & Threat Model](#15-security-assumptions--threat-model)
- [16. Failure Modes & Recovery](#16-failure-modes--recovery)
- [17. Event Naming & Canonicalization](#17-event-naming--canonicalization)
- [18. Charter vs Agreements](#18-charter-vs-agreements)
- [19. Co-op Dissolution](#19-co-op-dissolution)
- [20. Inter-Co-op Boundaries](#20-inter-co-op-boundaries)
- [21. Performance Envelope](#21-performance-envelope)
- [22. Enforcement Classification](#22-enforcement-classification)
- [23. Resolved Design Decisions](#23-resolved-design-decisions)
- [24. Open Questions](#24-open-questions)

---

## Core Principle

**Each Pi is the single source of truth for its own data.** There is no
central server. Co-op data sharing is peer-to-peer over the Tailscale mesh.
A boat's data exists on that boat's Pi, and other co-op members query it
directly.

---

## 1. Identity Model

### Boat identity

Each Pi generates an **Ed25519 keypair** at setup time. The public key is the
boat's cryptographic identity. The private key never leaves the Pi.

```
~/.helmlog/identity/
├── boat.key          # Ed25519 private key (mode 0600)
├── boat.pub          # Ed25519 public key
└── boat.json         # { "pub": "<base64>", "sail_number": "69",
                      #   "name": "Javelina", "owner_email": "skipper@example.com" }
```

The `boat.json` is the **boat card** — a self-signed document that associates
the public key with human-readable metadata. It's freely shareable. The
`owner_email` field is **required for co-op membership** (enables out-of-band
communication for votes, admin transfers, and emergencies) but **optional for
standalone use**. Email addresses are **PII** — visible only to co-op admins,
never exposed to other members, and scrubbed on departure (see data licensing
policy Section 1).

### Ed25519 performance characteristics

- Signature size: 64 bytes
- Pi 4 verification speed: ~20,000 signatures/second
- Even querying 50 peers simultaneously for a race replay, signature
  verification overhead is negligible (<5 ms)

### Co-op identity

A co-op is identified by a **co-op public key**. Rather than a single admin
holding a private key (single point of failure), the co-op uses **multi-admin
signing**: membership records are valid when signed by M of N designated admin
boats. This allows the co-op to survive the loss of any single Pi without
complex key-sharding ceremonies.

The founding admin designates 2-3 admin boats at creation. A membership record
or revocation requires signatures from a majority of admins (e.g., 2-of-3).

**Single moderator mode** (available for co-ops of any size, per the data
licensing policy): instead of M-of-N, a single moderator boat signs all
admin records. A **designated backup boat**
can assume moderator role if the moderator's Pi is lost. The charter specifies
which mode the co-op uses.

```
~/.helmlog/co-ops/<co-op-id>/
├── charter.json      # Signed charter metadata (includes admin boat list)
└── members/          # Signed membership records (multi-sig)
    ├── <boat-pubkey-fingerprint>.json
    └── ...
```

### Key derivation and fingerprints

- Keys: Ed25519 via Python `cryptography` library
- Fingerprints: SHA-256 of the public key bytes, base64url-encoded, truncated
  to 16 chars (collision-safe for fleets of <1000 boats)
- Co-op ID: fingerprint of the co-op public key

---

## 2. Membership Protocol

### Creating a co-op

The founding admin's Pi:

1. Designates the initial admin boats (2-3 boats, including themselves)
2. Creates a **charter record** signed by all founding admins:
   ```json
   {
     "type": "charter",
     "co_op_id": "<fingerprint>",
     "name": "Puget Sound J/105",
     "area": ["Elliott Bay", "Central Puget Sound"],
     "created_at": "2026-04-01T00:00:00Z",
     "admin_boats": [
       { "boat_pub": "<base64>", "name": "Javelina" },
       { "boat_pub": "<base64>", "name": "Blackhawk" },
       { "boat_pub": "<base64>", "name": "Surfer Girl" }
     ],
     "admin_threshold": 2,
     "heartbeat_inactive_days": 60,
     "sharing_delay": "immediate",
     "session_visibility": "event_scoped",
     "data_aging": { "current_season": "full", "previous_season": "reduced", "older": "summary" },
     "benchmark_min_boats": 4,
     "benchmark_cache_ttl": 86400,
     "membership_eligibility": { "active_racing_required": true },
     "charter_url": "https://...",
     "admin_sigs": [
       { "admin_boat_pub": "<base64>", "sig": "<base64>" },
       { "admin_boat_pub": "<base64>", "sig": "<base64>" },
       { "admin_boat_pub": "<base64>", "sig": "<base64>" }
     ]
   }
   ```
3. Admins sign membership records for each other (multi-sig bootstrap)

### Joining a co-op

```
Joining boat                          Admin's Pi
     |                                     |
     |  --- POST /co-op/join-request --->  |
     |       { boat_card, message }        |
     |                                     |
     |       Admin reviews request         |
     |       - Verifies eligibility        |
     |         (active racer, not a        |
     |          commercial actor)          |
     |       - Returns pre-join disclosure  |
     |         (active agreements, ML,     |
     |          current models, embargo)   |
     |       (web UI or CLI)               |
     |                                     |
     |  <-- signed membership record ---   |
     |                                     |
     |  Stores membership record locally   |
     |  Adds co-op pub to trusted list     |
     |                                     |
```

**Membership eligibility** (per data licensing policy Section 3):

- The co-op charter may require **active racing** as a condition of membership
- **Commercial actors** (coaching services, analytics companies, sail lofts)
  must use the coach access mechanism instead of full membership
- Joining primarily for **data observation** (without contributing) is grounds
  for expulsion
- The admin presents a **pre-join disclosure** of all active agreements before
  admission

### Membership record

A membership record is signed by a **majority of admin boats** (M-of-N
multi-sig) and proves that a boat is an authorized member:

```json
{
  "type": "membership",
  "co_op_id": "<fingerprint>",
  "boat_pub": "<base64>",
  "sail_number": "69",
  "boat_name": "Javelina",
  "owner_email": "skipper@example.com",
  "role": "member",
  "joined_at": "2026-04-15T00:00:00Z",
  "expires_at": null,
  "admin_sigs": [
    { "admin_boat_pub": "<base64>", "sig": "<base64>" },
    { "admin_boat_pub": "<base64>", "sig": "<base64>" }
  ]
}
```

Any Pi can verify this record by checking that the required number of admin
signatures are present and valid — no network call required. The admin boat
list is in the charter record.

### Revoking membership (departure or expulsion)

The admins sign a **revocation record** (M-of-N, same threshold as
membership):

```json
{
  "type": "revocation",
  "co_op_id": "<fingerprint>",
  "boat_pub": "<base64>",
  "reason": "voluntary_departure",
  "effective_at": "2026-09-01T00:00:00Z",
  "grace_until": "2026-10-01T00:00:00Z",
  "admin_sigs": [
    { "admin_boat_pub": "<base64>", "sig": "<base64>" },
    { "admin_boat_pub": "<base64>", "sig": "<base64>" }
  ]
}
```

During the 30-day grace period, the departing boat's data is still accessible
but marked as pending departure. After `grace_until`, other Pis stop querying
that boat and purge any cached data.

#### Revocation broadcast

Revocation records are **aggressively pushed**, not passively polled. When
admins sign a revocation:

1. The admin Pi immediately pushes the revocation record to **all online
   co-op peers** via `POST /co-op/{co_op_id}/revocations`
2. Each receiving Pi verifies the admin signatures and immediately:
   - Drops the revoked boat from its `co_op_peers` table
   - Rejects any future API requests from the revoked boat's fingerprint
   - Purges any cached data from the revoked boat (after `grace_until`)
3. Offline Pis receive the revocation on their next tombstone poll cycle
   (Section 3 tombstone polling already covers this as a fallback)

This is critical for **expulsion** scenarios — the co-op cannot rely on
the expelled boat voluntarily deleting caches or stopping queries. The
push ensures all peers enforce the revocation within minutes of signing,
not hours or days.

### Admin rotation

Since admin authority is distributed across M-of-N admin boats, rotating
an admin is straightforward — no private key transfer needed. The existing
admins (meeting threshold) sign a **charter amendment** that updates the
admin boat list:

```json
{
  "type": "charter_amendment",
  "co_op_id": "<fingerprint>",
  "remove_admin": "<boat_pub to remove>",
  "add_admin": "<boat_pub to add>",
  "effective_at": "2027-04-01T00:00:00Z",
  "admin_sigs": [
    { "admin_boat_pub": "<base64>", "sig": "<base64>" },
    { "admin_boat_pub": "<base64>", "sig": "<base64>" }
  ]
}
```

The amendment is distributed to all members. No key material changes hands.
If a single admin's Pi is lost, the remaining admins still meet threshold
and can sign a replacement into the admin set.

---

## 3. Co-op API Endpoints

Each Pi exposes these endpoints to other co-op members over Tailscale. All
requests include a signed authentication header (see Section 4).

### Discovery & membership

```
GET  /co-op/identity
     Returns this boat's boat card (public key + metadata).

GET  /co-op/memberships
     Returns all membership records this boat holds (which co-ops it
     belongs to). Used by other Pis to verify mutual co-op membership.

POST /co-op/join-request
     Submit a join request to this boat's admin. Body: boat card + message.
     Returns 202 Accepted (admin reviews async).

GET  /co-op/{co_op_id}/members
     Returns all membership records for a co-op (if this boat is the admin).
     Other members can request the member list to discover peers.
```

### Session data (shared with co-op)

```
GET  /co-op/{co_op_id}/sessions
     List sessions this boat has shared with the co-op.
     Query params: ?after=<iso>&before=<iso>&type=race|practice
     Returns: session summaries (id, type, start, end, event_name).
     Does NOT return private data (audio, notes, crew, sails, video links).
     See [Data Licensing Policy](data-licensing.md) Definitions section for the full
     PII definition and Section 2 ("Data Sharing — The Co-op Model") for the shared/private data boundary.

     Respects temporal sharing controls:
     - Embargoed sessions return { "status": "embargoed", "available_at": "<iso>" }
       in the summary instead of full metadata
     - Embargo policy is co-op-level (set in charter or by majority vote)

GET  /co-op/{co_op_id}/sessions/{session_id}/track
     GPS track for a shared session. Returns position + instrument data
     at 1 Hz: lat, lon, bsp, tws, twa, hdg, cog, sog, aws, awa.

     Respects session visibility and data aging:
     - If the co-op uses event-scoped visibility, the requester must
       include a **Proof of Participation** header:
         X-HelmLog-PoP: <signed-session-summary>
       The PoP is a signed claim that the requesting boat has a shared
       session in the same event (event_name match). The provider Pi
       verifies the signature and event match before releasing full
       track data. This solves the discovery loop: neither Pi needs to
       see the other's private session list — the requester proves
       participation with a self-signed, verifiable claim.
     - Without a valid PoP (or if the co-op doesn't use event scoping),
       returns 403 with { "error": "event_scope", "message": "Track
       data available only for events you participated in" }
     - If data aging is enabled, older sessions return reduced-resolution
       data (e.g., 10-second intervals for previous season, summary-only
       for 2+ seasons ago)
     - Returns 403 for embargoed sessions

GET  /co-op/{co_op_id}/sessions/{session_id}/results
     Race results for a shared session (if results exist).
     Returns: [{boat, place, finish_time}].
     Results are always visible (even for event-scoped co-ops) since race
     results are public data.

GET  /co-op/{co_op_id}/sessions/{session_id}/polar
     Polar performance data for the session (BSP vs target at each TWS/TWA).
     Subject to the same event-scoping and data aging rules as /track.
```

**Not served via co-op API:** audio recordings, transcripts, video
recordings, YouTube links, photos, session notes, crew roster, sail
selections. These remain boat-private. Video is the most tactically
revealing data type (360° footage shows sail trim, crew positions,
tacking technique) — see "Video and the camera pipeline" below.

### Fleet benchmarking

```
GET  /co-op/{co_op_id}/benchmarks/maneuvers
     This boat's per-maneuver metrics for benchmark aggregation.
     Returns: [{ "type": "tack"|"gybe"|"mark_rounding"|"start"|"acceleration",
                 "session_id": 42, "tws_bin": "10-12",
                 "duration_sec": 5.2, "loss_metric": 0.3 }]
     Only served to co-op members. Each boat contributes its own maneuver
     metrics; no boat sees another boat's individual data points.

     Respects embargo: embargoed session maneuvers are excluded until
     embargo lifts.

GET  /co-op/{co_op_id}/benchmarks/polar
     This boat's polar performance data points for benchmark aggregation.
     Returns: [{ "session_id": 42, "tws_bin": "10-12", "twa_bin": "30-40",
                 "bsp_avg": 5.88, "vmg_avg": 5.72, "n_samples": 120 }]
```

**Benchmark computation** happens on the requesting Pi (or a designated
aggregator):

1. Query all online peers for `/benchmarks/maneuvers` and `/benchmarks/polar`
2. Aggregate into anonymous fleet statistics (median, percentiles)
3. Enforce minimum 4-boat threshold per condition bin
4. Render the **Percentile Heatmap** dashboard showing:
   - Each maneuver type with fleet 10th%, median, 90th%, your result,
     your percentile rank
   - Color coding: green (top 25%), yellow (middle 50%), red (bottom 25%)
5. No per-boat data points are stored or displayed — only aggregates

The benchmark endpoints return raw metrics, not identities. The requesting
Pi never learns which data points came from which boat — it sees only
anonymous arrays from each peer, and the aggregation discards per-source
attribution.

### Current / tide observations

```
GET  /co-op/{co_op_id}/currents
     This boat's derived current observations (BSP+HDG vs SOG+COG vectors).
     Only served if the co-op has an active current-sharing agreement with
     unanimous consent. Returns 403 if no agreement or boat has opted out.
     Query params: ?area=<area-name>&after=<iso>&before=<iso>
```

### Consent & governance

```
GET  /co-op/{co_op_id}/agreements
     Active agreements for this co-op (commercial, ML, current models,
     cross-co-op). Used for pre-join disclosure.

POST /co-op/{co_op_id}/votes/{proposal_id}
     Submit a signed vote on a proposal. Body:
     { "vote": "approve" | "reject", "boat_sig": "<base64>" }

GET  /co-op/{co_op_id}/votes/{proposal_id}
     Get current vote tally. Any member can verify all signatures.
```

### Deletion & anonymization

```
POST /co-op/{co_op_id}/tombstones
     Publish a signed tombstone for data this boat is deleting or
     anonymizing. Other Pis that have cached this data must honor it.
     Body: { "session_ids": [...], "action": "delete" | "anonymize",
             "effective_at": "<iso>", "boat_sig": "<base64>" }

GET  /co-op/{co_op_id}/tombstones?after=<iso>
     Fetch recent tombstones. Pis poll this on each other periodically
     to stay in sync on deletions.

POST /co-op/{co_op_id}/revocations
     Admin pushes a signed revocation record to all peers. Receiving Pi
     verifies admin signatures, drops the revoked boat from co_op_peers,
     rejects future requests from that fingerprint. This is push-based
     (not poll-based) to ensure rapid enforcement on expulsion.
```

### Heartbeat & presence

```
GET  /co-op/{co_op_id}/heartbeat
     Lightweight presence check. Returns:
     {
       "timestamp": "2026-03-07T19:00:00Z",
       "status": "online",
       "last_gps_fix": "2026-03-07T18:55:00Z",
       "sig": "<base64>"
     }

     Pis poll this to determine who is "on the water" vs "in the slip"
     without pulling heavy session lists. Also used to determine
     active vs inactive membership status for quorum calculations
     (see Section 3.1).
```

### Audit

```
GET  /co-op/{co_op_id}/audit-log
     This boat's audit log of co-op data access events. Admin-only.
     Returns: who accessed what session, when, from which boat.
```

### 3.1. Active vs Inactive Members and Quorum

In a seasonal sport, "unanimous" is a recipe for deadlock when a boat is
hauled out for the winter. The heartbeat endpoint solves this:

- **Active**: last heartbeat within `heartbeat_inactive_days` (charter
  default: 60 days). Counted in quorum denominator.
- **Inactive**: no heartbeat in 60+ days. **Excluded from quorum
  denominator** for standard votes (2/3 supermajority) but retains full
  data access and co-op membership.
- **Unanimous votes** (e.g., current model sharing): still require all
  **active** members. Inactive members are excluded from the denominator
  but can opt back in by sending a heartbeat.

Example: 7-boat co-op, 2 boats inactive for winter.
- 2/3 supermajority vote: need 4 of 5 active boats (not 5 of 7)
- Unanimous vote: need 5 of 5 active boats

A boat that comes back online and sends a heartbeat immediately becomes
active again and is included in future votes.

---

## 4. Request Authentication

Every co-op API request includes a signed header proving the caller's
identity:

```
X-HelmLog-Boat: <boat-pub-fingerprint>
X-HelmLog-Timestamp: <iso-8601-utc>
X-HelmLog-Nonce: <16-byte-random-hex>
X-HelmLog-Sig: <base64>
```

The signature covers: `METHOD /path timestamp nonce`. The receiving Pi:

1. Looks up the boat's public key by fingerprint
2. Verifies the signature
3. Checks that the timestamp is within the allowed window (replay protection)
4. **Checks the nonce is unique** — the receiving Pi maintains a
   `seen_nonces` set (bounded by the timestamp window). If the nonce has
   been seen before, the request is rejected. This prevents replay attacks
   even within the relaxed 20-minute clock skew window.
5. Checks that the boat holds a valid membership record for the requested
   co-op
6. Logs the access to the audit trail (including nonce hash for forensics)

No OAuth, no tokens to refresh, no central auth server. Just signatures.

### Clock skew handling

Raspberry Pis without a battery-backed RTC (Real-Time Clock) lose time when
powered down at the dock without internet. The protocol must tolerate this:

- **Default window**: 5 minutes (when both Pis have NTP sync)
- **Relaxed window**: 20 minutes (if either Pi's last NTP sync is >1 hour
  old, indicated by a `X-HelmLog-NTP-Age` header)
- **Peer clock slew**: on startup, if a Pi lacks NTP sync, it queries
  heartbeat timestamps from 3+ Tailscale peers and uses the median as a
  reference to slew its local clock
- The signature is always valid if the cryptographic verification passes;
  the timestamp window is a replay-protection heuristic, not an auth gate.
  A valid signature with an out-of-window timestamp logs a warning but does
  not reject the request — it just flags it in the audit trail

---

## 5. Data Flow Patterns

### Race day (all Pis on same network)

```
Race ends
  → Each Pi marks session as co-op-shared (or not, boat's choice)
  → Pis discover each other via Tailscale peer list
  → Co-op view on any Pi queries all online peers for today's sessions
  → Track data rendered as multi-boat replay
  → Nothing is copied — all queries are live
```

### Post-race review (Pis at marina, some offline)

```
Crew member opens co-op view on their Pi
  → Pi queries all known co-op members over Tailscale
  → Online Pis respond with session data
  → Offline Pis time out → UI shows "2 of 5 boats available"
  → Optional: if caching is enabled, previously fetched sessions
    are available from local cache (respects tombstones)
```

### Coach access

Coach access is **per-boat opt-in** with **session-level permissioning**.
The admin does not grant co-op-wide coach access — each boat individually
decides whether to share with a specific coach.

```
Coach requests access to Boat A
  → Boat A owner grants coach a time-limited, session-scoped access record
    signed by Boat A's key (not the co-op admin)
  → Access record specifies: coach pub key, allowed session IDs (or "all"),
    expiry date, no-aggregation flag
  → Coach's device gets a keypair + per-boat access records
  → Coach queries only Boat A's Pi directly over Tailscale
  → Boat A's Pi verifies: access record signature, expiry, session scope
  → Access logged to audit trail
  → After expiry, Pi rejects the coach's requests automatically

Coach wants access to Boat B too
  → Boat B owner independently grants (or denies) access
  → Coach accumulates per-boat access records
  → Each Pi enforces its own access scope independently
```

A coach may **not aggregate** multiple boats' data into a derived dataset
that could be shared with other clients. This is a normative obligation
(see data licensing policy Section 1) — the platform logs access patterns
but cannot technically prevent a coach from combining knowledge.

### Fleet benchmarking (Percentile Heatmap)

```
Boat owner opens benchmarking dashboard
  → Pi queries all online co-op peers for /benchmarks/maneuvers
    and /benchmarks/polar (in parallel)
  → Each peer returns its own anonymous metric arrays
  → Pi aggregates into fleet statistics per maneuver per condition bin
  → Bins with <4 contributing boats show "insufficient data"
  → Embargoed session data excluded from aggregation
  → Dashboard renders Percentile Heatmap:

    | Maneuver       | Fleet 10th% | Median | Fleet 90th% | You  | %ile |
    | Upwind tacks   | 4.8 sec     | 5.2 s  | 6.0 sec     | 5.5s | 60%  |
    | Downwind gybes | 3.0 sec     | 3.5 s  | 4.2 sec     | 3.8s | 55%  |
    | Mark rounding  | 12.5 sec    | 13.0 s | 14.2 sec    | 13.5 | 58%  |
    | Acceleration   | 1.5 m/s²    | 1.3    | 1.0 m/s²    | 1.2  | 40%  |

  → Color coded: green (top 25%), yellow (mid 50%), red (bottom 25%)
  → Click any row to drill into historical trend (own-boat only)
  → No per-boat data stored — only aggregates held in memory
```

This is the co-op's primary value proposition for competitive sailors.
It answers "where am I losing time?" without revealing who is fast.

### Voting on a proposal

```
Admin creates proposal (e.g., "enable current model sharing")
  → Admin signs proposal record, distributes to all members
  → Each member's Pi displays the proposal in the co-op admin UI
  → Boat owner votes (approve/reject) → Pi signs the vote
  → Signed vote sent to admin's Pi (or any peer — votes are
    idempotent and verifiable by anyone)
  → Once threshold met (2/3, unanimous, etc.), admin signs a
    resolution record and distributes it
  → All Pis update their local agreement state
```

---

## 6. Peer Caching (Optional)

By default, co-op queries are live — no data is copied. But for offline
resilience, boats can opt into **peer caching**:

- When a co-op session is fetched, the requesting Pi can cache the track
  data locally with a TTL (e.g., 30 days)
- Cached data is tagged with the source boat's fingerprint and session ID
- Tombstone polling: each Pi periodically checks peers for tombstones and
  purges any cached data that's been deleted or anonymized at the source
- Cache is encrypted at rest using the co-op's public key (so if the Pi is
  stolen, cached co-op data isn't readable without the co-op key)

Caching is **opt-in per boat** (the source boat decides whether its data
is cacheable) and **opt-in per Pi** (the receiving boat decides whether to
store cached data locally).

---

## 6.5. Processing Offload

Some tasks are too heavy for the Raspberry Pi: audio transcription, speaker
diarization, photo analysis, video processing, and potentially benchmark
aggregation for large co-ops. Helm Log supports **offloading** these tasks
to a faster machine.

### Current implementation: transcription offload

The Pi sends WAV files over the Tailscale mesh to a worker process (e.g.,
a Mac running `scripts/transcribe_worker.py`). The worker runs
faster-whisper and returns the transcript. The Pi stores the result in
SQLite. This is configured via the `TRANSCRIBE_URL` environment variable.

```
Pi (corvopi)                          Mac (worker)
  |                                     |
  | --- POST /transcribe (WAV) ------>  |
  |     over Tailscale                  |
  |                                     |  runs faster-whisper
  |                                     |  + optional diarization
  | <-- JSON (text + segments) ------   |
  |                                     |
  | stores in SQLite                    | deletes WAV + artifacts
```

### Protocol rules for offload

1. **Offload hosts are ephemeral processors, not storage.** The Pi is the
   single source of truth. The offload host must delete the source file and
   all intermediate artifacts after returning the result.

2. **PII obligations follow the data.** Audio contains crew voices (PII).
   If a crew member requests voice deletion, the boat owner must ensure the
   offload host has also purged any cached copies. The Pi logs all offload
   events (what was sent, where, when) for audit purposes.

3. **Transport must be encrypted.** Tailscale provides this by default.
   For offload to non-Tailscale endpoints (cloud APIs, public services),
   HTTPS (TLS 1.2+) is required. The Pi should warn the user when
   configuring an offload URL outside the Tailscale network.

4. **Own-boat offload needs no co-op approval.** The boat owner sending
   their own data to their own hardware is a local processing decision.
   The co-op has no say in how a boat processes its own private data.

5. **Co-op offload requires a vote.** If a co-op wants to designate a
   shared processing host (e.g., for current model aggregation or
   benchmark computation), the host must be approved by 2/3 supermajority
   vote and identified in the charter. The offload host must not retain
   raw per-boat data beyond the computation — only aggregated results.
   Audit logging of the host's data access is required.

### Future offload patterns

The transcription offload pattern generalizes to:

- **Still photo analysis** — sail shape measurement, rig tune detection,
  mark identification
- **Video analysis** — automated maneuver detection from on-board camera
  footage, start replay analysis
- **ML inference** — running trained models (polar prediction, current
  estimation) on hardware with GPU acceleration
- **Benchmark aggregation** — if co-ops grow beyond 20-30 boats, a
  designated aggregator could collect metrics from all peers and compute
  fleet benchmarks centrally instead of each Pi doing it independently

All patterns follow the same rules: encrypted transit, ephemeral
processing, no data retention on the offload host, PII deletion
obligations, and co-op approval for shared infrastructure.

---

## 6.6. Video and the Camera Pipeline

Helm Log controls on-board cameras (Insta360 X4) that automatically record
during sessions. The resulting video flows through a pipeline that is
**entirely outside the co-op protocol** — it is a boat-private operation
with third-party (YouTube) involvement.

### Pipeline overview

```
Session starts → Pi triggers camera via OSC HTTP API
Session ends   → Pi stops camera
                 ↓
SD card transferred to Mac (manual or launchd trigger)
                 ↓
.insv (360°) → Docker stitch → equirectangular .mp4
.mp4 (single-lens) → direct
                 ↓
Upload to YouTube (unlisted by default)
                 ↓
Pi links video via POST /api/sessions/{id}/videos
  with sync point: (sync_utc, sync_offset_s)
                 ↓
Video appears in session history with time-synced playback
```

### Why video is not in the co-op API

Video is the **most tactically revealing data type** the platform handles.
360° footage shows sail trim, crew weight placement, tacking sequence,
mark rounding approach, and competitive information that instrument data
alone cannot capture. For this reason:

1. **Video metadata (YouTube links, sync points) is boat-private by default**
   — not served via any co-op endpoint.
2. **Video content is hosted on YouTube**, not on the Pi. The co-op protocol
   has no mechanism to serve, cache, or proxy video content between peers.
3. **Coach access to video** follows the same per-boat, session-scoped model
   as other private data. A coach with an access record can view a boat's
   linked videos for their authorized sessions, but video links are not
   included in co-op session lists.

### Video PII in the protocol context

Video is PII (crew faces, voices, other boats' identifying marks). The
protocol implications:

- **Tombstone propagation**: if a boat requests deletion, YouTube video
  metadata is deleted from the Pi and any co-op references (coach access
  records, session metadata). The YouTube video itself is outside the
  protocol's reach — it persists on YouTube until the uploader deletes it.
- **Processing offload**: video stitching and upload run on the Mac (see
  Section 6.5). The same ephemeral processing rules apply — the Mac should
  not retain raw video after upload completes.
- **Crew PII requests**: if a crew member requests face-blur in video, this
  is handled on the boat owner's machine (re-process and re-upload), not
  through the co-op protocol.

### Future: video sharing in the co-op

If a future version enables voluntary video sharing between co-op members:

- Video links (not content) would be served via an opt-in co-op endpoint
- The same event-scoping and embargo rules that apply to track data would
  apply to video links
- Video content would still be hosted on YouTube — the co-op protocol would
  only share the link and sync metadata
- Given the tactical sensitivity of 360° footage, video sharing would likely
  require explicit per-session opt-in (not blanket sharing like instrument
  data)

---

## 7. Per-Event Co-op Assignment

When a boat belongs to multiple co-ops, session sharing works as follows:

1. At race start (or when marking a session as shared), the UI presents
   a co-op selector if the boat has multiple memberships
2. The boat owner picks **one** co-op for that session
3. The session's `co_op_id` is stored in SQLite
4. The session only appears in API responses for that co-op
5. This is enforced locally on the source Pi — no coordination needed

For sessions that don't overlap with another co-op's events (e.g., a
Wednesday night race when the second co-op only covers weekends), the
boat can share with both if the co-ops' charters allow it.

---

## 8. Current Model Computation

The observed current model is the one feature that requires aggregation
across multiple boats. Here's how it works without a central server:

1. **Unanimous consent verified**: the admin's Pi holds signed approval
   records from every member for the current-sharing agreement
2. **Computation runs on the admin's Pi** (or any designated member):
   - Queries each member Pi for current observations via
     `GET /co-op/{id}/currents?area=<area>`
   - Combines BSP/HDG vs SOG/COG vectors from all boats
   - Bins by location, tide cycle phase, and time
   - Produces a current model (grid of velocity vectors)
3. **Model is signed** by the computing Pi and distributed to members
4. **Each Pi can verify** the model signature and the underlying consent
   records
5. **Per-area opt-out**: a boat that opted out of a specific area simply
   returns 403 for queries in that area — the model is built without them

---

## 8.5. Fleet Benchmark Computation

Fleet benchmarking is the second feature (after current models) that requires
aggregation across boats. Unlike current models, **benchmarking does not
require a governance vote** — it uses only the instrument data and derived
metrics that are already shared by default under Section 2 of the data
licensing policy.

### How it works without a central server

1. **Maneuver detection runs locally** on each Pi after each session:
   - Tack detection: heading change >60° with BSP dip
   - Gybe detection: heading change >60° downwind with BSP dip
   - Mark rounding: GPS track curvature + heading change at known mark positions
   - Start: proximity to start line at gun time
   - Acceleration: BSP recovery rate after maneuver
   - Results stored in local `maneuver_events` table

2. **Benchmark query is peer-to-peer**: the requesting Pi queries all
   online co-op peers for `/benchmarks/maneuvers` (anonymous metric arrays).
   Each peer returns its own metrics — no identities attached.

3. **Aggregation runs on the requesting Pi**: compute median, 10th/25th/75th/90th
   percentiles per maneuver type per TWS bin. If fewer than 4 boats contribute
   to a bin, that bin shows "insufficient data."

4. **No persistent fleet-wide storage**: benchmark results exist only in
   the requesting Pi's memory during the dashboard session. No Pi stores
   fleet-wide benchmark data to disk. (Individual maneuver events are
   stored locally for the boat's own history.)

### Privacy properties

- Each peer returns an **anonymous array** of metrics — no boat identifiers
  in the response
- The requesting Pi knows which Tailscale IP returned which array (necessary
  for the query), but the aggregation discards per-source attribution
- With the minimum 4-boat threshold, no single boat can be isolated in the
  aggregate statistics
- The small-fleet anonymization disclaimer applies: in a 5-boat co-op,
  "top 10%" is effectively one boat

---

## 9. SQLite Schema Additions

New tables on each Pi to support federation:

```sql
-- This boat's keypair reference (key material in filesystem, not DB)
CREATE TABLE IF NOT EXISTS boat_identity (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
    pub_key     TEXT NOT NULL,       -- base64 Ed25519 public key
    fingerprint TEXT NOT NULL,       -- SHA-256 truncated
    sail_number TEXT NOT NULL,
    boat_name   TEXT,
    created_at  TEXT NOT NULL
);

-- Co-ops this boat belongs to
CREATE TABLE IF NOT EXISTS co_op_memberships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    co_op_id        TEXT NOT NULL,       -- fingerprint of co-op public key
    co_op_name      TEXT NOT NULL,
    co_op_pub       TEXT NOT NULL,       -- base64 co-op public key
    membership_json TEXT NOT NULL,       -- full signed membership record
    role            TEXT NOT NULL DEFAULT 'member',  -- member | admin
    joined_at       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',  -- active | departing | revoked
    UNIQUE(co_op_id)
);

-- Per-session co-op sharing decisions
CREATE TABLE IF NOT EXISTS session_sharing (
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    co_op_id    TEXT NOT NULL,
    shared_at   TEXT NOT NULL,
    shared_by   INTEGER REFERENCES users(id),
    embargo_until TEXT,              -- null = immediate sharing; ISO timestamp = embargoed
    event_name  TEXT,                -- event identifier for event-scoped visibility
    PRIMARY KEY (session_id, co_op_id)
);

-- Known peers (other boats in co-ops we belong to)
CREATE TABLE IF NOT EXISTS co_op_peers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    co_op_id        TEXT NOT NULL,
    boat_pub        TEXT NOT NULL,       -- base64 public key
    fingerprint     TEXT NOT NULL,
    sail_number     TEXT,
    boat_name       TEXT,
    tailscale_ip    TEXT,                -- last known Tailscale IP
    last_seen       TEXT,                -- last successful query
    membership_json TEXT NOT NULL,       -- signed membership record
    UNIQUE(co_op_id, fingerprint)
);

-- Co-op data access audit trail
CREATE TABLE IF NOT EXISTS co_op_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    co_op_id        TEXT NOT NULL,
    accessor_fp     TEXT NOT NULL,       -- fingerprint of requesting boat
    action          TEXT NOT NULL,       -- session_list | track_fetch | current_fetch | benchmark_fetch
    resource        TEXT,                -- e.g., session_id
    timestamp       TEXT NOT NULL,
    ip              TEXT,
    points_count    INTEGER,             -- number of data points returned (1Hz samples, maneuver events, etc.)
    bytes_transferred INTEGER,           -- response payload size in bytes
    nonce_hash      TEXT                 -- SHA-256 of request nonce for replay forensics
);

-- Rate limiting: the Pi tracks per-peer rolling windows over co_op_audit.
-- If a peer exceeds thresholds (e.g., 50+ track_fetch actions in 1 hour,
-- or 500K+ points_count in 1 hour), the Pi auto-freezes that peer's access
-- and alerts the co-op admin. This enforces the "no bulk export" policy at
-- the API level by detecting scraping patterns from data volume, not just
-- request count.

-- Tombstones received from peers (for cache invalidation)
CREATE TABLE IF NOT EXISTS co_op_tombstones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    co_op_id        TEXT NOT NULL,
    source_fp       TEXT NOT NULL,       -- boat that deleted the data
    session_id      INTEGER,
    action          TEXT NOT NULL,       -- delete | anonymize
    effective_at    TEXT NOT NULL,
    tombstone_json  TEXT NOT NULL,       -- full signed tombstone
    received_at     TEXT NOT NULL
);

-- Cached session data from peers (optional, opt-in)
CREATE TABLE IF NOT EXISTS co_op_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    co_op_id        TEXT NOT NULL,
    source_fp       TEXT NOT NULL,
    session_id      INTEGER NOT NULL,
    data_type       TEXT NOT NULL,       -- track | results | polar
    data_json       TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    UNIQUE(co_op_id, source_fp, session_id, data_type)
);

-- Detected maneuver events (local, not shared directly)
CREATE TABLE IF NOT EXISTS maneuver_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    type        TEXT NOT NULL,       -- tack | gybe | mark_rounding | start | acceleration
    timestamp   TEXT NOT NULL,       -- UTC
    duration_sec REAL,               -- maneuver duration
    loss_metric  REAL,               -- speed/VMG loss during maneuver
    tws_bin     TEXT,                -- e.g., "10-12"
    twa_bin     TEXT,                -- e.g., "30-40"
    details_json TEXT                -- type-specific details
);

-- Coach access records (per-boat, session-scoped)
CREATE TABLE IF NOT EXISTS coach_access (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    coach_pub       TEXT NOT NULL,       -- base64 Ed25519 public key
    coach_name      TEXT,
    session_scope   TEXT NOT NULL,       -- "all" or JSON array of session IDs
    granted_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    revoked_at      TEXT,                -- null if still active
    access_json     TEXT NOT NULL,       -- full signed access record
    UNIQUE(coach_pub, granted_at)
);

-- Signed votes on co-op proposals
CREATE TABLE IF NOT EXISTS co_op_votes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    co_op_id        TEXT NOT NULL,
    proposal_id     TEXT NOT NULL,
    proposal_json   TEXT NOT NULL,       -- signed proposal
    vote            TEXT,                -- approve | reject | null (pending)
    vote_json       TEXT,                -- signed vote (once cast)
    created_at      TEXT NOT NULL
);

-- Nonce deduplication for replay protection (Section 4)
-- Entries are pruned when their timestamp falls outside the clock skew window
CREATE TABLE IF NOT EXISTS request_nonces (
    nonce_hash  TEXT PRIMARY KEY,         -- SHA-256 of nonce
    timestamp   TEXT NOT NULL,            -- request timestamp
    boat_fp     TEXT NOT NULL             -- requesting boat fingerprint
);
```

---

## 10. New Python Modules

```
src/helmlog/
├── federation.py       # Core federation logic
│   ├── generate_keypair()
│   ├── sign_message(private_key, message) -> signature
│   ├── verify_signature(public_key, message, signature) -> bool
│   ├── create_boat_card(key_dir, sail_number, name) -> dict
│   ├── create_co_op(key_dir, name, areas) -> dict
│   ├── sign_membership(co_op_key, boat_card, role) -> dict
│   ├── verify_membership(co_op_pub, membership_record) -> bool
│   ├── sign_revocation(co_op_key, boat_pub, reason) -> dict
│   ├── sign_tombstone(boat_key, session_ids, action) -> dict
│   ├── verify_request(pub_key, method, path, timestamp, nonce, sig) -> bool
│   ├── check_nonce(nonce_hash, timestamp) -> bool  # replay protection
│   ├── sign_coach_access(boat_key, coach_pub, sessions, expiry) -> dict
│   ├── verify_coach_access(boat_pub, access_record) -> bool
│   └── CoOpPeer (dataclass for peer connection state)
│
├── co_op_api.py        # FastAPI router for /co-op/* endpoints
│   ├── router = APIRouter(prefix="/co-op")
│   ├── Middleware: verify_co_op_request (signature check)
│   ├── All endpoints from Section 3 above
│   ├── Embargo enforcement: check co-op sharing delay before serving data
│   ├── Event-scoping: check requesting boat's event participation
│   ├── Data aging: downsample older sessions per charter config
│   └── Depends on federation.py + storage.py
│
├── co_op_client.py     # Client for querying other Pis
│   ├── query_peer_sessions(peer, co_op_id, filters) -> list[dict]
│   ├── fetch_track(peer, co_op_id, session_id) -> list[dict]
│   ├── fetch_results(peer, co_op_id, session_id) -> list[dict]
│   ├── fetch_currents(peer, co_op_id, area, time_range) -> list[dict]
│   ├── poll_tombstones(peer, co_op_id, since) -> list[dict]
│   ├── submit_vote(peer, co_op_id, proposal_id, vote) -> dict
│   ├── discover_peers(co_op_id) -> list[CoOpPeer]
│   └── aggregate_co_op_view(co_op_id, filters) -> dict
│       # Queries all online peers in parallel, merges results,
│       # applies cache, returns unified session list
│
├── benchmarks.py       # Fleet benchmarking engine
│   ├── detect_maneuvers(session_data) -> list[ManeuverEvent]
│   │   # Detect tacks, gybes, mark roundings, starts, acceleration
│   │   # from instrument data (heading rate, BSP delta, GPS geometry)
│   ├── compute_maneuver_metrics(maneuvers) -> list[ManeuverMetric]
│   ├── fetch_fleet_metrics(peers, co_op_id) -> list[AnonymousMetrics]
│   │   # Query all peers for /benchmarks/maneuvers and /benchmarks/polar
│   ├── aggregate_benchmarks(fleet_metrics, own_metrics, min_boats=4)
│   │   -> BenchmarkResult
│   │   # Compute percentiles, enforce min-boat threshold, bin by condition
│   ├── ManeuverEvent (dataclass: type, session_id, timestamp, duration)
│   ├── ManeuverMetric (dataclass: type, tws_bin, loss_sec)
│   └── BenchmarkResult (dataclass: rows of maneuver × fleet stats × rank)
│
└── maneuver_detect.py  # Maneuver detection from instrument data
    ├── detect_tacks(heading_series, bsp_series) -> list[TackEvent]
    ├── detect_gybes(heading_series, bsp_series) -> list[GybeEvent]
    ├── detect_mark_roundings(gps_track, course_marks) -> list[RoundingEvent]
    ├── detect_starts(gps_track, start_line) -> list[StartEvent]
    └── detect_accelerations(bsp_series) -> list[AccelEvent]
```

---

## 11. Peer Discovery

How does a Pi find other co-op members on the Tailscale network?

| Approach | Pros | Cons |
|---|---|---|
| **Option A: Tailscale API** — scan `/localapi/v0/status` for peers, probe each with `GET /co-op/identity` | Zero configuration — discovers peers automatically; handles IP changes and new members without admin action; works even if membership records are stale | Chatty — probes every Tailscale peer (not just co-op members); slower startup; doesn't work if Pis aren't on the same tailnet; small attack surface (port-scanning peers) |
| **Option B: Membership record exchange** — admin distributes a member list with Tailscale hostnames/IPs | Instant discovery — you know exactly who to query; no probing; works across tailnets (via Funnel URLs); member list is signed and verifiable | Requires admin action when IPs change; stale if a Pi moves to a new Tailscale node; admin is in the critical path for peer updates |

**Decision: hybrid (Option B primary, Option A fallback).**

Use Option B (explicit member list from admin) as the primary mechanism.
Each membership record includes the member's Tailscale hostname. Pis
query known members directly — no scanning. If a known peer is unreachable
at its last-known address, fall back to Option A (Tailscale API scan) to
re-discover the peer at a new IP. This gives instant startup from the
member list with self-healing when addresses change.

Option A scanning runs on startup and every 10 minutes, but only probes
Tailscale peers that aren't already in the `co_op_peers` table with a
recent `last_seen` timestamp. This keeps the scan lightweight.

---

## 12. What This Does NOT Require

- **No blockchain** — identity is Ed25519 keypairs, membership is signed
  records, votes are signed messages. Standard public-key cryptography.
- **No central server** — each Pi serves its own data. The co-op is a mesh.
- **No cloud storage** — data stays on the Pi that generated it (unless
  peer caching is opted into).
- **No DNS changes** — Tailscale handles addressing. Each Pi is reachable
  at its Tailscale IP.
- **No new TLS certs** — Tailscale provides end-to-end encryption between
  peers. The co-op API runs over the Tailscale mesh, not the public internet.
- **No OAuth / OIDC / JWT** — request auth is a signature over the request
  method, path, and timestamp. The boat's Ed25519 key is the credential.

---

## 13. Migration Path

### Phase 1: Identity + local co-op tables
- Generate keypairs on each Pi
- Add federation tables to SQLite (schema migration)
- CLI: `helmlog identity init`, `helmlog co-op create`, `helmlog co-op invite`
- No networking yet — just the data model

### Phase 2: Co-op API + peer queries
- Add `/co-op/*` router to FastAPI
- Implement request signing and verification
- Query peers for session lists and track data
- Temporal sharing controls (embargo enforcement)
- Event-scoped session visibility
- Co-op view page in web UI

### Phase 3: Fleet benchmarking
- Maneuver detection from instrument data (tacks, gybes, mark roundings,
  starts, acceleration)
- Benchmark API endpoints (`/benchmarks/maneuvers`, `/benchmarks/polar`)
- Fleet benchmark aggregation engine (min 4-boat threshold)
- **Percentile Heatmap dashboard** — the primary co-op value proposition
- Benchmark embargo sync (exclude embargoed data)

### Phase 4: Governance + voting
- Proposal creation and vote collection
- Agreement state management
- Pre-join disclosure endpoint
- Membership eligibility enforcement

### Phase 5: Current models + advanced features
- Current observation sharing
- Aggregated current model computation
- Coach access records (per-boat opt-in, session-scoped)
- Data aging tiers
- Peer caching (opt-in)
- Benchmark historical trends and drilldowns

---

## 14. Protocol Versioning & Upgrades

### Protocol version header

Every co-op API response includes a `X-HelmLog-Protocol` header:

```
X-HelmLog-Protocol: 1.0
```

The version follows `major.minor`:
- **Major** increments are breaking changes (new required fields in signed
  records, changed authentication semantics, removed endpoints)
- **Minor** increments are additive (new optional endpoints, new optional
  fields in existing records)

### Compatibility rules

- A Pi **must** accept requests from peers with the same major version
- A Pi **should** accept requests from peers with a higher minor version
  (ignore unknown fields)
- A Pi **may** reject requests from a peer with a different major version,
  returning `426 Upgrade Required` with a human-readable message
- Signed records (membership, revocation, charter) include a
  `protocol_version` field. Records signed under an older major version
  remain valid — the signature doesn't expire with a protocol upgrade

### Rolling upgrades

Co-ops don't upgrade atomically. During a transition:

1. The new version is released. Pis update via `deploy.sh` on their own
   schedule (some within hours, some within weeks)
2. Updated Pis include the new version in their heartbeat
3. The admin Pi can query `GET /co-op/{id}/heartbeat` to see which Pis
   are on which version
4. Once all active Pis are on the new version, the admin can issue a
   charter amendment setting `min_protocol_version` to the new major,
   which rejects connections from outdated Pis

For Phase 1-3, there is only one protocol version (`1.0`). Versioning
infrastructure is included from the start so it doesn't have to be
retrofitted.

---

## 15. Security Assumptions & Threat Model

### What the protocol trusts

| Assumption | Why it's acceptable |
|---|---|
| **Tailscale identity is authentic.** A Pi's Tailscale node key maps to a real device on the co-op's tailnet. | Tailscale uses WireGuard keys authenticated via the control plane. Spoofing requires compromising the boat owner's Tailscale account. |
| **Pi physical security is the boat owner's responsibility.** The private key on the Pi is as secure as the Pi itself. | Same as any personal computing device. We encrypt caches at rest, but the Pi's own data is protected by physical access control (locked nav station, secured below). |
| **The OS is not compromised.** The Pi runs a standard Raspberry Pi OS with security hardening (see `setup.sh`). | Automatic security updates, dedicated service account, SSH hardened, unused services masked. |
| **Signed records are non-repudiable.** Once a membership or revocation record is signed and distributed, the signer cannot deny it. | Ed25519 signatures are deterministic. The signed record includes the signer's public key fingerprint. |

### Threat model

| Threat | Mitigation | Residual risk |
|---|---|---|
| **Malicious insider** (member scrapes all shared data) | Audit logging with volume-based rate limiting; auto-freeze on anomalous access patterns (50+ views/minute or bulk data volume); admin alerted | A patient insider accessing data at normal rates over weeks. Mitigation: audit trail makes this detectable retrospectively. |
| **Compromised Pi** (stolen or remotely accessed) | Admin issues revocation, pushed to all online peers. Revoked Pi's identity is rejected on all future requests. Owner re-joins with a new keypair. | Window between compromise and revocation — attacker can access shared data during this period. |
| **Lost keypair** (SD card failure, no backup) | Boat re-joins as a new identity. Old identity can be revoked by admin. Data the boat previously shared remains cached on peers under the old identity. | Historical data on the dead Pi is lost if not backed up. Old identity remains valid until explicitly revoked. |
| **Admin collusion** (admins abuse multi-sig) | M-of-N threshold means a single admin cannot act alone. All admin actions are signed and auditable. Charter amendment for admin removal requires member vote. | If M admins collude (e.g., 2 of 3), they can issue fraudulent membership or revocation records. Detection: all records are visible to all members. |
| **Replay attack** | Per-request nonce, checked against seen-nonce set bounded by timestamp window. Requests with duplicate nonces are rejected. | If both NTP and peer clock slew fail, the relaxed 20-minute window is the maximum replay window. |
| **Man-in-the-middle** | Tailscale provides WireGuard-level E2E encryption. API requests are additionally signed with the boat's Ed25519 key. | Requires compromising Tailscale infrastructure or the boat's WireGuard key. |
| **helmlog.org gateway compromise** | Gateway is stateless — no sailing data at rest. It routes requests to Pis but cannot read signed payloads. Pi-side auth (magic-link) prevents unauthorized access even if routing is compromised. | Attacker could deny service (route requests to wrong Pi) or observe metadata (which boats are online). Cannot access session data. |

### What the protocol does NOT protect against

- A boat owner who lies about their identity (social engineering)
- A coach who photographs a screen (normative obligation, not technical)
- A legal subpoena for data on a Pi (law enforcement can compel disclosure)
- Bugs in the implementation (standard software risk)

---

## 16. Failure Modes & Recovery

### Identity and key management

| Failure | Recovery |
|---|---|
| **Pi SD card dies, no backup** | Boat re-joins as new identity. Admin revokes old identity. Previously shared data remains on peers under old fingerprint. Historical data on the Pi is lost. |
| **Pi SD card dies, backup exists** | Restore `boat.key` and `boat.json` from backup. Pi re-joins as same identity. No admin action needed. Restore `helmlog.db` for historical data. |
| **Pi stolen** | Contact admin immediately. Admin issues signed revocation record, pushed to all online peers. Old identity rejected on all future requests. Owner creates new identity on replacement Pi. |
| **Keypair exposed** (key file leaked) | Same as stolen Pi — admin revokes old identity. Owner generates new keypair and re-joins. |

### Admin disagreements

| Failure | Recovery |
|---|---|
| **Admins disagree on revocation timing** | Multi-sig threshold determines the outcome. If M-of-N admins sign the revocation, it takes effect regardless of dissent from the remaining admins. If fewer than M agree, the revocation does not proceed. |
| **Admin goes rogue** (refuses to sign, blocks legitimate actions) | Other admins can propose a charter amendment to remove the rogue admin. Requires 2/3 member vote. The rogue admin's signature is no longer counted toward the M-of-N threshold after removal. |
| **All admins become unavailable** | Members vote (2/3 supermajority) to elect new admins. If quorum cannot be reached (too many inactive), active members can invoke a bootstrap re-election by signing a petition (majority of active heartbeating members). |

### Network and sync failures

| Failure | Recovery |
|---|---|
| **A Pi is offline for weeks** | Data syncs on reconnection. Heartbeat marks the boat inactive after 60 days (excluded from quorum denominator). No data loss. |
| **Revocation push fails** (target Pi offline) | Revocation record is stored on all online peers. Offline Pi receives it via tombstone polling on reconnection. Window of stale access is bounded by the offline period. |
| **A malicious boat drops revocation messages** | Revocations are pushed to all peers, not just the target. Even if the target ignores the message, all other peers enforce it. The revoked boat can access its own data but no other peer will serve it co-op data. |

### Data integrity

| Failure | Recovery |
|---|---|
| **Corrupted database** | Pi restores from backup. Shared data can be re-pulled from peer caches. Identity key is stored separately from the database. |
| **Accidental session share** | Boat owner un-shares the session. Tombstone record propagated to peers. Cached copies deleted on next sync. |
| **Divergent benchmark results** (Pis compute different numbers) | Expected — each Pi queries at a slightly different time and may see different sets of peers. Benchmarks converge as all Pis come online. Not a failure; a property of decentralized computation. |

---

## 17. Event Naming & Canonicalization

Proof of Participation (PoP) depends on matching event names across boats.
If one boat types "CYC Wed" and another types "CYC Wednesday," PoP breaks.

### Solution: canonical event IDs

1. **Admin-defined event calendar.** The co-op admin creates a calendar of
   events in the charter metadata:

```json
{
  "events": [
    { "id": "cyc-wed", "name": "CYC Wednesday Night", "pattern": "weekly:wed" },
    { "id": "ballard-mon", "name": "Ballard Cup Monday", "pattern": "weekly:mon" },
    { "id": "swiftsure-2026", "name": "Swiftsure 2026", "dates": ["2026-05-23", "2026-05-24"] }
  ]
}
```

2. **Auto-matching on session start.** When a boat starts a race, the Pi
   checks the event calendar by day/date and suggests the matching event.
   The boat owner confirms or overrides.

3. **PoP uses the canonical event ID**, not the free-text event name. Two
   boats at the same event will have the same ID even if their display
   names differ.

4. **Unknown events.** If a session doesn't match any calendar entry, the
   boat owner enters a free-text name. PoP for ad-hoc events falls back to
   temporal proximity (sessions overlapping in time at similar GPS positions
   are assumed to be the same event).

---

## 18. Charter vs Agreements

### What lives in the charter

The **charter** is the co-op's constitution — it defines structure and
rules that apply to all members:

- Co-op name, class, geographic scope
- Admin roster and governance mode (single moderator or multi-admin)
- Membership requirements (minimum sessions, dual membership policy)
- Season dates
- Default sharing mode (event-scoped or full visibility)
- Embargo policy (if any)
- Benchmark cache TTL
- Event calendar
- `min_protocol_version`

Charter amendments require a 2/3 supermajority vote.

### What lives in agreements

**Agreements** are specific, scoped authorizations that can be added or
removed without amending the charter:

- Current model sharing (unanimous consent)
- ML/AI model training projects (2/3 vote)
- Commercial use arrangements (2/3 vote)
- Cross-co-op data sharing (2/3 vote from both co-ops)

Each agreement has:
- A **signed proposal** (who proposed it, when, what it authorizes)
- **Signed votes** from each voting member
- A **status** (active, expired, revoked)
- An optional **expiration date**

### Key differences

| | Charter | Agreement |
|---|---|---|
| Scope | Structural rules | Specific authorizations |
| Amendment | 2/3 supermajority | Varies by type |
| Expiration | No (persists until amended) | Optional (can be time-limited) |
| Pre-join disclosure | Yes — shown to prospective members | Yes — all active agreements shown |
| Superseding | New amendment replaces old | New agreement of same type replaces old |

Agreements do not require charter amendments. A co-op can activate current
model sharing via a unanimous vote without touching the charter. The
charter's "Active Agreements" section in the template is a disclosure
summary, not the agreements themselves.

---

## 19. Co-op Dissolution

When a co-op ceases to operate:

### Voluntary dissolution

1. An admin proposes dissolution (charter amendment)
2. 2/3 supermajority of active members vote to approve
3. On approval:
   - All membership records are marked as revoked (dissolution reason)
   - Coach access records are revoked
   - Active agreements are terminated
   - Each Pi retains its own data (instrument, audio, notes) permanently
   - Peer caches are purged within 30 days (same as departure)
   - Fleet benchmarks are retained locally on each Pi but no longer updated
   - The co-op identity (public key) is retired — cannot be reused

### Dormancy (no active governance)

Per the data licensing policy, if a co-op has no governance activity
(votes, membership changes, charter amendments) for 2 years:

1. The co-op enters **dormant** status
2. No new data sharing occurs
3. Existing shared data remains accessible to members
4. Any admin can re-activate by issuing a charter amendment
5. If no admin acts within 6 months of dormancy, the co-op auto-dissolves

### What dissolution does NOT do

- It does not delete any boat's own data
- It does not affect the boat's identity (keypair remains valid)
- It does not prevent the same boats from forming a new co-op
- It does not affect memberships in other co-ops

---

## 20. Inter-Co-op Boundaries

### Can a boat share the same session with multiple co-ops?

**No, by default.** Per-event exclusivity (Section 7) requires that each
shared session is assigned to exactly one co-op. This prevents the same
data from appearing in multiple co-ops' benchmark pools, which would
create cross-co-op information leakage.

**Exception:** If both co-ops vote (2/3 supermajority each) to establish a
cross-co-op sharing agreement, sessions from joint events can be shared
with both. The agreement specifies which events are covered.

### Can a boat belong to two co-ops with overlapping membership?

**Yes.** A boat can belong to a J/105 co-op and a PHRF co-op simultaneously.
Per-event exclusivity ensures each session goes to only one co-op. The
boat owner picks which co-op gets each session at share time.

### Can two co-ops merge?

Not directly. To merge:

1. Create a new co-op with the combined membership
2. Both old co-ops dissolve
3. Members share new sessions with the merged co-op going forward
4. Historical data from old co-ops is not migrated (no backfill)

---

## 21. Performance Envelope

### Expected resource usage

| Metric | Expected range |
|---|---|
| **Storage per boat per season** | ~50-100 MB (instrument data at 1 Hz, ~6 months, 2-3 sessions/week) |
| **Storage with audio** | +500 MB - 2 GB per season (WAV recordings) |
| **Storage with video metadata** | Negligible (links only; video is on YouTube/SD card) |
| **Peer cache size** | ~10-50 MB per co-op (track data from ~10 boats, 30-day TTL) |
| **SQLite DB total** | ~200 MB - 2 GB after 2+ seasons with audio |

### Query performance

| Operation | Expected latency (Pi 4/5 on Tailscale) |
|---|---|
| **Session list** (`GET /co-op/{id}/sessions`) | <100 ms per peer |
| **Track data** (`GET /co-op/{id}/sessions/{id}/track`) | 200-500 ms (1 Hz data, ~2 hour race = ~7200 points) |
| **Benchmark pull from all peers** (10-boat co-op) | 1-3 seconds (parallel queries, lightweight metric arrays) |
| **Benchmark pull** (20-boat co-op) | 2-5 seconds |
| **Full co-op view refresh** | 3-10 seconds depending on co-op size and peer availability |

### Scaling limits

The fully decentralized benchmark model works well up to ~20 boats.
Beyond that:

- **20-50 boats**: still viable but benchmark refreshes may take 10-20
  seconds. Consider longer cache TTLs (48-72 hours).
- **50+ boats**: consider a designated aggregator (see resolved decision
  #15). The N×N query pattern becomes the bottleneck.
- **100+ boats**: unlikely for a single one-design fleet co-op. If needed,
  the aggregator model or sharded sub-co-ops would be required.

### Small fleet benchmark fragility

With exactly 4 boats in a condition bin (the minimum threshold):

- A single anomalous data point can skew percentiles significantly
- Benchmarks are marked with a confidence indicator based on sample size:
  `low` (4-6 boats), `medium` (7-12), `high` (13+)
- The UI displays "limited data" warnings for low-confidence bins
- Bins with fewer than 4 boats show no benchmark (not enough data)

---

## 22. Enforcement Classification

Policy obligations fall into three categories:

| Category | Meaning | Examples |
|---|---|---|
| **Technically enforced** | The protocol prevents violation — no human action required | Session visibility (API returns 403), coach access expiration (record has TTL), no-bulk-export (API doesn't support it), replay protection (nonce rejection), revocation (signature invalid after revocation) |
| **Socially enforced** | The protocol detects or deters violation, but cannot prevent it | Audit logging (admin sees anomalous access), screenshot accumulation (trust model), derivative works after expiry (agreement, not control), coach data deletion (normative obligation) |
| **Charter-enforced** | Violation is addressed through governance, not technology | No-protest-use (charter prohibition), no cross-co-op aggregation (coach agreement), dispute resolution (charter process), admin removal (member vote) |

The guides and data licensing policy reference these categories implicitly.
The distinction matters for setting accurate expectations: technically
enforced rules are guarantees; socially and charter-enforced rules depend
on community trust and governance.

---

## 23. Resolved Design Decisions

Decisions reached through PR review and external feedback:

1. **Tailscale is the transport layer for Phase 1-3.** The protocol is
   transport-agnostic (just signed HTTPS requests), but Tailscale provides
   free NAT traversal, E2E encryption, and stable addressing. The "cost of
   entry" for a boat to join a co-op includes a Tailscale node. If Tailscale
   becomes a problem, swapping to plain WireGuard or a relay is a transport
   change, not a protocol change.

2. **Multi-admin signing replaces single co-op key.** No single point of
   failure. Membership records require M-of-N admin boat signatures. The
   co-op survives the loss of any single Pi. See updated Sections 1 and 2.

3. **Active/inactive quorum based on heartbeat.** Boats without a heartbeat
   in 60+ days are excluded from the quorum denominator. Prevents winter
   haul-outs from deadlocking votes. See Section 3.1.

4. **Helmlog.org is a static directory + lightweight API gateway.** Static
   site on Cloudflare Pages (free), API gateway on Cloudflare Workers (free
   tier: 100K req/day). Gateway is a stateless router — maintains a registry
   of co-ops and boat endpoints in Cloudflare KV, proxies requests to Pis
   via Tailscale Funnel URLs. DDoS protection included. Registry entries
   authenticated via Ed25519 signed boat cards. Zero sailing data at rest.

5. **Mobile access via the gateway.** Crew members hit
   `helmlog.org/<co-op>/<boat>` in a browser. The Cloudflare Worker proxies
   to the Pi's Tailscale Funnel URL. The Pi handles auth via the existing
   magic-link flow. No app install, no Tailscale on the phone.

6. **Owner email required for co-op membership.** The boat card includes an
   `owner_email` field (required for co-op, optional for standalone). Enables
   out-of-band communication for votes, admin transfers, and emergencies.

7. **Clock skew tolerance for Pis without RTC.** Default 5-minute window
   relaxes to 20 minutes when NTP sync is stale. Peer clock slew on startup
   via heartbeat timestamps. See Section 4.

8. **Email is PII, admin-only visibility.** Owner and crew email addresses
   are personally identifiable information. Visible only to co-op admins,
   never exposed to other members. Scrubbed on departure. See data licensing
   policy Section 1.

9. **Single moderator mode.** Available for co-ops of any size (per data
   licensing policy). A single moderator with a designated backup instead
   of M-of-N multi-admin. Simpler for small fleets; larger fleets may
   also use it if the charter specifies it.
   Charter specifies which mode. See Section 1 above.

10. **Heartbeat with manual inactive toggle.** "On the water" vs "in the
    slip" is inferred from GPS fix recency by default. Boat owners can also
    manually set themselves inactive (for seasonal haul-outs, extended
    cruises, etc.) via a toggle in the web UI. The manual toggle prevents
    false-active from a Pi left running at the marina.

11. **Coach access is per-boat, not co-op-wide.** Each boat individually
    opts into sharing with a specific coach at the session level. The co-op
    admin does not control coach access. See Section 5 above.

12. **Fleet benchmarking is the primary co-op value proposition.** Anonymous
    aggregate statistics (Percentile Heatmap) let elite sailors see exactly
    where they stand without revealing who is faster. This is what makes the
    co-op worth joining. See Sections 3 and 5.

13. **Temporal sharing is co-op-level.** Embargo policies are set by the
    co-op (charter or majority vote), not per-boat. All members operate
    under the same delay rules. See Section 3 above.

14. **Session visibility defaults to event-scoped.** Members see full-detail
    data only from events they also participated in. Non-attended events
    show summary metrics only. This is the recommended default; co-ops can
    opt for full visibility in their charter.

15. **Decentralized benchmark computation with local caching.** Each Pi
    computes its own benchmarks by querying all peers (fully decentralized).

    Alternatives considered:

    | Approach | Pros | Cons |
    |---|---|---|
    | **Fully decentralized** (each Pi queries all peers) | No coordination role, no single point of failure, each boat sees fresh data, no trust required in an aggregator | N×N queries on a co-op of N boats; every Pi does the same aggregation work; more network traffic |
    | **Designated aggregator** (one Pi computes, distributes results) | One set of queries per refresh cycle, less network traffic, consistent results across all boats | Introduces a coordination role (who runs it?), aggregator sees all per-peer responses (privacy concern), single point of failure if aggregator is offline |

    **Decision: fully decentralized.** In a co-op of 10-20 boats, each Pi
    querying all peers for lightweight metric arrays is trivial network load.
    The aggregator model's efficiency gains don't justify the coordination
    complexity and privacy trade-off. If co-ops grow to 50+ boats, a
    designated aggregator could be reconsidered.

    Benchmark results are **cached locally** with a **co-op-configurable TTL**
    (charter field `benchmark_cache_ttl`, default: 24 hours). This avoids
    hammering peers on every dashboard load while keeping results reasonably
    fresh. The cache is invalidated when the boat uploads a new session or
    when the TTL expires. Boats can force a live refresh from the dashboard.

16. **Maneuver detection auto-calibrated from co-op data.** Rather than
    requiring each co-op charter to specify detection thresholds (heading
    change angle, BSP dip threshold, etc.), the platform auto-calibrates
    from the co-op's own data:

    - On first run, use conservative defaults (e.g., heading change >70°
      for a tack, >60° for a gybe)
    - After accumulating 20+ sessions in the co-op, compute fleet-specific
      thresholds from the distribution of heading change rates and BSP
      patterns during known maneuvers
    - Store calibrated thresholds in the co-op's charter metadata
    - Re-calibrate periodically (once per season or when the co-op votes
      to reset)

    This handles the J/105-vs-J/80 problem automatically — a co-op of
    heavy displacement boats will naturally produce different heading rate
    distributions than a co-op of sportboats, and the calibration adapts.

---

## 24. Open Questions

(Previously open questions 1-3 have been resolved — see Section 23 items
8-10. Questions 4-6 resolved — see items 15-16 above.)

No open questions remain. All design decisions have been resolved through
PR review feedback.
