# Helm Log — Federated Co-op Protocol Design

> Design document for decentralized data sharing between boats in a co-op,
> built on the existing Raspberry Pi + Tailscale + FastAPI architecture.

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
standalone use**.

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
     |       (web UI or CLI)               |
     |                                     |
     |  <-- signed membership record ---   |
     |                                     |
     |  Stores membership record locally   |
     |  Adds co-op pub to trusted list     |
     |                                     |
```

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
     Does NOT return private data (audio, notes, crew, sails).

GET  /co-op/{co_op_id}/sessions/{session_id}/track
     GPS track for a shared session. Returns position + instrument data
     at 1 Hz: lat, lon, bsp, tws, twa, hdg, cog, sog, aws, awa.

GET  /co-op/{co_op_id}/sessions/{session_id}/results
     Race results for a shared session (if results exist).
     Returns: [{boat, place, finish_time}].

GET  /co-op/{co_op_id}/sessions/{session_id}/polar
     Polar performance data for the session (BSP vs target at each TWS/TWA).
```

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
X-HelmLog-Sig: <base64>
```

The signature covers: `METHOD /path timestamp`. The receiving Pi:

1. Looks up the boat's public key by fingerprint
2. Verifies the signature
3. Checks that the timestamp is within the allowed window (replay protection)
4. Checks that the boat holds a valid membership record for the requested
   co-op
5. Logs the access to the audit trail

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

```
Admin grants coach temporary access
  → Admin signs a time-limited access record for the coach's key
  → Coach's device (laptop/phone) gets a keypair + access record
  → Coach queries member Pis directly over Tailscale
  → Each Pi verifies the access record signature + expiry
  → Access logged to audit trail
  → After expiry, Pis reject the coach's requests automatically
```

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
    action          TEXT NOT NULL,       -- session_list | track_fetch | current_fetch
    resource        TEXT,                -- e.g., session_id
    timestamp       TEXT NOT NULL,
    ip              TEXT
);

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
```

---

## 10. New Python Modules

```
src/logger/
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
│   ├── verify_request(pub_key, method, path, timestamp, sig) -> bool
│   └── CoOpPeer (dataclass for peer connection state)
│
├── co_op_api.py        # FastAPI router for /co-op/* endpoints
│   ├── router = APIRouter(prefix="/co-op")
│   ├── Middleware: verify_co_op_request (signature check)
│   ├── All endpoints from Section 3 above
│   └── Depends on federation.py + storage.py
│
└── co_op_client.py     # Client for querying other Pis
    ├── query_peer_sessions(peer, co_op_id, filters) -> list[dict]
    ├── fetch_track(peer, co_op_id, session_id) -> list[dict]
    ├── fetch_results(peer, co_op_id, session_id) -> list[dict]
    ├── fetch_currents(peer, co_op_id, area, time_range) -> list[dict]
    ├── poll_tombstones(peer, co_op_id, since) -> list[dict]
    ├── submit_vote(peer, co_op_id, proposal_id, vote) -> dict
    ├── discover_peers(co_op_id) -> list[CoOpPeer]
    └── aggregate_co_op_view(co_op_id, filters) -> dict
        # Queries all online peers in parallel, merges results,
        # applies cache, returns unified session list
```

---

## 11. Peer Discovery

How does a Pi find other co-op members on the Tailscale network?

**Option A: Tailscale API** (simplest)

Tailscale's local API (`/localapi/v0/status`) returns all peers on the
tailnet with their IPs and hostnames. Each Pi:

1. Lists Tailscale peers
2. Attempts `GET /co-op/identity` on each peer's IP (port 3002)
3. If the peer responds with a boat card, checks for shared co-op
   membership
4. Caches discovered peers in `co_op_peers` table

This happens on startup and periodically (every 10 minutes).

**Option B: Membership record exchange**

When the admin signs a membership record, it includes the new member's
Tailscale hostname (or IP). All members receive the full member list from
the admin. No discovery needed — you know exactly who to query.

**Recommendation:** Use Option B (explicit member list from admin) as the
primary mechanism, with Option A as a fallback for discovering peers whose
IPs have changed.

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
- Co-op view page in web UI

### Phase 3: Governance + voting
- Proposal creation and vote collection
- Agreement state management
- Pre-join disclosure endpoint

### Phase 4: Current models + advanced features
- Current observation sharing
- Aggregated current model computation
- Coach access records
- Peer caching (opt-in)

---

## 14. Resolved Design Decisions

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

---

## 15. Open Questions

1. **Email visibility within co-op**: Should member emails be visible to
   all co-op members, or admin-only? Charter should specify.

2. **Admin threshold for small co-ops**: With only 3 boats, 2-of-3 admin
   threshold means every admin is critical. Should the minimum co-op size
   for multi-admin be 4+ boats, with single-admin + backup key for 3-boat
   co-ops?

3. **Heartbeat granularity**: Should "on the water" vs "in the slip" be
   inferred from GPS fix recency, or should the boat owner explicitly set
   a status?
