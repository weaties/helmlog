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
└── boat.json         # { "pub": "<base64>", "sail_number": "69", "name": "Javelina" }
```

The `boat.json` is the **boat card** — a self-signed document that associates
the public key with human-readable metadata. It's freely shareable.

### Co-op identity

A co-op is created by an admin who generates a **co-op keypair**. The co-op's
public key is its identity. The private key is held by the current lead admin
(and can be transferred via a signed handoff message).

```
~/.helmlog/co-ops/<co-op-id>/
├── co-op.key         # Ed25519 private key (admin only, mode 0600)
├── co-op.pub         # Ed25519 public key
├── charter.json      # Signed charter metadata
└── members/          # Signed membership records
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

1. Generates co-op keypair
2. Creates and signs a **charter record**:
   ```json
   {
     "type": "charter",
     "co_op_pub": "<base64>",
     "name": "Puget Sound J/105",
     "area": ["Elliott Bay", "Central Puget Sound"],
     "created_at": "2026-04-01T00:00:00Z",
     "admin_boat_pub": "<base64>",
     "charter_url": "https://...",
     "sig": "<base64>"
   }
   ```
3. Signs their own membership record (self-admission)

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

A membership record is signed by the **co-op admin key** and proves that a
boat is an authorized member:

```json
{
  "type": "membership",
  "co_op_pub": "<base64>",
  "boat_pub": "<base64>",
  "sail_number": "69",
  "boat_name": "Javelina",
  "role": "member",
  "joined_at": "2026-04-15T00:00:00Z",
  "expires_at": null,
  "admin_sig": "<base64>"
}
```

Any Pi can verify this record using the co-op's public key — no network call
required.

### Revoking membership (departure or expulsion)

The admin signs a **revocation record**:

```json
{
  "type": "revocation",
  "co_op_pub": "<base64>",
  "boat_pub": "<base64>",
  "reason": "voluntary_departure",
  "effective_at": "2026-09-01T00:00:00Z",
  "grace_until": "2026-10-01T00:00:00Z",
  "admin_sig": "<base64>"
}
```

During the 30-day grace period, the departing boat's data is still accessible
but marked as pending departure. After `grace_until`, other Pis stop querying
that boat and purge any cached data.

### Admin transfer

A signed handoff message transfers the co-op private key to a new admin:

```json
{
  "type": "admin_transfer",
  "co_op_pub": "<base64>",
  "from_boat_pub": "<base64>",
  "to_boat_pub": "<base64>",
  "effective_at": "2027-04-01T00:00:00Z",
  "from_sig": "<base64>"
}
```

The new admin receives the co-op private key via a secure channel (in-person
USB transfer, or encrypted over Tailscale). The transfer record is distributed
to all members so they know who can sign new membership records.

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

### Audit

```
GET  /co-op/{co_op_id}/audit-log
     This boat's audit log of co-op data access events. Admin-only.
     Returns: who accessed what session, when, from which boat.
```

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
3. Checks that the timestamp is within 5 minutes of now (replay protection)
4. Checks that the boat holds a valid membership record for the requested
   co-op
5. Logs the access to the audit trail

No OAuth, no tokens to refresh, no central auth server. Just signatures.

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

## 14. Open Questions

1. **Tailscale dependency**: Should the federation protocol work without
   Tailscale (e.g., over plain WireGuard or even the public internet)?
   Tailscale is convenient but creates a vendor dependency.

2. **Co-op key custody**: The co-op private key is a single point of
   failure. If the admin's Pi dies, the co-op can't sign new memberships.
   Options: key escrow with a second admin, Shamir secret sharing among
   N members, or a re-keying protocol where members vote to accept a new
   co-op key.

3. **Offline voting**: If a proposal needs unanimous consent and one boat
   is offline for the winter, how long do you wait? The charter should
   define a voting window (e.g., 30 days) and quorum rules.

4. **Helmlog.org portal**: The path-based URL structure
   (`helmlog.org/<co-op>/<boat>`) implies a web portal. Should that portal
   be a thin proxy that routes to Pis over Tailscale, or a static site
   that links to each Pi's Tailscale Funnel URL? The former requires a
   server; the latter is just a directory.

5. **Mobile access**: Crew members without Tailscale access need a way to
   view co-op data. Tailscale Funnel exposes each Pi to the public internet
   — should the co-op view work over Funnel, or is Tailscale membership
   required for co-op features?
