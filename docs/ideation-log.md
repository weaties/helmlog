# Ideation Log

Half-baked ideas that aren't yet actionable enough for GitHub issues. Each entry
captures early thinking so it isn't lost. When an idea matures, it gets promoted
to one or more GitHub issues and its status changes to `promoted`.

## Statuses

| Status | Meaning |
|---|---|
| `raw` | Just captured, no validation or design work |
| `evolving` | Being discussed or refined across conversations |
| `superseded` | Replaced by a different approach (link the replacement) |
| `promoted` | Converted to GitHub issue(s) (link the issue numbers) |

---

## IDX-001: Cross-co-op discussion threads

- **Date captured:** 2026-03-13
- **Origin:** Discussion about threaded comments feature
- **Status:** `evolving`
- **Related:** `docs/data-licensing.md`, `docs/federation-design.md`, threaded comments feature, IDX-007

**Description:**
Discussion threads that span across co-ops (not just within a single co-op).
Would allow boats in different co-ops to have shared conversations. Deferred as
a separate phase/feature because it has data-licensing implications — co-op data
boundaries would need to be addressed. May require amendments to
data-licensing.md and federation-design.md.

**Notes:**
- *2026-03-13:* Initial capture. Data-licensing implications are the main blocker
  — cross-co-op threads would need to reconcile different co-ops' data policies.
- *2026-03-13:* IDX-007 explores a simpler approach inspired by Craig Mod's TGP —
  a constrained social feed rather than full threaded discussion in the federation
  protocol. May sidestep the data-licensing complexity by keeping the feed separate
  from co-op instrument data.

---

## IDX-002: Scalable plugin distribution

- **Date captured:** 2026-03-13
- **Origin:** Discussion about pluggable analysis/visualization
- **Status:** `raw`
- **Related:** analysis/visualization plugin system

**Description:**
Current plugin model (Python classes as PRs to the repo) works for early
adoption (a few boats, one co-op). When the platform grows to dozens of co-ops,
hundreds of boats, and multiple developers, will need a more scalable
distribution mechanism — possibly a package registry, marketplace, or separate
plugin repos. Don't solve now, but don't block evolution toward it.

**Notes:**
- *2026-03-13:* Initial capture. Current PR-based model is fine for now. Watch
  for signs that it's becoming a bottleneck.

---

## IDX-003: Notification channel expansion (SMS, WhatsApp, push)

- **Date captured:** 2026-03-13
- **Origin:** Discussion about comment notifications
- **Status:** `raw`
- **Related:** threaded comments feature, notification preferences

**Description:**
Platform launches with in-app indicators and email notifications. Future
channels include SMS (Twilio), WhatsApp (Business API), mobile push
notifications. Notification system is designed as channel-pluggable so
contributors can add channels without architectural changes. Each channel has
cost/complexity implications.

**Notes:**
- *2026-03-13:* Initial capture. Email + in-app is sufficient for launch. SMS
  and push are the most likely next channels.

---

## IDX-004: Custom JS visualization plugins

- **Date captured:** 2026-03-13
- **Origin:** Discussion about visualization architecture
- **Status:** `raw`
- **Related:** visualization plugin system

**Description:**
The baseline visualization plugin model uses Python-defined Plotly JSON specs.
For advanced use cases (3D boat models, custom canvas animations, novel
interactive widgets), a `CustomJSVisualization` plugin type could allow loading
custom JS bundles. Not needed early — the Plotly model covers sailing analysis
needs — but the plugin registry should not preclude this evolution.

**Notes:**
- *2026-03-13:* Initial capture. The plugin base class design should leave room
  for a JS subclass without requiring it now.

---

## IDX-005: Tuning guide auto-population from wind range

- **Date captured:** 2026-03-13
- **Origin:** Previous conversation about boat settings capture (referenced in memory)
- **Status:** `raw`
- **Related:** boat settings capture (#274, #275, #276), `src/helmlog/polar.py`

**Description:**
Pre-populate boat tuning settings (shroud tensions, halyard positions, etc.)
based on wind range using the boat's tuning guide. Would connect the boat
settings capture feature with polar/performance data. Identified as a separate
future feature during the boat settings design.

**Notes:**
- *2026-03-13:* Initial capture. Depends on boat settings capture being
  implemented first. Could leverage polar data to suggest settings for conditions.

---

## IDX-006: HelmLog platform-level discussion (GitHub Discussions)

- **Date captured:** 2026-03-13
- **Origin:** Discussion about threaded comments feature
- **Status:** `evolving`
- **Related:** threaded comments feature, GitHub repo, IDX-007

**Description:**
Platform-level discussion (not boat or co-op level) should live in the GitHub
Discussions repo rather than building custom infrastructure. This is for platform
community conversations, feature requests, etc. — distinct from the in-app race
discussion threads.

**Notes:**
- *2026-03-13:* Initial capture. GitHub Discussions is zero-cost and already
  integrated with the development workflow.
- *2026-03-13:* IDX-007 proposes a TGP-style constrained social feed that could
  serve this need with a more intentional, sailing-community-native UX. GitHub
  Discussions remains the right place for developer/contributor discussion; IDX-007
  would cover the broader community/social layer.

---

## IDX-007: TGP-style constrained social feed for the HelmLog community

- **Date captured:** 2026-03-13
- **Origin:** Craig Mod's "The Good Place" (craigmod.com/roden/102) — a members-only reverse-chron social feed with intentional constraints, built in ~10 hours with Claude Code
- **Status:** `raw`
- **Related:** IDX-001 (cross-co-op discussion), IDX-006 (platform discussion), federation, `auth.py`

**Description:**
A lightweight, constrained social feed for inter-co-op communication and HelmLog
platform discussion — inspired by Craig Mod's "The Good Place." Instead of
building full threaded comments into the federation protocol (IDX-001, complex
data-licensing implications) or defaulting to GitHub Discussions (IDX-006,
developer-facing), build a standalone social space with intentional constraints
that reflect sailing culture.

Craig Mod's design tenets and how they'd map to HelmLog:

| TGP | HelmLog adaptation |
|---|---|
| 2 posts/day limit | Similar — prevents firehose; encourages quality |
| 20 replies/day | Conversations are good, flooding isn't |
| Text-first, 1-bit inline photos | Fits Pi/low-bandwidth ethos; full color on click |
| Posts expire in 7 days (kept alive by replies) | Ephemeral by default — sailing is seasonal, keep it fresh |
| Single RSS feed | Async-first, no real-time pressure — fits offshore/intermittent connectivity |
| No follows/following | Natural scale cap via co-op membership |
| Links celebrated, with aggregation page | Sailing content curation — articles, weather, regattas |
| No read receipts, no real-time | Async matches the sailing rhythm |

**Key design questions:**
- **Auth:** Magic-link auth (already in `auth.py`) or co-op membership as access gate?
- **Hosting:** Centralized service vs. federated across boats? Centralized is simpler
  and avoids the data-licensing knots of IDX-001. Could run on a cheap VPS alongside
  the co-op registry.
- **Scope:** One feed per co-op? One global feed? Both? A single global feed with
  co-op tags might be simplest to start.
- **Data policy:** Since posts are text (not instrument data), they may sit outside
  the co-op data-licensing framework entirely. Ephemeral by default makes privacy
  simpler.
- **Relationship to in-app comments:** This is a *community* space, not race
  analysis discussion. Session-specific comments (IDX-001 territory) remain separate.

**Why this is exciting:**
Craig Mod's experience validates that constraints-as-features produce better
communities. The members-only, post-limited, ephemeral model naturally avoids
the failure modes of Discord/Slack/Twitter. And the implementation cost is
trivially small — Mod built TGP in 10 hours. HelmLog already has auth, FastAPI,
templates, and a community of boat owners who'd benefit from a shared space that
isn't a group chat.

**Notes:**
- *2026-03-13:* Initial capture. This could be the simplest path to both inter-co-op
  communication and platform community — sidesteps the federation protocol complexity
  of IDX-001 and the developer-facing nature of IDX-006. Start with a single global
  feed gated by any co-op membership. The 7-day expiry and post limits are the key
  insight — they make moderation almost unnecessary.

---

## IDX-008: Rich comment interactions — quote-reply and thread forking

- **Date captured:** 2026-03-13
- **Origin:** Conversation about comment/discussion UX patterns
- **Status:** `raw`
- **Related:** IDX-001 (cross-co-op discussion), IDX-007 (TGP-style feed)

**Description:**
Two interaction patterns for whatever comment/discussion system HelmLog builds:

1. **Quote-reply (highlight-to-quote):** Users should be able to select/highlight
   a passage from a previous comment and have it quoted inline in their reply.
   Common pattern in forums (Discourse, phpBB) and email clients. Provides
   conversational clarity — especially valuable when a comment touches multiple
   topics and the reply addresses only one. In a constrained feed (IDX-007),
   quote-reply becomes even more important because the post limit means you
   can't waste a post on "what do you mean by X?" — you quote the specific
   passage and respond substantively.

2. **Thread forking:** When a discussion drifts off-topic or a sub-point deserves
   its own conversation, allow forking into a separate thread. The fork should
   link back to the originating comment for context. Design questions: does a
   forked thread count against post limits? Does it inherit the parent's 7-day
   expiry clock or start fresh? In a session-specific context (race debrief),
   forking could split tactical discussion from boat-handling discussion.

**Notes:**
- *2026-03-13:* Initial capture. Both features depend on having a comment system
  first (IDX-001 or IDX-007). Quote-reply is straightforward UI — highlight text,
  click reply, selected text appears as blockquote. Thread forking has more design
  questions around how it interacts with constraints (post limits, expiry, scope).

---

## IDX-009: Self-hosted video upload with automatic track sync

- **Date captured:** 2026-03-13
- **Origin:** ChartedSails product announcement — direct video upload + auto-sync to sailing data
- **Status:** `raw`
- **Related:** `video.py`, `youtube.py`, `pipeline.py`, `insta360.py`, `cameras.py`, `session.js`

**Description:**
HelmLog's current video workflow is YouTube-centric: users upload to YouTube, then
link the URL with a manually-specified sync point (UTC time + video offset). This
works but has friction — you need a YouTube account, you need to figure out the
sync point, and the video lives on a third-party platform.

ChartedSails just shipped a compelling alternative: direct video upload to the
server with **automatic timestamp sync** from file metadata. Key features worth
studying:

1. **Multi-device upload** — coach films from RIB, sailors from boats, everyone
   uploads from their own phone. No more WhatsApp/Drive file shuffling.
2. **Automatic sync** — video file creation timestamp (EXIF/MP4 metadata) is used
   to align with track data. No manual sync point needed. Critical caveat: apps
   like WhatsApp and Messenger strip timestamp metadata, so users must upload from
   the original device or transfer via AirDrop/Drive.
3. **Click-to-play at any track point** — HelmLog already has this with YouTube
   embed, but self-hosted video could use `<video>` element directly, avoiding
   YouTube API complexity and privacy concerns.
4. **Instrument data overlay** — speed, heel, pitch displayed alongside video.
   HelmLog's session page already shows instrument data; the overlay is the
   visual integration step.
5. **Shareable via link** — coach, crew, class association can all see video +
   data together without needing YouTube access.

**Key design questions for HelmLog:**
- **Storage:** Pi has limited disk. Options: (a) store on Pi with aggressive
  retention/cleanup, (b) external storage (S3, Backblaze B2), (c) hybrid — Pi
  stores recent, offloads to cloud. ChartedSails offers 1000 min / 90-day
  retention as a baseline.
- **Upload path:** Web upload via browser? Mobile-friendly upload page? Does this
  need a mobile app or is a responsive web upload sufficient?
- **Coexistence with YouTube:** Should this replace the YouTube path or complement
  it? YouTube has unlimited free storage and reach; self-hosted has privacy and
  simplicity. Both probably have a place.
- **Co-op sharing:** If boat A uploads a video, can co-op members see it? Data
  licensing implications — video is PII-adjacent (faces, voices). May need
  explicit sharing consent separate from instrument data sharing.
- **Metadata extraction:** Need to reliably extract creation timestamp from
  MP4/MOV/MKV containers across different devices (iPhone, Android, GoPro,
  Insta360). `ffprobe` or `pymediainfo` can do this. The Insta360 module
  (`insta360.py`) already has some of this logic.

**Notes:**
- *2026-03-13:* Initial capture inspired by ChartedSails launch email. The
  auto-sync-from-metadata approach is the key insight — it eliminates the biggest
  friction point in HelmLog's current video workflow (manual sync point entry).
  The existing `insta360.py` metadata extraction and `video.py` sync-point model
  provide a foundation. Storage is the main architectural question — Pi disk is
  precious, but a 90-day / 1000-minute model with cloud offload could work.

---

## IDX-010: Agentic manual testing — Claude Code as a browser tester

- **Date captured:** 2026-03-13
- **Origin:** PR #262 (Playwright E2E scaffold) + Simon Willison's agentic manual testing patterns (simonwillison.net/guides/agentic-engineering-patterns/agentic-manual-testing/)
- **Status:** `raw`
- **Related:** PR #262 (Playwright infrastructure), `/tdd` skill, `tests/e2e/`

**Description:**
PR #262 scaffolds Playwright E2E testing for HelmLog — `playwright.config.ts`,
smoke tests, CI workflow. That gives us regression testing in CI. But Willison's
article describes a more ambitious pattern: agents using browser automation
*during development* to interactively test UI changes they've just made.

The core insight from Willison: "The defining characteristic of a coding agent is
that it can execute the code that it writes." For web UIs, this means the agent
should spin up the dev server, navigate the UI with Playwright, take screenshots,
verify behavior, and fix issues — all before committing. Key patterns:

1. **Agent-driven exploratory testing** — after making a UI change, Claude Code
   runs Playwright to navigate the affected pages, screenshots the result, and
   visually verifies the change looks correct. This catches CSS regressions,
   broken layouts, and rendering issues that unit tests miss.

2. **Documented test runs (Showboat pattern)** — Willison's `showboat` tool
   records manual testing as Markdown documents with embedded commands, outputs,
   and screenshots. HelmLog could adopt a similar pattern: after implementing a
   UI feature, Claude Code produces a test-run document proving the feature works.
   This is especially valuable for PR review — reviewers see screenshots, not
   just code diffs.

3. **Red-green TDD from manual findings** — when exploratory testing reveals an
   issue, the agent writes a failing test first (red), then fixes the code
   (green). This converts manual testing discoveries into permanent regression
   coverage. Complements the existing `/tdd` skill.

4. **API exercising via curl** — for non-UI changes, agents should `curl` API
   endpoints after implementation to verify behavior with real HTTP requests,
   not just `httpx.AsyncClient` in-process tests.

**What HelmLog already has:**
- PR #262's Playwright config with auto-start of the FastAPI server
- Screenshot-on-failure and trace retention
- The `/tdd` skill for test-driven development
- `httpx.AsyncClient` + `ASGITransport` for in-process API testing

**What's missing / design questions:**
- **Claude Code + Playwright integration:** Can Claude Code run `npx playwright
  test` mid-conversation and interpret results? Can it take ad-hoc screenshots
  via Playwright's API? The tooling exists (PR #262) but the workflow pattern
  isn't established.
- **Screenshot review:** Claude Code can read images. A workflow of "run
  Playwright → screenshot → read screenshot → verify" could work today.
- **Test-run documentation:** Should we adopt a showboat-like pattern for
  recording exploratory test sessions? Or is the PR description + screenshots
  sufficient?
- **Scope creep risk:** Agentic manual testing is powerful but slow. Need clear
  guidance on when to use it (UI changes, new pages) vs. when unit/integration
  tests suffice (API logic, data processing).

**Notes:**
- *2026-03-13:* Initial capture. PR #262 is currently open — once merged, the
  Playwright infrastructure is in place. The next step would be establishing a
  workflow pattern (possibly a new skill or extension of `/tdd`) for Claude Code
  to use Playwright interactively during UI development. Start simple: after any
  template/CSS/JS change, run the smoke suite and screenshot the affected page.

---

## IDX-011: Write derived data back to B&G network — live polar performance gauge

- **Date captured:** 2026-03-13
- **Origin:** Conversation about surfacing computed metrics on B&G instrument displays during racing
- **Status:** `raw`
- **Related:** `polar.py`, `sk_reader.py`, `can_reader.py`, `nmea2000.py`, IDX-005 (tuning auto-population)

**Description:**
HelmLog reads instrument data from the B&G network (via Signal K or direct CAN)
and stores it. But the data flow is one-directional — read only. The idea: compute
derived metrics in real time (e.g., percentage of as-sailed polar target) and
**write them back** to the NMEA 2000 / Signal K network so they appear on B&G
MFDs, Vulcan displays, or any other NMEA 2000 gauge on the boat.

The killer use case: a gauge on the helm display showing "you're at 94% of your
historical polar for this TWS/TWA" — not a theoretical polar from a design office,
but *your* boat's actual as-sailed performance baseline built from your own race
data (`polar.py`). The sailor sees immediately whether they're above or below
their personal benchmark without looking at a separate screen.

**Two write-back paths:**

1. **Signal K PUT/Delta API** — Signal K Server supports writing values back via
   its HTTP PUT or WebSocket delta interface. HelmLog could publish to a custom
   Signal K path like `performance.polarRatio` or use the standard
   `performance.polarSpeedRatio`. Signal K plugins (like the Instrument Display
   plugin or KIP) can render these on web-based displays. B&G integration
   depends on whether the Signal K → NMEA 2000 gateway is configured for
   bidirectional flow (via `signalk-to-nmea2000` plugin).

2. **Direct CAN bus write** — The Pi's CAN hat can transmit frames directly.
   `python-can` supports `bus.send(can.Message(...))`. Would need to encode
   a suitable PGN (possibly PGN 130578 Vessel Speed Performance or a
   manufacturer-proprietary PGN). This bypasses Signal K entirely but requires
   careful bus arbitration — writing garbage to a live NMEA 2000 bus during
   racing could cause real problems.

**Potential derived metrics to publish:**
- Polar performance ratio (BSP / polar target BSP for current TWS/TWA)
- VMG efficiency (actual VMG / optimal VMG)
- Target boat speed for current conditions
- Heel angle vs. optimal heel for the point of sail
- Performance trend (improving / degrading over last N minutes)

**Key design questions:**
- **Safety:** Writing to a live NMEA 2000 bus during racing is not something to
  get wrong. Bad data on a display could cause poor decisions. Need fail-safe
  behavior — if HelmLog loses confidence in its computation (stale wind data,
  insufficient polar baseline), it should stop publishing rather than show
  misleading numbers.
- **Signal K vs. direct CAN:** Signal K PUT is the cleaner path — it goes
  through the server's validation and plugin ecosystem. Direct CAN is more
  universal (works without Signal K) but riskier and harder to debug.
- **NMEA 2000 compliance:** Devices on an NMEA 2000 network need a unique
  source address and should follow the address claim protocol (PGN 60928).
  Just blasting frames without claiming an address is technically non-compliant
  and could conflict with other devices.
- **Latency:** Polar lookup needs to be fast enough for real-time display
  updates (~1 Hz). Current `lookup_polar()` queries SQLite — may need an
  in-memory cache of the polar table for the active session.
- **Display configuration:** Even if HelmLog publishes the data, the user still
  needs to configure a gauge on their B&G MFD to show it. For custom PGNs this
  may not be straightforward. Signal K web displays (KIP, Instrument Panel) are
  more flexible.
- **Which polar?** The as-sailed polar from `polar.py` is the most interesting
  (it's *your* boat), but it requires enough historical race data to be
  meaningful. Need a fallback for new boats with no baseline yet.

**Notes:**
- *2026-03-13:* Initial capture. This would be a game-changer for on-the-water
  use — HelmLog goes from a passive logger to an active performance instrument.
  Signal K PUT is probably the right starting path since HelmLog already connects
  via WebSocket. The `signalk-to-nmea2000` plugin can handle the gateway to B&G
  displays if configured. Start with polar performance ratio as the single
  metric — it's the most universally useful and the computation already exists
  in `polar.py`. IDX-005 (tuning auto-population) is complementary — together
  they form a feedback loop: historical data informs both setup and on-water
  performance tracking.
