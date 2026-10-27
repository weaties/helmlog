Helmlog on a web server

one of the largest limiations of Helmlog is it abillity to run only on the pi which restricts is reach to other users.  The solution is to run all the review (non-race) capabilities over the web by hosting Helmlog on a public web server.

Goals:
1. to allow other users tied to a boat to log in and review race data
2. to allow viedos to be streamed into helmlog on the webserver or on youtube
3. easy upload and syncronization of boat data from the pi to the web server
4. Web server should be hosted in a homeserver to start with then as load increases move to a paid web server in the cloud
5. setup script that makes setting up helmlog on the web easy.

Assumptions:
1. PI has a connection to the internet

---

## Decisions

Resolved through review discussion (2026-08-15):

1. **Multi-boat, not a single-boat mirror.** This is a hosted product other
   boats sign up for, not a personal backup a single owner points at their
   own server. Implies real multi-tenancy, not just an optional mirror.
2. **Sync reuses the existing boat identity, not a new protocol.** The
   co-op federation protocol already gives every boat an Ed25519 keypair and
   authenticated peer-to-peer data exchange (`peer_api.py`/`peer_client.py`,
   see `docs/federation-design.md`). The web server is built as another peer
   in that same protocol rather than a separate sync mechanism — one
   identity/auth model, not two.
3. **Auth: per-boat user tables, synced up like any other data** — not a
   combined central table (superseded 2026-08-15; see below). Each boat's
   existing `auth.py` user table rides along in the same one-way sync as
   decision 2, so the web server holds a *replica* per boat rather than
   owning identity itself. The Pi stays fully authoritative and works
   offline with zero dependency on the web server — this is what resolves
   the "does the Pi still authenticate locally" risk that the combined-table
   version had. Accepted trade-off: a person with access to more than one
   boat needs a separate login per boat — no cross-boat linking layer for
   v1. Minor caveat: a password changed on the Pi won't reach the web
   server until the next sync, so there's a brief window where the web
   server's copy is stale.
3a. **Boat selection is via URL, not a picker UI.** Each boat gets its own
   subdomain on the web server (mirroring the Pi's own tunnel subdomain
   pattern in `docs/https-deployment.md`, e.g. `<boat>.helmlog.org`) — the
   subdomain the user is on determines which boat's (synced) user table
   they're authenticating against. Avoids exposing a directory of boats and
   avoids building a boat-picker UI.
4. **Video is hosted on the web server for v1**, not deferred to
   YouTube-only. Likely the main cost driver behind goal 4's
   homelab-to-paid-cloud migration path.
5. **Sync is one-way: Pi -> web server only**, with one narrow exception —
   **reviewer notes write back to the Pi.** A note is additive (a new row,
   not an edit to existing state), so two people leaving separate notes on
   the same race never conflicts — it doesn't need general two-way sync,
   just a small one-directional write-back channel for notes specifically.
   Everything else stays one-way, avoiding the distributed write-conflict
   resolution that ruled out full two-way sync in the first place.
5a. **Race "clean up" (editing/trimming existing race data, e.g. the kind
   of thing the manual trim feature does) is out of scope for now** —
   dropped 2026-08-16 rather than resolving whether it needs to reach back
   to the Pi. If a reviewer's cleanup action doesn't need to reach the Pi
   at all, the web server's copy would simply diverge from the Pi's raw
   original — revisit if/when this comes back into scope.
6. **The web server is the public front door.** Not an addition alongside
   the Pi's own direct public exposure (Cloudflare Tunnel / Tailscale
   Funnel, per `docs/https-deployment.md`) — it's the primary point of
   public access.
7. **Architecture: a thin multi-tenant layer wrapping the existing Pi
   codebase, not a schema rewrite.** "Tables for each boat" means a
   **separate SQLite file per boat** (byte-for-byte the same schema the Pi
   already runs), not one shared table with a `boat_id` column — so
   `storage.py`, `auth.py`, and every route in `routes/*.py` are reused
   unchanged; they already assume one boat's DB per process via
   `get_storage(request)`. Genuinely new pieces: (a) a subdomain-resolution
   middleware backed by a small top-level boat registry (the *only*
   actually-shared table on the server), (b) a storage pool —
   `get_storage(request)` resolves to a lazily-opened, idle-evictable
   `Storage` instance per boat instead of one baked into `app.state`, (c)
   the sync-ingest endpoint (decision 8). Everything hardware- or
   background-loop-related in `main.py` (`sk_reader.py`, `can_reader.py`,
   `cameras.py`, `audio.py`, weather/tide/monitor/deploy loops) is dropped
   entirely — none of it is meaningful on a review-only server, and this
   lines up with the hardware-isolation boundary the codebase already has.
8. **Sync mechanism: incremental row-push through the federation peer
   protocol (option 2 of two considered).** The alternative — Litestream-
   style byte-level WAL replication — was rejected: it's less code, but
   replicates through an object-storage hop (a third infrastructure
   component) rather than boat-to-server directly, and it's one-directional
   at the byte level, so notes-write-back (decision 5) would need a
   completely separate channel regardless. Option 2 reuses the Ed25519
   signing/verification already in `peer_client.py`/`peer_api.py` — the new
   work is a per-table watermark/cursor ("send everything newer than X"),
   the ingest endpoint that applies incoming rows via `storage.py`'s own
   write methods, an initial full-copy bootstrap for new boats, and
   retry/resume handling — none of which exist yet, but all of it lives in
   one place and carries notes-write-back through the same channel for
   free, no new infrastructure.
9. **Payload scope: raw/source tables only — derived data is recomputed on
   the web server, not synced.** Telemetry (`positions`, `cogsog`, `winds`,
   `speeds`, `headings`, `depths`) and `races` sync; polar baselines,
   maneuver-detection cache, and debrief cache do not — the web server
   recomputes them locally from synced raw data, keeping sync payload size
   down. **Video and audio get their own transfer pipe/storage**, separate
   from the row-sync mechanism (decision 8) entirely — large binary files
   don't fit a "batch of changed rows" model.
10. **v1 syncs new rows only — no edit or delete propagation.** The
   watermark/cursor (decision 8) only ever asks "what's new," not "what
   changed" or "what was removed." Deliberately deferred rather than
   guessing: need customer feedback on whether edit/delete-sync is actually
   needed before committing to a design for it, rather than locking in
   behavior that's hard to unwind later. (Decision 5a's dropped race-cleanup
   scope and this are the same underlying call, made twice for different
   reasons.)
11. **Sync is on-demand only — no schedule, no automatic trigger.** Run
   manually so it happens when the boat actually has good connectivity and
   video files are accessible on the Pi's own storage, not on a timer that
   might fire mid-race or over a bad connection. UI: a **Sync button on the
   Pi's own web UI**, under the existing Network settings page
   (`/admin/network`, `routes/network.py`) — sync sits alongside the
   connectivity controls it depends on, not buried elsewhere.
12. **Resilience: resumable to completion, idempotent, no silent retry.**
   If the connection drops mid-sync, the sync must be resumable and must
   complete — not silently abandon partial progress. The ingest endpoint
   must be idempotent (upsert semantics, not blind insert) so a resumed or
   resent batch never creates duplicate records. If the web server is
   unreachable when sync is triggered, the Pi's UI surfaces a clear alert
   to retry once it's back online — no background queuing or automatic
   retry loop.
13. **GPS-time video sync is hosting-independent — confirmed by reading the
   existing code, not assumed.** `gopro.py`/`video.py` already matches a
   camera recording to a race session and persists the result (`race_videos
   .sync_utc`/`sync_offset_s`) entirely locally, before any upload —
   keyed off the video file's embedded metadata cross-referenced against
   HelmLog's own session records. Nothing downstream depends on the hosted
   copy retaining metadata, so **the YouTube-vs-self-host choice (decision
   4) has no bearing on GPS-sync capability either way.** Correction to
   earlier discussion: today's matching is a **manual, two-step CLI flow**
   (`gopro-match` suggests time-overlap candidates; a human supplies the
   final `sync_utc`/`sync_offset_s` via a separate `link-video` command) —
   not automated, and the GPS location tag it captures isn't currently used
   in the matching logic, only printed for a human to eyeball. Small,
   unrelated schema note surfaced along the way: `race_videos.youtube_url`
   is `NOT NULL` — self-hosting needs that column (or a new one) to
   accommodate a non-YouTube video reference.
14. **Direction (pending validation): automate sync for GPS-equipped
   cameras.** Some GoPros embed GPS-derived timestamps, some cameras don't.
   When a video has an embedded GPS location tag (`GoProVideo.gps_position`
   already captures this, no new probing needed), trust its `creation_time`
   as GPS-disciplined — the same time reference as HelmLog's own
   `storage.disciplined_now()` — and auto-link with `sync_offset_s = 0`
   directly, skipping manual offset-guessing entirely. Non-GPS cameras keep
   today's manual flow. Even in the GPS-trusted case, only auto-link when
   exactly one session overlaps the video's time window — an ambiguous
   match (e.g. two races the same day) still requires a human to confirm,
   since auto-linking to the *wrong* race is worse than not auto-linking.
   This is orthogonal to the hosting decision (it helps YouTube-hosted and
   self-hosted video equally) but emerged from evaluating decision 4.
   **Not yet trusted**: whether a GPS location tag being present reliably
   means `creation_time` is *also* GPS-corrected hasn't been confirmed for
   the actual cameras in use — a GPS-equipped camera could tag location
   from a fix while still running its file-level `creation_time` off an
   uncorrected internal clock. Needs validation against real footage before
   this is implemented — see the validation branch this spawned.
15. **GoPro ingest is Pi-side via USB card dongle — no Mac watcher, unlike
   Insta360.** User pulls the card and plugs it into the Pi directly. A
   Pi-side watcher (same shape as Insta360's `watch-exports.sh`, but running
   on the Pi instead of the Mac) detects new files on the mounted card and
   runs the lightweight probe from decision 14 (GPSU extraction, session
   matching) immediately — that step is metadata-only, no transcoding, so
   it's cheap enough to run on Pi hardware directly. The probed video (plus
   its `creation_utc`/`creation_source`/matched-session metadata) then goes
   to the web server over **decision 9's existing video/audio transfer
   pipe** — this is not a new sync mechanism, just another producer onto
   that pipe. Any heavier processing (transcoding, future compute-heavy
   work) happens **server-side**, where compute/GPU is available, following
   the same offload shape already established for Whisper transcription
   (`docs/transcription-offload.md`: Pi hands heavy work to a more capable
   machine over the network) — except the offload target is now the web
   server instead of a Mac worker. Publishing — self-hosted (decision 4)
   and/or republished to YouTube or another streaming service — also
   happens from the web server, not the Pi. End-to-end user experience:
   plug in the card, wait, then see video + data together on the website,
   auto-synced by GPS time with no manual step.
16. **Homelab hardware: 4th-gen Xeon, 32GB RAM, running Proxmox.** Sized
   against the app/sync/DB workload only — the video-serving component is
   kept architecturally separate (own volume/service, not bundled into the
   main app process, so it can move to object storage + CDN later without
   a rewrite), so this box doesn't need to carry video storage/bandwidth.
   Per-boat SQLite storage (KB-MB per race, tens of GB even at scale), an
   I/O-bound web app reused unchanged from the Pi codebase (decision 7),
   and on-demand-only sync bursts (decision 11, no continuous load) are
   comfortably covered by a small slice of this host — a couple vCPUs and
   a few GB RAM in a Proxmox guest, with the rest of the Xeon free for
   other use. LXC vs full VM for that guest is left as an implementation
   detail, not yet decided. If/when video hosting is brought into this
   same server's scope, this sizing would need revisiting.
17. **OS: Debian 12 (Bookworm). Web server: the same nginx-reverse-proxy →
   uvicorn/FastAPI pattern already running on the Pi** — no new stack.
   Raspberry Pi OS is Debian-based, so staying on Debian keeps
   `scripts/setup.sh`, `bootstrap-new-pi.md`, and the `/diagnose` runbook
   nearly 1:1 transferable instead of forking Ubuntu-specific ops docs.
   Subdomain routing (decision 3a) happens in a Python middleware layer
   inside the FastAPI app (decision 7), not in nginx — nginx's job stays
   simple: TLS termination + reverse proxy, same as today. This guest needs
   a much smaller install than the Pi: `uv` + Python deps + nginx + a
   systemd unit for the app — none of the Pi's Signal K / InfluxDB /
   Grafana / CAN-interface / audio pieces, which are hardware/
   background-loop code already excluded by decision 7.
18. **Reachability (for now): Tailscale, not Cloudflare Tunnel wildcard
   subdomains yet.** The Proxmox host joins the same tailnet already used
   for inter-boat MagicDNS (`scripts/setup.sh` step k.3) — reachable over
   the tailnet directly, or via Tailscale Funnel at
   `<hostname>.<tailnet>.ts.net` if public access is needed before it's
   locked down further. Explicitly staged: decision 3a's per-boat
   wildcard-subdomain scheme (`<boat>.helmlog.org` via Cloudflare Tunnel)
   is still the target once boats outside your own tailnet are being
   onboarded — Funnel only gives one hostname for the whole server, not
   per-boat subdomains, so it doesn't yet satisfy decision 3a for real
   multi-tenant use. Good enough while the server is still just you,
   building and testing. Revisit before onboarding outside boats.

## Open questions

Still to be resolved before this is spec-ready:

1. ~~Does the Pi still authenticate anyone locally, or does all auth depend
   on reaching the central server?~~ **Resolved by decision 3** (per-boat
   tables, synced as a replica) — the Pi stays fully authoritative and
   offline-capable.
2. ~~How does one person get access to more than one boat?~~ **Resolved by
   decision 3**: separate login per boat, accepted as-is for v1 — no
   cross-boat linking layer. User creation itself is otherwise unchanged
   from today — same per-boat `auth.py` invite flow, now also usable via
   the web server once synced.
3. ~~Is the web server read-only for review, or can reviewers write
   anything?~~ **Resolved by decision 5**: reviewers can leave notes
   (write back to the Pi). Race cleanup/editing is dropped from scope for
   now (decision 5a) rather than resolved either way.
4. ~~What would the architecture look like end to end?~~ **Resolved by
   decision 7**: per-boat SQLite files behind a thin multi-tenant routing
   layer, existing Pi codebase reused unchanged, hardware/background-loop
   code dropped entirely.
5. ~~What would the sync mechanism be, in detail?~~ **Resolved by decisions
   8-12**: incremental row-push via the federation peer protocol, raw
   tables only (derived data recomputed server-side), video/audio on a
   separate pipe, new-rows-only for v1, on-demand trigger via a button on
   `/admin/network`, and resumable/idempotent delivery with no silent
   retry on an unreachable server.
6. ~~What specs would the web server itself need (compute/storage/
   bandwidth)?~~ **Resolved by decision 16**: a 4th-gen Xeon / 32GB RAM
   Proxmox homelab host, sized against the app/sync/DB workload with video
   kept architecturally separate.
7. ~~On a homelab starting point, how is the server made reachable from the
   outside?~~ **Resolved by decision 18**: Tailscale for now (tailnet or
   Funnel), staged ahead of decision 3a's Cloudflare Tunnel wildcard
   subdomains once boats outside your own tailnet are being onboarded.
8. ~~Does GPS-location-tag presence reliably imply a GPS-disciplined
   `creation_time`, for the actual cameras in use?~~ **Resolved by the
   validation spike**: no — container `creation_time` was found off by 10
   years on a Hero 6. The GPMF stream's `GPSU` field is GPS-disciplined and
   safe to trust; `probe_video()` (`gopro.py`) now extracts it in
   preference to `creation_time`, tagging the source via `creation_source`
   (`"gpmf:GPSU"` vs `"ffprobe:creation_time"`) so decision 14's auto-link
   gate can check it directly.
