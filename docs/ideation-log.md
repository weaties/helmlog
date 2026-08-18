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
- **Status:** `superseded`
- **Related:** `docs/data-licensing.md`, `docs/federation-design.md`, threaded comments feature, IDX-007, **IDX-013**

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
- *2026-03-14:* Superseded by IDX-013, which captures a much more detailed and
  concrete vision for co-op discussion threads with race/time anchoring, @mentions,
  and identity requirements.

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
- *2026-03-14:* IDX-018 identifies a risk with the feed model — Julie Beck's research
  shows that scrollable feeds make even real relationships feel parasocial. The TGP
  feed's constraints (post limits, ephemeral content) partially mitigate this, but
  the feed should remain the casual/social layer, not the primary channel for race
  analysis discussion (that's IDX-013's role).

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

---

## IDX-012: GRIB wind forecasts for race-area weather prediction

- **Date captured:** 2026-03-13
- **Origin:** Conversation about using GRIB files to predict wind in the racing area
- **Status:** `raw`
- **Related:** `external.py` (Open-Meteo weather), IDX-005 (tuning auto-population), IDX-011 (write-back to B&G), `polar.py`

**Description:**
HelmLog currently fetches **current** weather observations via Open-Meteo
(`external.py`). The idea: download GRIB (GRIdded Binary) forecast files to
get **predicted** wind speed, direction, and shifts for the waters where the
boat will be racing. This is the data competitive sailors study before race day
— the same data that drives tools like PredictWind, Squid, and LuckGrib.

**Use cases:**
- **Pre-race planning:** Show forecast wind speed/direction over the race area
  for the next 6–48 hours. Overlay on a chart with the race course.
- **Shift prediction:** GRIB data at multiple forecast hours reveals when and
  how wind is expected to shift — the single most important tactical input for
  upwind racing. Show predicted shift timeline on the session page.
- **Tuning selection:** Combine forecast wind range with IDX-005 (tuning
  auto-population) — "tomorrow's forecast is 12–18 kts from 220°, here are
  your tuning settings for that range."
- **Post-race comparison:** Compare what GRIB predicted vs. what the boat
  actually measured. Over time this builds a model of how reliable the forecast
  is for your specific racing area (local effects, thermal patterns).
- **Live performance context:** If IDX-011 (write-back to B&G) is implemented,
  the forecast wind could be displayed alongside actual wind — "forecast said
  14 kts, you're seeing 11 kts, expect it to build."

**GRIB data sources:**
- **GFS (NOAA)** — Global, free, 0.25° resolution (~28 km), 3-hour intervals,
  384-hour forecasts. Accessible via NOMADS or Open-Meteo's GFS endpoint.
  Good enough for open-water racing.
- **HRRR (NOAA)** — US-only, free, 3 km resolution, hourly, 18-hour forecasts.
  Much better for coastal/bay racing (e.g., SF Bay, Chesapeake, Long Island
  Sound) where local topography drives wind patterns.
- **NAM (NOAA)** — North America, free, 3–12 km resolution. Good middle ground.
- **ECMWF (European)** — Generally the best global model, but commercial access
  is expensive. Some data available via Open-Meteo.
- **Open-Meteo forecast API** — Already integrated in `external.py`. Provides
  hourly forecasts from multiple models via a simple JSON API — no GRIB parsing
  needed. May be sufficient without raw GRIB files.

**Key design questions:**
- **GRIB vs. JSON API:** Raw GRIB files are large (tens of MB) and require
  parsing libraries (`cfgrib`, `eccodes`, `xarray`). Open-Meteo already wraps
  GFS/ECMWF into a JSON API that HelmLog uses — extending the existing
  `fetch_weather()` to pull hourly forecasts might be simpler than GRIB parsing.
  Raw GRIB only wins if you need custom interpolation or offline access.
- **Storage on Pi:** GRIB files are big. If fetching raw GRIB, need a retention
  policy. If using the JSON API, it's just a few KB per forecast fetch.
- **Racing area definition:** How does HelmLog know *where* you're racing? GPS
  position during the session is obvious, but for pre-race planning you'd need
  a configured "home waters" location or a way to set a race area.
- **Visualization:** Time-series wind chart? Wind barbs on a map? Simple table?
  The session page already has a track map — overlaying forecast wind arrows
  would be powerful but complex.
- **Offline access:** Boats may not have internet at the race course. Could
  download GRIB/forecast data while on wifi (at the dock, morning of race day)
  and cache locally for the day.

**Notes:**
- *2026-03-13:* Initial capture. The simplest first step might be extending
  `external.py`'s Open-Meteo integration to fetch multi-hour forecasts (not
  just current conditions) and displaying them on the session or home page.
  That avoids GRIB parsing entirely and gets 80% of the value. Raw GRIB is
  a later optimization for offline use or higher-resolution coastal models
  like HRRR. Connects well with IDX-005 (tuning from wind range) and IDX-011
  (write-back to displays).

---

## IDX-013: Co-op discussion threads with race/time anchoring and @mentions

- **Date captured:** 2026-03-14
- **Origin:** Conversation about reworking inter-co-op communication — building on and superseding IDX-001
- **Status:** `raw`
- **Related:** IDX-001 (superseded), IDX-007 (TGP feed — complementary), IDX-008 (quote-reply/forking UX), IDX-003 (notification channels), `docs/data-licensing.md`

**Description:**
Discussion threads that can be scoped to a single boat (private) or shared with
the entire co-op. Threads can optionally be anchored to a specific session, a
specific timestamp within a session, or a specific mark (start, leeward, windward,
finish). When anchored to a time or mark, a pin appears on the track replay for
every co-op boat that participated — at their own position for that moment or mark.

**Key design decisions (resolved):**

- **Visibility is co-op-configurable:** The co-op decides which visibility tiers
  are available. Options include boat-private, intra-boat (shared with specific
  boats — e.g., a coach and their boats), and co-op-wide. The co-op admin
  controls whether intra-boat threads are allowed or whether it's strictly
  boat-private vs co-op-wide. Thread creators choose from the tiers the co-op
  has enabled.
- **No anonymity:** All comments identify the commenter by boat name, crew member
  name, and position. This encourages accountability and constructive discussion.
  When a thread is anchored to a session, the commenter's position is resolved
  from that session's crew list — the same person may show as "tactician" in one
  race thread and "helm" in another if they switched roles between sessions.
- **Taggability opt-out:** Users can make themselves untaggable in co-op
  discussions (hidden from @mention autocomplete), but if they do, they cannot
  participate in co-op-visible threads. You're either in the conversation or not.
- **Protest firewall extends to threads:** Co-op discussion content is protected
  — cannot be exported or used in protest proceedings. Sailors must feel safe
  discussing tactics openly.

**@mention system:**

Supported mention formats with autocomplete:
- `@name` — mention a person by name (across all co-op boats)
- `@email` — mention by email
- `@boat` — mention an entire boat's crew
- `@boat/position` — mention whoever held that position in the anchored session
  (e.g., `@SailFast/tactician` resolves to the person who sailed tactician on
  SailFast in that race)
- `@boat/@name` — mention a specific person on a specific boat

Autocomplete populates from all co-op members who have not opted out of
taggability. When a thread is anchored to a session, position-based mentions
resolve against that session's crew list.

**Race/time anchoring:**

- **Session-level:** Thread is about a whole race or practice session
- **Timestamp-level:** Thread is pinned to a specific UTC timestamp. On the
  track replay, each co-op boat sees the pin at their own GPS position at that
  time. Use case: "at 14:32, the wind shifted — what did everyone do?"
- **Mark-level:** Thread is pinned to a mark (start, windward, leeward, finish).
  Each boat sees the pin at their own time of rounding that mark, regardless of
  when they arrived. Use case: "what happened at the leeward mark in race 3?"

**Notifications:**

Delivered based on user notification preferences (ties into IDX-003). @mentions
trigger notifications; thread updates may optionally notify participants.

**Crew visibility rules:**

Co-ops can configure crew visibility in threads:
- Which crew details are visible to other boats (name, position, both, neither)
- Whether crew from other boats can be @mentioned by name or only by position
- These are co-op-level policies, not per-user choices — the co-op admin sets
  the rules and they apply uniformly

**Coach role in threads:**

Coaches need special consideration:
- A coach may work with multiple boats in the same co-op — they need to see
  and participate in threads across their boats without being "crew" on any
- Coaches may run intra-boat threads with a subset of their boats (e.g., a
  coaching group within a larger co-op) — this is a key use case for the
  intra-boat visibility tier
- Should coaches have a distinct role in the thread system? Or is "coach" just
  a crew position that happens to span boats?
- Coach access to boat-private threads: does the boat owner grant access, or
  does the co-op admin? Likely the boat owner — it's their data
- Coaches may want to start threads that are visible only to their coached boats,
  not the whole co-op — another argument for the intra-boat tier

**Open design questions:**

- How are marks identified? Need a mark model (start/finish line, windward mark,
  leeward mark, gate) tied to sessions. This may depend on race course modeling
  which doesn't exist yet.
- Thread editing/deletion — can you edit or delete your own comments? Time limit?
- Moderation — who can delete threads or ban users from co-op discussions? Co-op
  admin? Any boat owner?
- Media in threads — text only? Or allow images (e.g., screenshot of track)?
  Images have storage/PII implications.
- How does this interact with IDX-007 (TGP-style feed)? Is TGP the casual social
  layer and this is the race-analysis layer? Or does this subsume TGP?
- Federation: are threads stored centrally (co-op server) or replicated across
  boats? Central is simpler but requires connectivity.
- How does the coach role map to the existing auth/identity model? Is "coach" a
  co-op-level role, a boat-level role, or both?
- Can a coach be in multiple co-ops (coaching different fleets)? How does that
  interact with thread visibility?

**Notes:**
- *2026-03-14:* Initial capture. Supersedes IDX-001 which was a vague sketch.
  The key new insights are: configurable visibility tiers, mandatory identity
  (no anonymity), taggability as participation gate, mark-level anchoring (not
  just timestamps), and extending the protest firewall to discussion content.
  The @mention system with position-based resolution against session crew lists
  is particularly powerful for post-race debrief across a fleet.
- *2026-03-14:* Added co-op-configurable visibility rules (co-op decides whether
  intra-boat threads are allowed), crew visibility policies, and coach role
  considerations. Coaches are a key use case for the intra-boat tier — they
  work across boats and need thread access patterns that don't map cleanly to
  "crew on one boat." Open questions added around coach identity model.
- *2026-03-14:* IDX-018 (anti-parasocial design principles, from Julie Beck's
  Atlantic article) validates this design direction. Mark-anchoring and position-based
  @mentions are inherently anti-parasocial — they ground discussion in shared physical
  experience. Key new consideration: make the track replay the primary entry point for
  threads, not a separate "discussions" tab. Format should feel like conversation at
  the yacht club bar, not a social media feed.

---

## IDX-014: Structured feature specs with decision tables and state diagrams

- **Date captured:** 2026-03-14
- **Origin:** "Future of Software Engineering" retreat article — spec-driven development section; EARS, state machines, decision tables rediscovered as precision tools for AI agents
- **Status:** `promoted`
- **Promoted to:** `/spec` skill in `.claude/skills/spec/SKILL.md`, "Structured Specs" section in `CLAUDE.md`
- **Related:** CLAUDE.md (coding conventions), `/tdd` skill, GitHub issues

**Description:**
The retreat found that traditional user stories are too vague for AI-driven development —
"bad specs produce bad code at scale." Teams are adopting structured specification
formats (EARS syntax, state machines, decision tables) because they give agents enough
precision to produce correct implementations.

HelmLog already has strong conventions in CLAUDE.md and the `/tdd` skill drives
test-first implementation. But feature specifications — the descriptions in GitHub issues
and conversations that kick off work — are still free-form prose. For complex features
(federation lifecycle, embargo enforcement, co-op thread visibility tiers), this means the
human and agent spend significant conversation turns disambiguating requirements that
could have been precise from the start.

**Proposed improvements:**

1. **Decision tables for policy-heavy features:** Features like data licensing, embargo
   rules, and thread visibility have combinatorial logic (role × visibility tier × co-op
   policy → allowed/denied). A decision table in the issue body makes every combination
   explicit. The agent can generate tests directly from the table rows.

2. **State diagrams for lifecycle features:** Co-op membership, session lifecycle, and
   embargo transitions are state machines. Drawing the states and transitions up front
   (even as ASCII art or Mermaid in the issue) prevents the "what happens if X then Y?"
   back-and-forth during implementation.

3. **EARS-style requirements for hardware interfaces:** For features like IDX-011
   (write-back to B&G), structured requirements like "WHEN polar confidence < 0.5
   THE SYSTEM SHALL stop publishing to Signal K" are unambiguous and directly testable.

4. **Spec review before code:** The retreat's insight — "pre-review the plans,
   post-review the engineering." For complex features, the human reviews and approves
   the spec (decision table, state diagram, EARS requirements) before the agent writes
   any code. This catches misunderstandings at the cheapest point.

**What this is NOT:**
- Not heavyweight documentation or BDUF (big design up front)
- Not required for simple bug fixes or small features
- A lightweight structured format for the ~20% of features where ambiguity costs the most

**Notes:**
- *2026-03-14:* Initial capture. The `/tdd` skill already produces test-first implementations,
  but the specs that inform the tests are still conversational. Adding a structured spec step
  before TDD would be: spec → review spec → write tests from spec → implement. The spec
  format should be as lightweight as possible — a decision table can be a Markdown table in
  the issue body, a state diagram can be Mermaid or ASCII. The goal is precision, not ceremony.

---

## IDX-015: Sailing domain ontology as agent grounding layer

- **Date captured:** 2026-03-14
- **Origin:** "Future of Software Engineering" retreat article — knowledge graphs and semantic layers as grounding for domain-aware agents
- **Status:** `promoted`
- **Promoted to:** #350 (`/domain` skill — Signal K and NMEA 2000 reference as agent grounding layer)
- **Related:** `nmea2000.py` (PGN dataclasses), `polar.py`, `sk_reader.py`, `races.py`, CLAUDE.md

**Description:**
The retreat found that domain ontologies — formal models of concepts and relationships
within a business domain — are suddenly relevant as grounding layers for AI agents. A
large telecom captured its entire domain in ~286 concepts. The practical value: agents
with a domain ontology make fewer mistakes because they understand how concepts
relate, not just what code does.

HelmLog operates in a rich domain (sailing, racing, instrument systems, weather,
federation) that has specific terminology, physical relationships, and conventions that
general-purpose LLMs only partially understand. Examples of domain knowledge that
trips up or slows down agent work:

- **Instrument relationships:** TWA (true wind angle) is derived from AWA (apparent
  wind angle) + BSP (boat speed). Changing one affects the others. The agent needs
  to understand these physical relationships to reason about data correctness.
- **Racing concepts:** A "mark rounding" is not just a GPS waypoint — it has rules
  (port/starboard, proper course, room). "VMG" means different things upwind vs.
  downwind. Polars are functions of TWS × TWA → BSP.
- **Signal K paths:** `navigation.speedThroughWater` vs. `navigation.speedOverGround`
  — the difference matters for polar calculations and the agent needs to know which
  to use when.
- **NMEA 2000 conventions:** PGN numbers, update rates, source addresses, device
  priorities — the CAN bus has its own conceptual model.
- **Racing customs:** Protest rules, co-op etiquette, coach-sailor relationships —
  these inform feature design (protest firewall, crew visibility policies).

**Proposed approach:**
A `docs/domain-model.md` file that captures the sailing/instrument domain as a
structured reference. Not a formal OWL ontology — a readable document organized by
concept cluster (instruments, racing, performance, weather, federation) with
relationships made explicit. The agent reads this when working on domain-sensitive
features. CLAUDE.md would point to it.

**Why a doc and not just CLAUDE.md?**
CLAUDE.md is about *how to work in this codebase*. The domain model is about *what
this codebase models*. Keeping them separate lets the domain model grow without
bloating CLAUDE.md, and makes it referenceable from issues, specs, and discussions.

**Notes:**
- *2026-03-14:* Initial capture. Start small — the instrument relationship cluster (TWS,
  TWA, AWA, BSP, SOG, COG, VMG, heel, leeway and how they relate) would pay for
  itself immediately in any work touching `polar.py`, `sk_reader.py`, or `nmea2000.py`.
  The retreat's observation that a large telecom's domain fit in ~286 concepts is
  encouraging — sailing instrumentation is a much smaller domain. Could potentially
  auto-generate the initial draft from the existing dataclasses in `nmea2000.py` and
  Signal K path mappings in `sk_reader.py`.
- *2026-03-18:* Promoted to #350 as part of the skill improvement initiative (#355).
  Part of a 6-issue series informed by Thariq's skill categories article and the
  Improving Skill Creator blog post.

---

## IDX-016: Risk-tiered verification for HelmLog modules

- **Date captured:** 2026-03-14
- **Origin:** "Future of Software Engineering" retreat article — risk mapping as the new core engineering discipline; verification proportional to blast radius
- **Status:** `promoted`
- **Promoted to:** "Risk Tiers" section in `CLAUDE.md`, tier-aware `/pr-checklist` skill
- **Related:** CLAUDE.md (testing strategy), `/tdd` skill, `/pr-checklist` skill, `/data-license` skill

**Description:**
The retreat reframed code review from "did someone review this?" to "what is the blast
radius if this is wrong, and is our verification proportional to that risk?" Not all code
carries the same risk. HelmLog has modules that range from safety-critical (writing to
a live NMEA 2000 bus during racing) to low-risk (CSS tweaks on the history page).

Currently, all HelmLog code goes through the same verification: TDD, ruff, mypy,
`/pr-checklist`. This is already good, but as the codebase grows — especially with
features like IDX-011 (write-back to B&G) and federation — we should be explicit about
which modules demand extra rigor and which can move fast with standard checks.

**Proposed risk tiers for HelmLog:**

| Tier | Modules | Blast radius | Verification |
|---|---|---|---|
| **Critical** | `can_reader.py` (CAN write-back), `peer_auth.py`, `federation.py`, `auth.py`, `storage.py` (migrations) | Data loss, safety (bad data on displays during racing), security (auth bypass), data corruption | TDD + integration tests + manual review of spec + data-license review |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py` | Incorrect data capture, broken federation, PII exposure | TDD + integration tests where applicable |
| **Standard** | `web.py`, `polar.py`, `external.py`, `races.py`, `triggers.py` | Wrong numbers on screen, broken features | TDD + standard PR checklist |
| **Low** | Templates, CSS, JS, docs, config | Visual issues, non-functional | Smoke test / visual check |

**How this would work in practice:**
- CLAUDE.md or a separate `docs/risk-tiers.md` documents the tier assignments
- The `/pr-checklist` skill checks which files were touched and flags the appropriate
  tier's verification requirements
- The agent (and human) can skip heavyweight review for low-tier changes and focus
  review energy where it matters most
- Tier assignments are reviewed when modules change scope (e.g., if `can_reader.py`
  gains write capability per IDX-011, it moves to Critical)

**Notes:**
- *2026-03-14:* Initial capture. The immediate value is in the `/pr-checklist` skill —
  it currently runs the same checks for all PRs. Making it tier-aware would mean a
  CSS-only PR gets a quick pass while a federation change triggers integration tests
  and data-license review automatically. The tier assignments themselves are a form of
  institutional knowledge that helps both human and agent calibrate effort.

---

## IDX-017: Architecture comprehension sessions — fighting cognitive debt

- **Date captured:** 2026-03-14
- **Origin:** "Future of Software Engineering" retreat article — cognitive debt (gap between system complexity and human understanding), continuous comprehension, "agent subconscious"
- **Status:** `promoted` (components 1–3)
- **Promoted to:** #352 (`/architecture` skill — codebase comprehension and complexity tracking). Component 4 (decision archaeology in memory) remains a habit change, not a skill.
- **Related:** CLAUDE.md, memory system (`.claude/projects/`), IDX-015 (domain ontology)

**Description:**
The retreat's concept of "cognitive debt" — the gap between how complex the system
actually is and how well the human understands it — is directly relevant to HelmLog.
The codebase is growing (federation, co-op, peer API, data licensing, threading, cameras,
pipelines) and the primary developer's mental model can fall behind, especially when
significant implementation is done by the agent.

The retreat identified this risk specifically: "code review has historically served as a
learning mechanism as much as a quality gate. Mentorship, shared understanding and
codebase familiarity all happened through review. Losing that channel without replacing
it creates a comprehension gap that compounds over time."

**Proposed approach — periodic comprehension sessions:**

1. **Architecture snapshot skill (`/architecture`):** A new skill where the agent reads
   the current codebase and produces a concise system overview: module dependency
   graph, data flow paths, recent structural changes, areas of growing complexity.
   Not documentation for its own sake — a comprehension tool for the human to
   quickly re-orient after time away or after the agent has made significant changes.

2. **"What changed while I was away" briefing:** When starting a new conversation
   after a gap, the agent reviews recent git history and memory to produce a
   briefing: what was implemented, what architectural decisions were made, what's
   different from last time. This already happens informally through memory — making
   it an explicit workflow would be more reliable.

3. **Complexity hotspot detection:** The agent periodically scans for modules that
   have grown beyond the ~200 line convention, functions with high cyclomatic
   complexity, or areas where multiple recent PRs have clustered (suggesting the
   module is becoming a kitchen sink). Flags these proactively.

4. **Decision archaeology in memory:** The retreat's "latent knowledge" concept maps
   to our memory system. Currently memories capture facts and preferences. We could
   be more intentional about capturing *why* decisions were made — the reasoning
   behind architectural choices, rejected alternatives, and trade-offs. This is the
   "agent subconscious" that helps future conversations avoid re-litigating settled
   questions.

**Connection to the "middle loop":**
The retreat identified a new category of supervisory engineering work — "directing,
evaluating, and fixing the output of AI agents" that requires "strong mental models of
system architecture" and the ability to "rapidly assess output quality without reading
every line." Architecture comprehension sessions are how the human maintains the
mental model needed to be an effective middle-loop supervisor.

**Notes:**
- *2026-03-14:* Initial capture. The lowest-effort, highest-value starting point is #4 —
  being more intentional about capturing decision reasoning in memory. This costs
  nothing extra (just a habit change in how we write memories) and pays dividends
  immediately. The `/architecture` skill is higher effort but would be valuable before
  major features or after returning from a break. Complexity hotspot detection could
  be a simple addition to `/pr-checklist` — flag if any touched file exceeds 200 lines.
- *2026-03-18:* Components 1–3 (architecture snapshot, delta briefing, complexity
  hotspots) promoted to #352 as part of the skill improvement initiative (#355).
  Component 4 (decision archaeology in memory) is a habit change, not a skill —
  remains as guidance in this entry.

---

## IDX-018: Anti-parasocial design principles for co-op communication

- **Date captured:** 2026-03-14
- **Origin:** Julie Beck, "When Real Relationships Start to Look Parasocial" (*The Atlantic*, Oct 2025) — how social media formats make real friendships feel like content to consume
- **Status:** `raw`
- **Related:** IDX-007 (TGP feed), IDX-008 (quote-reply/forking), IDX-013 (co-op threads), `docs/data-licensing.md`

**Description:**
Not a feature — a set of design principles that should govern how HelmLog builds
communication features (IDX-007, IDX-013). Beck's article argues that the *format*
of online communication — scrollable feeds, broadcast posting, passive consumption —
rewires how people perceive even real friends, making genuine relationships feel
parasocial. The key insight: when you consume a friend's update in the same format
as an influencer's post, your brain processes both the same way.

HelmLog's co-op communication should be deliberately designed to resist this effect.
Sailing teams need genuine vulnerability and reciprocity in post-race debrief —
the opposite of performative broadcasting.

**Principles (derived from Beck + existing HelmLog design):**

1. **Address, don't broadcast.** Communication should be directed at specific
   people or about specific events, not posted to "the feed." IDX-013's @mentions
   and mark-anchoring already do this — a comment pinned to the leeward mark in
   Race 3 and addressed to `@SailFast/tactician` is a conversation, not content.
   IDX-007's global feed is more vulnerable to the broadcast trap.

2. **Anchor to shared experience, not timeline.** Beck identifies the scrollable
   timeline as the format that makes friends look like influencers. HelmLog's
   natural anchor is the *race* — threads pinned to tracks, marks, and timestamps
   mean the context is a shared sailing experience, not a content feed. Design
   should lean hard into this: enter discussions from the track replay, not a
   notification inbox.

3. **Require engagement, not consumption.** Passive scrolling is the core
   parasocial behavior. Design affordances that make participation the default:
   - Quote-reply (IDX-008) forces engagement with specific content
   - Post limits (IDX-007) make each post cost something, discouraging broadcast-style updates
   - No "like" button — if you agree, say why; if you appreciate, quote and respond
   - Consider: threads only visible to participants + mentioned users, not browsable by the whole co-op?

4. **Reciprocity over audience.** Beck's "imagined audience" concept: when people
   post, they think of *an audience*, not specific people. Counter this by making
   the recipient explicit. IDX-013's visibility tiers help — a boat-private thread
   between a coach and their boats is a conversation, not a broadcast. IDX-007's
   global feed could add a "reply expected" convention or norm.

5. **Identity enables vulnerability.** Beck notes that self-disclosure collapses
   when people assume they know each other from posts. IDX-013's mandatory identity
   (boat + crew + position) and protest firewall create the conditions for genuine
   self-disclosure: you know who's talking, and what's said can't be used against
   you in protest. This is the opposite of anonymous social media — full identity
   with full protection.

6. **Format should feel like conversation, not content.** The visual and
   interaction design should not resemble a social media feed. No infinite scroll,
   no algorithmic ordering, no engagement metrics. Threads anchored to marks on
   a track chart feel like standing around the yacht club bar pointing at the
   race course, not scrolling Instagram.

**Implications for existing ideas:**

- **IDX-007 (TGP feed):** The feed model is inherently more susceptible to the
  parasocial trap than IDX-013's anchored threads. If we build both, the TGP feed
  should be the casual/social layer (and its constraints — post limits, ephemeral
  content — partially mitigate the risk), while IDX-013 threads are the race-analysis
  layer where genuine discussion happens. The TGP feed should not become the primary
  communication channel for co-op members.

- **IDX-013 (co-op threads):** Already well-aligned with these principles. The
  mark-anchoring and position-based @mentions are inherently anti-parasocial because
  they ground communication in shared physical experience. Consider making the track
  replay the primary entry point for threads, not a separate "discussions" tab.

- **IDX-008 (quote-reply):** Quote-reply is a key anti-parasocial affordance — it
  forces engagement with specific content rather than passive consumption. Prioritize
  this in the MVP of whichever communication system ships first.

**Notes:**
- *2026-03-14:* Initial capture. The Beck article validates design instincts already
  present in IDX-013 (mandatory identity, protest firewall, mark-anchoring) and adds
  a theoretical framework for *why* these constraints matter. The biggest actionable
  insight is principle #6 — the visual/interaction design should not resemble a social
  feed. Entering co-op discussions from the track replay (not a notification timeline)
  is a concrete way to enforce this.
- *2026-03-14:* Jason Kottke's "Just Enough Social" member profiles
  (kottke.org/26/03/people-in-this-neighborhood) provide a concrete reference
  implementation. Key pattern: **graduated disclosure** — display names are public,
  bios visible only to members, full profile visible only to the owner. Users control
  their exposure level at each tier. This maps well to IDX-013's co-op visibility
  rules but introduces a useful tension: Kottke embraces "no one knows you're a dog"
  anonymity (members choose how much to reveal), while IDX-013 mandates full identity
  (boat + crew + position) for accountability. Both are valid — they serve different
  trust models. For HelmLog, the resolution may be: **mandatory boat identity, optional
  personal disclosure.** Your boat and position are always visible (the co-op needs
  to know who's talking about the leeward mark rounding), but personal details
  (real name, email, bio) follow Kottke's graduated model — you control what's shared
  beyond the role. This preserves IDX-013's accountability ("@SailFast/tactician said
  this") while giving individuals control over personal exposure. Also reinforces the
  broader "Just Enough Social" label as a design philosophy for all co-op communication
  features — build the minimum social surface that enables genuine collaboration, resist
  the pull toward full social platform features.

---

## IDX-019: In-app bug reports and feature requests → GitHub Issues

- **Date captured:** 2026-03-14
- **Origin:** Conversation about keeping user feedback in context with what they're seeing in the app
- **Status:** `promoted`
- **Promoted to:** #369
- **Related:** GitHub Issues, `web.py`, `auth.py`

**Description:**
Add a lightweight affordance in the HelmLog web UI where any user can report a
bug or request a feature. The submission creates a GitHub issue in the helmlog
repo directly — no intermediate storage, no thread infrastructure, no triage
queue to build. GitHub Issues *is* the triage system.

**How it works:**
- Small "Report issue" or "Feedback" button in the nav or page footer
- Minimal form: category toggle (bug/feature), free-text description
- On submit, the backend calls `gh issue create` (or the GitHub API) with:
  - Title from the user's description (or a generated summary)
  - Body pre-populated with context: current page URL, session/race ID if
    applicable, user identity, browser/device info
  - Labels: `bug` or `feature-request`, plus `from-app`
  - The user sees a confirmation with a link to the created issue
- Admins and developers triage in GitHub as usual — no parallel system to manage

**Why this is better than thread-backed or custom feedback tables:**
- Zero new infrastructure — GitHub Issues already exists, already has labels,
  milestones, assignment, notifications, and search
- Developers are already in GitHub — feedback lands where work is tracked
- Users get a link back to their issue and can follow progress
- No need to wait for IDX-013 thread infra to be built

**Open questions:**
- Authentication for GitHub API: use a bot token stored server-side, or the
  GitHub App installation token? Bot token is simplest
- Rate limiting: should the app throttle issue creation to prevent spam?
  Probably yes — one issue per user per minute is reasonable
- Should the created issue include a screenshot? Future enhancement — start
  with text only

**Notes:**
- *2026-03-14:* Initial capture. The simplest viable approach: a form in the
  app that creates a GitHub issue with contextual metadata. Avoids building any
  custom feedback infrastructure. Can always add richer features (screenshots,
  thread discussion, linking to IDX entries) later.

---

## IDX-020: Supply-chain hardening — defending the data-license from maintainer takeover

- **Date captured:** 2026-03-15
- **Origin:** Conversation about hardening the platform against a contributor who gains merge authority and introduces code that violates the data-licensing policy
- **Status:** `raw`
- **Related:** `docs/data-licensing.md`, IDX-016 (risk tiers), `auth.py`, `peer_auth.py`, `federation.py`, `export.py`, `peer_api.py`

**Description:**
HelmLog's data-licensing policy (`docs/data-licensing.md`) is the social contract
with boat owners: you own your data, PII is protected, co-op data is view-only,
no gambling, no protest exports, biometrics require consent. The policy is
enforced by code — but what if someone contributes code that *silently erodes*
those guarantees?

The threat model: an attacker (or well-meaning but misaligned contributor)
gradually builds trust through legitimate contributions, earns merge rights,
and then introduces changes that:

1. **Exfiltrate data** — add a telemetry endpoint, analytics SDK, or "debug"
   logging that phones home with instrument data, PII, or co-op session data
2. **Weaken access controls** — relax embargo checks, widen the field allowlist
   in co-op API responses, remove audit logging, soften rate limits
3. **Enable bulk export of co-op data** — violating the "view-only" constraint
   by adding batch endpoints, pagination that allows full scraping, or export
   formats for other boats' data
4. **Bypass consent mechanisms** — auto-enable biometric collection, remove
   per-person consent gates, share diarized transcripts without opt-in
5. **Add gambling/protest affordances** — features that facilitate wagering on
   race data or produce co-op data exports formatted for protest committees
6. **Dependency poisoning** — add or update a dependency that itself exfiltrates
   data, introduces a backdoor, or changes behavior post-install

**Possible defenses (layered, not all-or-nothing):**

### Layer 1: Static policy gates in CI

- **Data-license linter**: A CI check that scans PRs for patterns that likely
  violate the policy. Examples:
  - New outbound HTTP calls (anything that sends data externally)
  - New `socket`, `urllib`, `httpx.post` calls outside known modules
  - Changes to field allowlists in co-op API responses
  - Removal of audit log calls, rate-limit decorators, or consent checks
  - New dependencies with network access capabilities
  - Changes to embargo timestamp comparisons
- This is a heuristic, not a proof — but it raises the bar significantly. The
  `/data-license` skill already does a manual version of this; automating key
  checks in CI makes it non-bypassable.

### Layer 2: Dependency integrity

- **Lock and pin everything**: `uv.lock` already pins hashes. Ensure CI
  verifies the lockfile is unchanged unless the PR explicitly adds/updates deps.
- **Dependency audit on update**: When a dependency is updated, CI diffs the
  new version's code and flags new network calls, native extensions, or
  post-install hooks. Tools like `pip-audit` for known CVEs, but also
  behavioral diff for new capabilities.
- **Minimal dependency surface**: Periodically audit whether all deps are still
  needed. Fewer deps = smaller attack surface.

### Layer 3: Architectural invariants as tests

- **Integration tests that assert policy**: The existing federation integration
  tests already verify embargo enforcement and field allowlists. Extend this to
  explicitly test *negative* cases that map to policy violations:
  - "Co-op API must not return audio file paths" (PII)
  - "Co-op API must not support bulk pagination beyond N records per request"
  - "No endpoint serves another boat's data without audit log entry"
  - "Embargo'd sessions return 403, not empty data"
- These tests are hard to remove without being noticed — they're not just testing
  functionality, they're asserting policy compliance.

### Layer 4: Contributor trust model

- **Graduated merge rights**: New contributors can merge Low/Standard tier
  changes. High and Critical tier changes require review from a trusted
  maintainer (currently: the boat owner).
- **Two-person rule for Critical modules**: Changes to `auth.py`,
  `peer_auth.py`, `federation.py`, `storage.py` migrations require approval
  from both the contributor and a separate trusted reviewer.
- **CODEOWNERS enforcement**: GitHub CODEOWNERS file requiring specific
  reviewers for Critical/High tier modules. This is the simplest mechanical
  defense — it's already built into GitHub.

### Layer 5: Behavioral monitoring (runtime)

- **Outbound connection audit**: On the Pi, monitor outbound network
  connections. HelmLog should only talk to: Signal K (localhost), known peers
  (Tailscale), Open-Meteo, NOAA CO-OPS, GitHub API, InfluxDB (if configured).
  Any other destination is suspicious.
- **Audit log integrity**: If audit logs are the evidence trail for data access,
  they need to be tamper-resistant. Currently they're in SQLite alongside
  everything else. Consider: append-only log file, or signed audit entries.

### Layer 6: Policy-as-code specification

- **Machine-readable policy**: Convert key data-licensing constraints into a
  structured format (JSON/YAML) that both humans and CI tools can consume.
  Example: a list of "PII fields" that must never appear in co-op API responses,
  or a list of "allowed outbound hosts." The policy doc is authoritative for
  humans; the machine-readable version drives automated checks.

**What makes this different from standard security hardening:**
The threat isn't an external attacker exploiting a vulnerability — it's an
*insider with legitimate access* making changes that are individually plausible
but collectively violate the data-licensing social contract. The defenses need
to make policy violations *structurally difficult*, not just detectable after
the fact.

**Open questions:**
- How much CI overhead is acceptable? Each new gate adds build time.
- Should the data-license linter be a ruff plugin, a standalone script, or a
  Claude Code skill that runs in CI?
- Is CODEOWNERS sufficient for the contributor trust model, or do we need
  something more nuanced (like tier-based approval requirements)?
- How do we handle the "boiling frog" problem — individually innocent changes
  that collectively erode a guarantee? This might require periodic full-codebase
  policy audits rather than per-PR checks.
- What's the right balance between openness to contributors and defensive rigor?
  Too many gates and nobody will contribute; too few and the policy is paper.

**Notes:**
- *2026-03-15:* Initial capture. The lowest-effort, highest-value starting points
  are: (1) CODEOWNERS for Critical/High tier modules — this is a single file and
  GitHub enforces it automatically, (2) extending the existing integration tests
  with explicit policy-violation negative cases — these are already in the test
  suite's idiom, and (3) a CI check that flags new outbound network calls in PRs.
  The more ambitious layers (dependency behavioral diffing, runtime connection
  monitoring, machine-readable policy) are valuable but higher effort. IDX-016's
  risk tiers provide the natural mapping for graduated contributor trust.

---

## IDX-021: Live audio keyword detection on Hailo NPU (AI HAT+ 2)

- **Date captured:** 2026-03-17
- **Origin:** Discussion about integrating a newly purchased Raspberry Pi AI HAT+ 2
- **Status:** `raw`
- **Related:** `src/helmlog/transcribe.py` (existing post-hoc keyword scanning), `src/helmlog/triggers.py`, `src/helmlog/audio.py`, IDX-011 (another hardware integration idea)

**Description:**
Run a lightweight keyword-spotting model on the Raspberry Pi AI HAT+ 2's
Hailo-8L NPU (13 TOPS) during active recording sessions. Instead of waiting
for full Whisper transcription after a session ends, the NPU listens to the
live audio stream and detects trigger words in real-time with near-zero CPU
overhead. Detected keywords fire `boat_settings` actions immediately — auto-mark
a race leg, create a timestamped note, trigger the Insta360 camera, etc.

The AI HAT+ 2 connects via the Pi 5's PCIe FPC ribbon connector, so it
coexists cleanly with the existing MCP2515 CAN HAT on SPI — no hardware
conflicts, no GPIO contention.

This builds on the existing trigger keyword scanning in `transcribe.py`
(`_run_trigger_scan()`), which currently runs post-hoc after Whisper
transcription completes. The live path would use a much smaller, purpose-built
keyword-spotting model (not full Whisper) optimized for the Hailo NPU's
int8/int4 inference pipeline.

**Possible keyword-spotting approaches:**
- **Google's speech_commands / keyword spotting models** — tiny CNNs designed
  for wake-word detection, easily compiled to HEF via Hailo Model Zoo
- **Whisper tiny/base on Hailo** — if the Hailo can run Whisper at real-time
  speed, skip the separate keyword model and do continuous transcription
- **Custom fine-tuned model** — train on sailing-specific vocabulary (mark,
  tack, gybe, protest, starboard) for higher accuracy in wind/engine noise

**Architecture sketch:**
1. `audio.py` feeds raw PCM chunks to both the WAV writer (existing) and a
   new `hailo_kws.py` module (hardware-isolated, only imported by `main.py`)
2. `hailo_kws.py` runs inference on each audio chunk via `hailort` Python
   bindings, emits keyword events
3. Keyword events feed into the existing `triggers.py` framework, which
   already handles event → action mapping
4. Post-session Whisper transcription still runs for full transcript — the
   live path is complementary, not a replacement

**Open questions:**
- Which keyword-spotting model gives the best accuracy-vs-latency tradeoff
  on the Hailo-8L? Need to benchmark with real sailing audio (wind noise,
  engine noise, crew shouting)
- How to handle the audio pipeline split — does `sounddevice` support
  multiple callbacks, or do we need a ring buffer shared between the WAV
  writer and the NPU feeder?
- Should the live keywords be stored separately from post-hoc transcript
  keywords, or reconciled after transcription completes?
- What's the false positive tolerance? A spurious "mark" call creating a
  waypoint is annoying but recoverable; a spurious "protest" trigger could
  be more disruptive
- Hailo SDK maturity on Pi OS — is `hailort` stable enough for always-on
  inference during multi-hour sailing sessions?
- Power draw implications — the AI HAT+ adds ~5W; is the Pi's USB-C PD
  supply sufficient with CAN HAT + AI HAT + USB audio + USB camera?

**Notes:**
- *2026-03-17:* Initial capture. The AI HAT+ 2 uses PCIe (FPC ribbon), the
  CAN HAT uses SPI — confirmed no hardware conflict. First step is to install
  the HAT, get `hailortcli fw-control identify` working, and benchmark a
  simple keyword-spotting model. The existing `TRANSCRIBE_URL` remote-offload
  pattern in `transcribe.py` is a good architectural precedent for
  "prefer accelerator, fall back gracefully."

---

## IDX-022: Capture B&G calibration values as boat settings + calibration analysis

- **Date captured:** 2026-03-17
- **Origin:** Conversation about improving instrument calibration workflow
- **Status:** `promoted`
- **Related:** IDX-005 (tuning auto-population), IDX-011 (write-back to B&G), `boat_settings` infrastructure, `sk_reader.py`, `polar.py`, #337

**Description:**
Capture all B&G instrument calibration values (wind offset, boatspeed factor,
compass deviation, mast height, keel offset, etc.) as boat settings in HelmLog.
These values are currently set on the B&G MFD and live only on the instrument
network — if they change, there's no record of what they were or when they
changed.

The bigger vision: once calibration values are captured over time alongside
sailing data, build an analysis package that evaluates calibration quality and
helps narrow in on better values. For example:

- **Wind:** Compare AWA from instruments against AWA derived from GPS COG/SOG +
  drift model. Flag systematic offsets that suggest the wind calibration is off.
- **Boatspeed:** Compare BSP against GPS SOG in known-current conditions (or
  cross-reference with tidal data from `external.py`). Detect paddle-wheel
  fouling or miscalibrated speed factor.
- **Compass:** Compare heading against GPS COG during straight-line sailing.
  Build a deviation table and compare against the B&G compass calibration.
- **Cross-validation:** Use polar data to detect when multiple calibrations are
  off in ways that partially cancel out (e.g., wind angle offset + boatspeed
  factor error producing "correct-looking" polar performance).

**Data entry approach:**
Manual transcription from B&G instrument setup pages into the Boat Setup panel
in HelmLog. No need to read calibration values from the network — the user
copies them from the MFD setup screens. This is practical because calibration
values change infrequently (a few times per season at most).

**Open questions:**
- What's the full list of B&G calibration fields to capture? At minimum: wind
  angle offset, wind speed factor, boatspeed factor, compass deviation/offset,
  mast height, keel offset, waterline offset. Need to inventory the actual B&G
  setup pages.
- What does the analysis UI look like? Per-session calibration report? Trend
  charts showing calibration drift over time?
- How much sailing data is needed to make statistically meaningful calibration
  recommendations? A single race? A season?
- Should recommendations be advisory only, or could HelmLog eventually write
  corrected calibration values back to the network (ties into IDX-011)?

**Notes:**
- *2026-03-17:* Initial capture. Manual transcription from B&G setup pages into
  the existing boat settings UI — no Signal K / PGN reverse-engineering needed.
  Calibration values change infrequently so manual entry is practical. The
  analysis package is a larger effort that depends on having enough captured data
  to work with. IDX-005 (tuning auto-population) is complementary — that's about
  physical setup (shroud tensions), this is about electronic instrument
  calibration.
- *2026-03-17:* Promoted to #337. Scope: add `instrument_calibration` category
  to `boat_settings.py` with 16 B&G calibration parameters (speed, compass,
  wind, depth, temp, heel/trim, leeway, rudder, mast height). H5000 advanced
  correction tables (TWA/TWS matrices, speed/heel table) deferred to follow-up.

---

## IDX-023: Plugin marketplace monetization — charge models, licensing, and ethos fit

- **Date captured:** 2026-03-17
- **Origin:** Conversation about plugin infrastructure for analysis and visualization
- **Status:** `raw`
- **Related:** IDX-002 (scalable plugin distribution), IDX-004 (custom JS visualization plugins), IDX-020 (supply-chain hardening), `docs/data-licensing.md`

**Description:**
As the plugin infrastructure for analysis and visualization takes shape, there's
a potential opportunity for third-party monetization — letting plugin authors
charge for their work. This could range from individual sailors selling niche
analysis tools to professional coaching outfits packaging visualization suites.

The idea is explicitly tentative. There's a real tension with HelmLog's ethos:
the project is built on boat-owns-its-data, open co-op federation, and
community trust. Introducing money into the plugin ecosystem could erode that —
or it could sustain it by rewarding contributors who build genuinely valuable
tools.

**Dimensions to explore:**

- **Charge models:** Free/open-source only? Freemium (basic free, advanced paid)?
  One-time purchase? Subscription? Pay-what-you-want / tip jar? Per-boat vs
  per-co-op licensing?
- **Distribution:** Central marketplace (HelmLog hosts it)? Decentralized
  (plugin authors host their own, HelmLog indexes)? Git-based with optional
  payment gates?
- **Licensing:** Must paid plugins be open-source with a commercial license
  (dual-license model)? Can they be closed-source? What license governs the
  plugin API itself?
- **Revenue sharing:** Does HelmLog take a cut? If so, what funds does that
  support (infrastructure, development, community)?
- **Data-licensing interaction:** Paid plugins that process co-op data — does
  this create a "selling access to other boats' data" problem? The
  data-licensing policy's view-only and no-bulk-export rules would need to
  extend into the plugin sandbox.
- **Trust and review:** IDX-020's supply-chain concerns are amplified when money
  is involved. A paid plugin has stronger incentives to phone home, fingerprint
  users, or exfiltrate data. Review/audit requirements would need to scale.
- **Community dynamics:** Does a paid tier create a two-class ecosystem (free
  hobbyist plugins vs polished paid ones)? Does it discourage open contribution?

**Ethos questions (the hard ones):**

- Is "boat owns its data" compatible with "plugin author charges for analysis
  of that data"? The boat still owns the data, but the *insight* is behind a
  paywall.
- Does monetization attract the wrong kind of contributor — someone optimizing
  for revenue rather than sailing value?
- Could a tip-jar / patronage model capture the upside (rewarding good work)
  without the downsides (paywalls, perverse incentives)?
- Is the sailing community small enough that a marketplace would never reach
  critical mass anyway, making the complexity not worth it?

**Open questions:**
- What do other open-source projects with plugin ecosystems do? (Grafana,
  Home Assistant, WordPress — each has a different model.)
- Would co-op moderators need a say in which paid plugins are allowed in their
  co-op's data context?
- How does this interact with the protest firewall and gambling prohibition —
  could a paid plugin be used to circumvent those policies?

**Notes:**
- *2026-03-17:* Initial capture. Explicitly flagged as uncertain — the idea may
  not survive contact with HelmLog's values. Recording it now so we can revisit
  when the plugin infrastructure is further along and we have a better sense of
  what the community actually wants. The ethos questions matter more than the
  technical ones here.

---

## IDX-024: On-device transcript and thread summarization via AI HAT+ 2

- **Date captured:** 2026-03-19
- **Origin:** Conversation about using the Raspberry Pi AI HAT+ 2 (Hailo-8, 26 TOPS) for summarizing transcripts and co-op discussion threads
- **Status:** `raw`
- **Related:** IDX-021 (live keyword detection on Hailo), IDX-013 (co-op discussion threads), `src/helmlog/transcribe.py`, `src/helmlog/audio.py`, AI HAT+ 2 hardware

**Description:**
Summarize post-session audio transcripts and (eventually) co-op discussion
threads, either on-device or via API. The AI HAT+ 2's Hailo-8 NPU (26 TOPS)
can accelerate encoder-style models but is not well-suited for autoregressive
LLM token generation — so the summarization path depends on connectivity and
quality requirements.

**Three approaches, in order of likely value:**

1. **Hybrid (recommended starting point):** Use the Hailo NPU to accelerate
   faster-whisper's encoder (the transcription step that precedes summarization),
   then send the transcript text to the **Claude API** (Haiku for cost, Sonnet
   for quality) for summarization when connectivity is available (post-sail,
   via Tailscale). Results cached in SQLite alongside the transcript. This gives
   the best summary quality by far — small on-device LLMs struggle with the
   nuance of sailing tactical discussion.

2. **Offline fallback (small on-device LLM on CPU):** For summarization without
   connectivity, run a quantized LLM on the Pi 5's ARM cores via `llama.cpp`:
   - **Qwen2.5-1.5B (Q4, ~1 GB)** — best quality-to-size ratio at this scale
   - **Phi-3.5-mini (3.8B, Q4, ~2.3 GB)** — better quality, fits in 8GB Pi 5
     with ~3-4 GB free after helmlog + faster-whisper
   - **Gemma 2 2B (Q4, ~1.5 GB)** — decent instruction following
   - **BART-large-CNN (~0.5 GB)** — purpose-built summarizer, much faster,
     but extractive only (good for "key moments" extraction, not narrative summary)
   The Hailo NPU doesn't help here — these models run on CPU. Quality will be
   noticeably worse than API-based summarization, especially for domain-specific
   sailing vocabulary and tactical nuance.

3. **Task-specific model on Hailo (future):** If Hailo's model zoo expands to
   support small encoder-decoder summarization models (BART, T5), these could
   run efficiently on the NPU. Currently the Hailo ecosystem is vision-heavy
   and this path isn't practical.

**What to summarize:**

- **Session transcripts:** After Whisper transcription completes, generate a
  summary of the crew conversation — key decisions, tactical calls, wind shift
  observations, maneuver discussions. Store alongside the full transcript in
  SQLite. Display on the session detail page.
- **Co-op discussion threads (future, depends on IDX-013):** When threads
  accumulate replies, generate a thread summary — key points, unresolved
  questions, action items. Useful for catching up on a discussion you missed.
- **Multi-session digest:** Summarize patterns across multiple sessions —
  "over the last 5 races, crew consistently discussed difficulty with leeward
  mark approaches" — requires aggregating transcripts.

**Key design questions:**
- **When to summarize?** Immediately after transcription? On-demand when the
  user views the session? Batch overnight?
- **Summary length/format?** Bullet points? Narrative paragraph? Structured
  (key decisions, wind observations, crew discussion topics)?
- **Hailo for whisper acceleration:** The Hailo-8 can accelerate the Whisper
  encoder via HailoRT — this would speed up the transcription step that
  precedes summarization. Worth benchmarking vs CPU-only faster-whisper.
- **Cost management for API path:** Haiku is ~$0.25/MTok input — a typical
  session transcript might be 2-5K tokens, so ~$0.001 per summary. Negligible
  per session but worth tracking.
- **Privacy:** Transcripts contain crew conversation (PII). API summarization
  sends this to Anthropic's servers. The data-licensing policy requires that
  PII handling respects deletion rights. Need to ensure API calls don't result
  in data retention beyond the response. Anthropic's API data policy (no
  training on API data) helps here.

**Connection to IDX-021:**
IDX-021 proposes live keyword detection on the Hailo during recording. This
idea is complementary — IDX-021 handles real-time triggers, this handles
post-session understanding. Both benefit from Hailo-accelerated Whisper
(faster transcription = faster time-to-summary and time-to-keywords).

**Notes:**
- *2026-03-19:* Initial capture. The hybrid approach (Hailo-accelerated Whisper
  + Claude API summarization) is the clear starting point — it uses the Hailo
  where it's strong (encoder acceleration) and the API where it's strong
  (language understanding). On-device LLM fallback is a nice-to-have for
  offline use but the quality gap is significant for domain-specific content.
  First step: benchmark faster-whisper on Hailo vs CPU to see if the
  acceleration is worth the integration effort.

---

## IDX-025: Deployment annotations on the Pi Health Grafana dashboard

- **Date captured:** 2026-03-19
- **Origin:** Conversation about correlating performance regressions with deployments
- **Status:** `raw`
- **Related:** `src/helmlog/deploy.py`, `src/helmlog/influx.py`, `src/helmlog/monitor.py`, Grafana Pi Health dashboard

**Description:**
Add annotations to the Pi Health Grafana dashboard that mark when deployments
happened. Each annotation should include metadata about the deployment:

- **Trigger type:** explicit (manual `helmlog deploy` or web UI button) vs
  evergreen (automatic deploy via the scheduled check in `deploy.py`)
- **Who initiated it:** the user who triggered the deploy (from auth context
  or SSH user), or "evergreen" for automatic deploys
- **Branch:** the branch that was deployed (e.g., `main`, `stage`, `live`)
- **Version/commit:** the git SHA and any tag at the deployed commit
- **Previous version:** what was running before the deploy (for easy diff)
- **Deploy result:** success/failure, duration, whether uv sync ran,
  whether the service restarted

The goal: when the health dashboard shows a CPU spike, memory increase, or
latency change, you can immediately see whether it correlates with a
deployment — and if so, exactly which version introduced the change. No more
"wait, when did we last deploy?" followed by SSH + git log.

**Implementation approach:**
The existing `influx.py` module already writes points to InfluxDB (session
notes, historical tracks). A new `write_deploy_annotation()` function would
write a point to a `deployments` measurement with tags for trigger type,
branch, user, and fields for commit SHA, previous SHA, duration, and result.
Grafana can query this measurement as an annotation source on any dashboard
panel.

The call site would be in `deploy.py`'s `execute_deploy()` — after a
successful (or failed) deploy, write the annotation with all available
metadata. The deploy result dict already contains most of this information.

**Grafana annotation display:**
- Vertical line on time-series panels at the deployment timestamp
- Hover tooltip shows: trigger type, branch, commit, who, duration, result
- Color-coded: green for success, red for failure
- Filterable by branch or trigger type

**Key design questions:**
- Should failed deploys also be annotated? Probably yes — a failed deploy
  that partially ran (e.g., git pull succeeded but uv sync failed) could
  still affect system behavior
- How to capture the "who" for evergreen deploys? The deploy runs as the
  helmlog service user — tag as "evergreen/automatic" rather than a person
- Should the annotation include a link to the git diff between old and new
  commits? Grafana annotations support URLs — a GitHub compare link
  (`/compare/old_sha...new_sha`) would be very useful
- Should we also annotate service restarts that aren't deploys (e.g.,
  `systemctl restart helmlog` for config changes)?

**Notes:**
- *2026-03-19:* Initial capture. The infrastructure is already in place —
  `influx.py` has the InfluxDB client pattern, `deploy.py` has the deploy
  result data, and the Grafana dashboard exists. This is mostly wiring:
  capture deploy metadata → write InfluxDB point → configure Grafana
  annotation query. The GitHub compare link in the annotation tooltip is
  the high-value detail — one click from "something changed" to "here's
  what changed."

---

## IDX-026: Vision-based sail settings capture for post-race analysis

- **Date captured:** 2026-03-22
- **Origin:** Phase four data entry planning — exploring ways to capture sail configuration automatically
- **Status:** `raw`
- **Related:** `cameras.py`, `insta360.py`, `storage.py`, `boat_settings.py`, IDX-022 (B&G calibration values)

**Description:**
Use computer vision to detect and record sail settings from camera imagery
during sailing sessions. The Insta360 X4 (or a dedicated camera) captures
the physical state of the sails — trim, draft, twist, cunningham tension,
outhaul, vang, backstay, jib lead position, etc. — and a vision model
classifies or measures these settings, storing them as time-series data
alongside instrument readings.

This fills a major gap in sailing data analysis: instrument systems record
wind, speed, and heading, but the *sail configuration* that produced those
numbers is lost unless someone manually logs it. By capturing sail settings
automatically via vision, every tack, gust response, and mode change has
both the "what happened" (instruments) and the "what we were doing" (sail
trim) — enabling much richer post-race debrief and polar analysis.

**Potential approaches:**
1. **On-device inference (Hailo AI HAT+):** Run a sail-state classification
   model on the NPU. Low latency, no connectivity needed, but requires a
   custom-trained model and the Hailo ecosystem is still maturing for
   custom models.
2. **Post-session batch processing:** After a session, run vision analysis
   on captured video/stills. Can use larger, more capable models (cloud or
   local). Latency doesn't matter since it's for debrief, not real-time.
3. **Periodic snapshot + cloud API:** Take a photo every N seconds, send to
   a vision API (Claude, etc.) for classification. Simple but requires
   connectivity and has cost implications over long sessions.

**What to detect:**
- Mainsail: reef state, cunningham, outhaul, vang, traveler position
- Jib/genoa: lead position, sheet tension, halyard tension
- Spinnaker: pole height/angle, sheet/guy trim, halyard
- General: heel angle from horizon (cross-check with instruments),
  sail shape (draft depth, draft position, twist)

**Key design questions:**
- Which camera angle gives the best view of sail settings? The Insta360's
  360° field is an advantage but distortion correction adds complexity.
  A dedicated mast-mounted or boom-mounted camera might give cleaner input.
- How to label training data? Sail settings are continuous, not discrete —
  do we classify into buckets (light/medium/heavy trim) or try to measure
  actual values? Buckets are easier to train and probably sufficient for
  debrief.
- How to store the data? Extend `boat_settings` with sail-state columns,
  or create a new `sail_settings` table with foreign key to sessions?
- Integration with polar analysis: once sail settings are captured, the
  polar builder could segment performance by sail configuration — "your
  VMG in 12 kts TWS is X with the outhaul eased vs Y with it tight."
- Privacy: sail settings are competitive information. Should these be
  excluded from co-op sharing by default, or included as part of the
  "view-only" co-op data?

**Notes:**
- *2026-03-22:* Initial capture. This is a "phase four" data entry concept —
  phases one through three presumably cover instrument data, manual entries,
  and audio/transcript capture. Vision-based sail settings would be a
  significant step toward fully automated sailing data collection. The
  Insta360 integration (`cameras.py`) and AI HAT+ (IDX-021) provide
  infrastructure building blocks, but the vision model itself is the hard
  part — no off-the-shelf model exists for sail trim classification.
  Training data collection could start now by capturing labeled stills
  during sessions.

---

## IDX-027: Race cards with yacht club burgee and crew snapshot

- **Date captured:** 2026-03-22
- **Origin:** Conversation about making the race list more visually rich and informative
- **Status:** `raw`
- **Related:** `web.py`, `races.py`, `storage.py`, `templates/history.html`, `templates/session.html`, IDX-013 (co-op threads with crew visibility)

**Description:**
Each race in the history/session list should display as a visual "card"
featuring two key elements: (1) a thumbnail of the yacht club burgee for
the club hosting the race, and (2) a snapshot of the crew that was aboard.

**Burgee thumbnails:** Yacht clubs have distinctive burgee flags that are
instantly recognizable to sailors. Showing the burgee next to a race
immediately communicates *where* a race happened without reading text.
This requires a burgee image source — options include:
- A curated local library of burgee images (SVG or PNG) bundled with
  HelmLog, keyed by club name or a standardized club identifier
- User-uploaded burgee images when configuring a race/regatta
- A community-maintained burgee registry that HelmLog can pull from
- Scraping from yacht club websites (fragile, IP concerns)

**Crew snapshot:** Show who was on the boat for each race — names and
positions (helm, tactician, trimmer, bow, etc.). This makes the history
browsable by *who you sailed with*, not just when and where. Combined
with audio transcripts (already captured), you can revisit the crew
conversations from a specific race with a specific crew.

**Key design questions:**
- Where does the burgee image come from? A local curated set is most
  reliable but requires maintenance. User upload is simplest but
  means every user re-uploads the same burgees.
- How are yacht clubs identified? Need a consistent identifier —
  club name strings are fragile (abbreviations, typos). Could use
  a canonical club registry or let users define their clubs.
- How is crew captured per race? Currently there's no per-session
  crew roster. Options: manual entry in the web UI before/after a
  race, voice recognition from audio to identify speakers, or a
  simple crew check-in feature.
- Storage: burgee images in `static/` or `data/`? Crew roster as a
  new table or JSON column on sessions?
- Co-op implications: if crew names are shared across co-op, they
  become PII subject to data-licensing rules. Crew positions alone
  may be sufficient for co-op views.

**Notes:**
- *2026-03-22:* Initial capture. This would significantly improve the
  visual experience of browsing race history. The burgee concept is
  especially compelling — sailors immediately recognize club burgees
  and it adds a sense of place and community to the data. Crew
  snapshots connect to several other ideas (IDX-013 crew visibility,
  audio transcription with diarization). The main prerequisite is a
  per-session crew management feature, which doesn't exist yet.

---

## IDX-028: Boat owner headshots in co-op peer views

- **Date captured:** 2026-03-22
- **Origin:** Conversation about visual identity — making other boats in the co-op feel like people, not just data
- **Status:** `raw`
- **Related:** `federation.py`, `peer_api.py`, `peer_client.py`, `web.py`, IDX-027 (race cards with burgee/crew), IDX-013 (co-op threads with crew visibility), IDX-018 (anti-parasocial design)

**Description:**
When viewing other boats in the co-op — peer list, session comparisons,
track overlays, discussion threads — show a headshot of the boat's owner.
This puts a face to the data and makes the co-op feel like a community of
people rather than an anonymous fleet of boat names and sail numbers.

Currently, boats in the co-op are identified by boat name and Ed25519
fingerprint. Adding an owner headshot (and optionally name) makes the
experience more personal and builds trust — you're sharing your data with
Dave on *Aria*, not with `fingerprint:a3b7c9...`.

**How it could work:**
- Owner uploads a headshot during `helmlog identity init` or via the
  admin settings page
- The headshot is included in the boat card (the identity bundle shared
  during co-op join/invite)
- Peer boats cache the headshot locally after receiving the boat card
- The image is displayed as a small avatar wherever that boat appears:
  co-op peer list, session list, track comparison views, thread posts

**Key design questions:**
- **Image size/format:** Boat cards are exchanged over Tailscale between
  Pis. Keep headshots small — a 128x128 JPEG or WebP thumbnail. Resize
  on upload.
- **Boat card format:** Currently the boat card contains Ed25519 public
  key, boat name, and metadata. Adding a base64-encoded image increases
  card size but keeps it self-contained. Alternatively, serve the image
  via the peer API and let peers fetch it.
- **Updates:** What happens when the owner changes their photo? Need a
  card versioning or update mechanism so peers get the new image.
- **Privacy / PII:** A headshot is biometric-adjacent PII under the data
  licensing policy. The owner consents by uploading, but co-op peers
  cache it — does deletion/anonymization need to propagate? If the
  owner leaves the co-op, peers should purge the cached headshot.
- **Multi-owner boats:** Some boats have co-owners or the "owner" rotates
  (club boats, charter boats). May need to support multiple faces or
  use a boat logo as fallback.
- **Fallback:** Boats without a headshot should show a generated avatar
  (initials, or a deterministic pattern from the fingerprint) rather
  than a blank placeholder.

**Notes:**
- *2026-03-22:* Initial capture. This pairs naturally with IDX-027 (burgee
  + crew for your own races) — together they create a visually rich,
  people-first experience. The PII angle needs careful design: headshots
  cached on peer boats must be deletable when someone leaves the co-op.
  The anti-parasocial principles from IDX-018 apply here too — showing
  faces builds genuine connection but shouldn't create social pressure
  or surveillance dynamics.

---

## IDX-029: Owner/admin data deletion — sessions, races, debriefs, practices, synthesized data

- **Date captured:** 2026-03-22
- **Origin:** Conversation about data management capabilities; user noted this could be promoted to an actionable issue
- **Status:** `promoted` → [#409](https://github.com/weaties/helmlog/issues/409)
- **Related:** Data licensing policy (`docs/data-licensing.md`), `storage.py`, `web.py`, `transcribe.py`, `audio.py`

**Description:**
Boat owners and admins currently have no way to delete sessions, races, debriefs,
practice sessions, or synthesized data through the UI or CLI. As data accumulates
on the Pi's limited storage, and as users refine their workflow, they need the
ability to clean up unwanted records. This is also a data-licensing requirement —
the policy states that the boat owns its data and has deletion rights over PII
(audio, transcripts, diarized content).

**Key design questions:**
- **Soft delete vs hard purge:** Soft delete (mark as deleted, hide from UI) is
  safer and allows undo, but doesn't reclaim disk space until a separate purge
  step. Hard delete is simpler but irreversible.
- **Cascade behavior:** Deleting a session should cascade to its instrument data,
  audio recordings, transcripts, and any synthesized data derived from it. Need
  to define the full dependency graph.
- **Co-op implications:** If session data has been shared with co-op peers, does
  deletion send a retraction to peers? The data-licensing policy may require
  notifying peers to purge cached data.
- **Audit trail:** Should deletions be logged for compliance? The data-licensing
  policy requires audit logging for co-op data access — deletion should likely
  be logged too.
- **Authorization:** Owner can delete anything on their boat. Admin role (if
  distinct from owner) may need scoped permissions. Crew members should not
  be able to delete.
- **Disk reclamation:** Audio WAV files and video metadata can be large. Deletion
  should remove files from `data/` as well as database rows.

**Notes:**
- *2026-03-22:* Initial capture. This is close to actionable — the scope is clear
  and effort is estimable. The main blocker is resolving the design questions above,
  particularly cascade behavior and co-op retraction. Could be promoted to a GitHub
  issue once those are settled.
- *2026-03-23:* Promoted to #409. Design decisions: hard delete (not soft),
  admin-only (admin = boat owner), active session guard (409), cascade via
  `delete_race_session()`, co-op sharing rows cascade via FK, audit logged.
  UI delete button on session detail page.

---

## IDX-030: DIY rudder angle sensor + Signal K integration for rudder angle logging

- **Date captured:** 2026-03-22
- **Origin:** Conversation about what exists around rudder angle — calibration offset is captured (#337) but no actual rudder angle data logging yet
- **Status:** `raw`
- **Related:** #343 (B&G PGN survey), #337 (calibration values), IDX-022 (calibration analysis), IDX-011 (write-back to B&G), `sk_reader.py`, `boat_settings.py`

**Description:**
Two parts to this idea: (1) log rudder angle data in HelmLog, and (2) build a
DIY rudder angle sensor instead of buying the B&G rudder transducer (which runs
several hundred dollars).

**Part 1 — HelmLog rudder angle logging:**
Subscribe to `steering.rudderAngle` in `sk_reader.py` and store it as a
time-series channel alongside existing instrument data. The `rudder_angle_offset`
calibration parameter already exists in `boat_settings.py` (from #337). Once the
data is flowing, rudder angle becomes available for export, session replay, and
performance analysis (e.g., correlating rudder movement with VMG, detecting
over-steering, comparing tack technique).

This is partially blocked on #343 (PGN survey) — if Signal K is already decoding
PGN 127245 (NMEA 2000 standard rudder angle) from the B&G network, HelmLog just
subscribes. If the B&G system doesn't broadcast rudder angle (no transducer
installed), Part 2 provides the data source.

**Part 2 — DIY rudder angle sensor:**
Build a low-cost rudder angle sensor using off-the-shelf components and publish
the reading to Signal K, so HelmLog (and any other Signal K consumer) can use it
without buying B&G's proprietary transducer.

Candidate hardware approaches:
- **Rotary encoder / potentiometer on the rudder post:** Mount a continuous-turn
  potentiometer or absolute rotary encoder (e.g., AS5600 magnetic encoder, ~$3)
  on the rudder post or quadrant. Direct mechanical coupling gives reliable angle
  measurement. Needs a bracket or coupling to attach to the post.
- **Hall effect sensor + magnet:** Mount a magnet on the rudder post and a
  hall-effect angle sensor nearby. Non-contact, no wear. The AS5600 is actually
  a hall-effect sensor with I2C output — cheap and readily available.
- **IMU on the rudder blade:** Mount a small IMU (e.g., BNO055) on the rudder
  blade itself. Measures absolute orientation. More complex but doesn't need
  mechanical coupling to the post.

Candidate microcontrollers:
- **ESP32** — WiFi built in, can POST Signal K deltas directly over HTTP or
  WebSocket. Lots of Arduino/MicroPython examples for AS5600 + ESP32. Low power,
  can run off a small USB supply. This is probably the simplest path.
- **Raspberry Pi Pico W** — similar WiFi capability, MicroPython or C SDK.
  Slightly more I/O flexibility than ESP32.
- **Pi Zero 2W** — overkill for this but could run a full Signal K plugin
  if more processing is needed.

Signal K integration:
- The sensor publishes deltas to Signal K Server's HTTP PUT API or WebSocket
  delta channel. Signal K path: `steering.rudderAngle` (radians, standard SK
  units). The sensor is just another data source to Signal K — no HelmLog code
  changes needed beyond the subscription in Part 1.
- Signal K's `signalk-to-nmea2000` plugin can optionally re-broadcast the
  rudder angle as PGN 127245 onto the NMEA 2000 bus, making it available on
  B&G displays too (connects to IDX-011's write-back concept).

**Open questions:**
- What's the best mounting approach for the rudder post on this specific boat?
  Need to inspect the quadrant/tiller arrangement below decks.
- Waterproofing and power — the sensor will be in the lazarette or near the
  rudder post. USB power from the Pi? Battery with charging?
- Calibration — the AS5600 gives absolute angle, but mounting offset needs to
  be zeroed. Could use the existing `rudder_angle_offset` in boat_settings.
- Sampling rate — how fast does rudder angle need to be sampled for useful
  analysis? 5 Hz? 10 Hz? B&G typically sends PGN 127245 at 10 Hz.

**Notes:**
- *2026-03-22:* Initial capture. The AS5600 + ESP32 path looks like the sweet
  spot — under $10 in parts, well-documented, WiFi-native. The HelmLog side
  (Part 1) is straightforward once Signal K has the data. The mechanical
  mounting is the main unknown — need to look at the boat's rudder post
  arrangement to design a bracket.

---

## IDX-031: Pluggable AI provider abstraction for all AI workloads

- **Date captured:** 2026-03-23
- **Origin:** Conversation about configuring multiple AI providers for offloading transcription, diarization, video/still analysis, thread summarization, and future AI workloads
- **Status:** `raw`
- **Related:** IDX-024 (on-device transcript summarization via Hailo + Claude API), IDX-021 (live keyword detection on Hailo NPU), `src/helmlog/transcribe.py` (existing `TRANSCRIBE_URL` remote-offload pattern)

**Description:**
HelmLog has a growing number of AI-powered workloads — transcription
(faster-whisper), diarization (pyannote), and planned features like thread
summarization (IDX-024), video/still analysis (IDX-026), and live keyword
detection (IDX-021). Today these are tightly coupled to specific
implementations (faster-whisper on-device, pyannote on-device). The idea is a
**pluggable provider abstraction** that lets any AI workload route to any
configured backend, with the user able to configure multiple providers and
priority/fallback chains.

**Candidate providers:**
- **Self-hosted** — a Mac at home running Whisper, llama.cpp, or other models
  over the local network or Tailscale. Zero marginal cost, full privacy, but
  requires hardware and setup.
- **Anthropic (Claude API)** — strong for summarization, analysis, reasoning.
  Already discussed in IDX-024 for transcript summaries.
- **OpenAI** — Whisper API for transcription, GPT for summarization/analysis,
  vision models for still/video frame analysis.
- **Google (Gemini)** — long-context models useful for analyzing full session
  transcripts or multi-image analysis. Competitive pricing.
- **Perplexity** — search-augmented generation, potentially useful for
  contextualizing weather/racing conditions against external knowledge.
- **On-device (Hailo NPU / Pi CPU)** — lowest latency, no connectivity
  required, but limited model capability. Already the default for
  transcription.

**Workloads that would use the abstraction:**
1. **Transcription** — `transcribe.py` already has `TRANSCRIBE_URL` for remote
   offload. This is the existing precedent for the pattern.
2. **Diarization** — currently pyannote on-device. Could offload to a more
   capable model remotely.
3. **Video/still analysis** — analyzing sail trim photos, start-line video,
   mark roundings. Requires vision-capable models (GPT-4o, Claude, Gemini).
4. **Thread/transcript summarization** — IDX-024's use case. Route to Claude,
   GPT, Gemini, or a local LLM depending on connectivity and preference.
5. **Race debrief generation** — synthesizing instrument data + transcript +
   video into a structured debrief narrative.
6. **Future workloads** — anything AI-powered that gets added later
   automatically benefits from the provider abstraction.

**Design sketch:**
- A provider registry configured via environment variables or a config file.
  Each provider has: name, type (api/self-hosted/on-device), endpoint,
  credentials, supported capabilities (transcription, vision, text-gen, etc.),
  cost tier, and priority.
- Each AI workload declares what capability it needs (e.g., "transcription",
  "vision", "text-generation"). The abstraction resolves the best available
  provider based on: user priority preferences, connectivity status,
  capability match, and cost constraints.
- Fallback chains: if the preferred provider is unreachable (e.g., no internet
  on the water), fall back to the next provider (e.g., on-device). The Pi is
  often offline during racing — graceful degradation is essential.
- Credential management: API keys stored in `.env` or a secrets file,
  per-provider. Never committed to git.
- Cost tracking: log API usage per provider per workload so the user can see
  what they're spending. Store in SQLite alongside the workload results.

**Open questions:**
- How granular should provider configuration be? Per-workload overrides
  (e.g., "use OpenAI Whisper for transcription but Claude for summaries")
  vs. a single priority list?
- Should the abstraction handle batching/queueing? E.g., queue up
  transcription jobs while offshore, process them when back on WiFi.
- How to handle provider-specific features? E.g., Gemini's long context
  window, Claude's structured output, OpenAI's function calling. Lowest
  common denominator vs. provider-specific extensions?
- Self-hosted discovery: how does the Pi find a Mac running a local model
  server? Tailscale + mDNS? Explicit config?
- Data licensing implications: sending crew audio/transcripts to external
  APIs requires consent per the data-licensing policy. How does this
  interact with the per-person consent model for PII?

**Notes:**
- *2026-03-23:* Initial capture. The `TRANSCRIBE_URL` pattern in
  `transcribe.py` is already a minimal version of this — a single remote
  endpoint for one workload. This idea generalizes it to N providers × M
  workloads with fallback chains. IDX-024 would become one consumer of this
  abstraction rather than its own bespoke provider selection. The PII/consent
  question is the trickiest design constraint — the data-licensing policy
  is clear that audio and transcripts are PII, so routing them to external
  APIs needs explicit user/crew consent.

---

## IDX-032: Automatic sea state recording via ESP32 accelerometer

- **Date captured:** 2026-03-30
- **Origin:** Planning conversation about automatically recording sea state instead of relying on manual crew entries
- **Status:** `raw`
- **Related:** IDX-012 (GRIB forecasts), IDX-030 (DIY rudder sensor), IDX-005 (tuning guide from wind range), IDX-026 (vision-based sail settings), `boat_settings.py` (manual `swell_height`, `swell_period`, `chop` fields), `external.py`, `polar.py`

**Description:**
Use an ESP32 with a 6-axis IMU (e.g., ICM-42688-P or LSM6DSO32) mounted
rigidly near the hull centerline to measure wave height, period, and boat
motion — then transmit summaries to the Pi over WiFi for storage and analysis.

The crew currently logs sea state manually via `boat_settings.py` fields
(`swell_height`, `swell_period`, `chop`), but this is infrequent, subjective,
and often forgotten. Automatic measurement gives continuous, objective data
that can feed into polar performance analysis and post-race debrief.

**Architecture — ESP32 as dedicated wave sensor:**

```
ESP32 + IMU (hull-mounted)          Raspberry Pi (HelmLog)
├─ 50 Hz accel sampling             ├─ POST /api/sea-state endpoint
├─ 60 s buffer (3000 samples)       ├─ sea_state table in SQLite
├─ High-pass filter → double        ├─ Merge with Open-Meteo marine
│  integrate → displacement          │  forecast for cross-validation
├─ FFT → wave energy spectrum        ├─ Display on home + session pages
├─ Extract Hs, Tp, heave RMS,       └─ Feed into polar/analysis
│  roll RMS, pitch RMS
└─ POST summary JSON every 30 s
```

**Why ESP32 over Pi for the IMU:**
- Deterministic sampling via FreeRTOS timer (no Linux scheduler jitter)
- ~50 mA power draw vs. Pi's ~3 W — can battery-power or sleep between bursts
- Tiny form factor for optimal hull mounting location
- Failure isolation — if the ESP32 crashes, HelmLog keeps running

**Summary JSON payload:**

```json
{
  "sensor_id": "esp32-hull-01",
  "ts": "2026-03-30T14:22:00Z",
  "wave_height_m": 0.8,
  "wave_period_s": 5.2,
  "heave_rms_m": 0.3,
  "roll_deg_rms": 4.1,
  "pitch_deg_rms": 2.8,
  "confidence": 0.85,
  "sample_count": 3000,
  "battery_v": 3.92
}
```

**Layered data sources (no single source is sufficient):**

| Layer | Source | What it gives | When it's best |
|-------|--------|---------------|----------------|
| 1 | Open-Meteo marine API | Background swell + wave forecast | Always available, baseline |
| 2 | NOAA buoy observations | Ground-truth from offshore buoys | Coastal US waters |
| 3 | ESP32 IMU | Local Hs, Tp, heel/pitch variance | Real-time, on-the-boat |
| 4 | Crew override | Manual swell/chop (existing) | Trumps all when entered |

Open-Meteo already supports marine wave parameters (`significant_wave_height`,
`wave_period`, `wave_direction`, `swell_wave_height`, `swell_wave_period`,
`swell_wave_direction`) but `external.py` doesn't request them yet — that's
the lowest-effort first step regardless of the ESP32 work.

**Data model sketch:**

```sql
CREATE TABLE sea_state (
  ts TEXT NOT NULL,
  source TEXT NOT NULL,  -- 'esp32' | 'open-meteo' | 'noaa-buoy' | 'crew'
  sensor_id TEXT,
  wave_height_m REAL,
  wave_period_s REAL,
  wave_dir_deg REAL,
  swell_height_m REAL,
  swell_period_s REAL,
  swell_dir_deg REAL,
  heave_rms_m REAL,
  roll_deg_rms REAL,
  pitch_deg_rms REAL,
  confidence REAL,
  sample_count INTEGER
);
```

**Direction estimation:**
A 6-axis IMU (accel + gyro, no magnetometer) gives wave height and period
but not direction. Options: (a) add a magnetometer (MMC5603 or LIS3MDL) for
9-axis with cross-spectral direction estimation, (b) infer from wind direction
in Signal K (swell ~aligns with wind, chop is downwind), or (c) skip direction
initially — height and period are most useful for performance analysis.

**Hardware available:** Raspberry Pi, multiple ESP32s, ESP32-CAMs. The
ESP32-CAMs could serve a secondary role in visual wave estimation (horizon
tracking for roll angle and dominant period) but the IMU path is simpler
and more reliable as the primary sensor.

**Open questions:**
- WiFi connectivity: is the ESP32 on the boat's existing WiFi network, or
  does it need its own AP / direct connection to the Pi?
- Power: USB from a 12 V→5 V converter, or battery with sleep cycles?
- Mounting: hull centerline below deck is ideal; mast base is acceptable
  but noisier from rigging vibration
- ESP32 firmware: live in this repo (`firmware/`) or a separate repo?
- Direction: worth adding a magnetometer now, or start with Hs + Tp only?

**Notes:**
- *2026-03-30:* Initial capture. The simplest first step is extending
  `external.py` to request Open-Meteo marine wave parameters — zero hardware,
  immediate value for debrief context. The ESP32 sensor is the high-value
  second step that gives real-time local measurements. IDX-030 (DIY rudder
  sensor) follows the same ESP32-to-Pi pattern and could share firmware
  infrastructure.

---

## IDX-033: Gear-switching analysis — detecting crew adaptation to changing conditions

- **Date captured:** 2026-03-30
- **Origin:** Planning conversation about sea state recording; desire to know whether crew is "switching gears" when conditions change
- **Status:** `raw`
- **Related:** IDX-032 (sea state recording), IDX-005 (tuning guide from wind range), IDX-026 (vision-based sail settings), IDX-011 (write derived data back to B&G), `polar.py`, `race_classifier.py`, `triggers.py`

**Description:**
An analysis package that detects whether the crew is adapting their sailing
technique — "switching gears" — in response to changing sea state, wind, or
other conditions. In racing, failing to adapt mode (pointing vs. footing,
flat vs. powered-up, smooth vs. aggressive helming) when conditions shift is
one of the biggest performance leaks, and it's invisible in traditional
post-race analysis.

**What "gear switching" means in sailing:**
Sailors talk about "gears" as distinct modes of operation tuned to conditions:

| Gear | Conditions | Characteristics |
|------|-----------|-----------------|
| Light/smooth | < 8 kts TWS, flat water | Point high, minimal helm movement, crew weight forward |
| Medium/powered | 8-14 kts, moderate chop | Target speed, crew hiking, moderate helm activity |
| Heavy/survival | 15+ kts, steep chop | Foot for speed, depower (vang, backstay, traveler), aggressive crew movement |
| Swell mode | Any TWS + large swell | Pump-like heel/trim rhythm, speed-build on wave faces |

**Detection signals (all available or planned in HelmLog):**

| Signal | Source | What it reveals |
|--------|--------|-----------------|
| Helm activity (rudder angle variance) | `rudder` table | Aggressive vs. smooth steering |
| Heel angle variance | ESP32 IMU (IDX-032) | Powered up vs. depowered |
| Speed-through-water stability | `speeds` table | Mode change shows as BSP variance shift |
| TWA target changes | `winds` + `cogsog` | Pointing vs. footing shows in TWA histogram |
| Polar performance ratio | `polar.py` | Drop in % target BSP after condition change |
| Tack/gybe frequency | derived from `headings` | Tactical response to shifts |
| Sea state transition | `sea_state` table (IDX-032) | The trigger event itself |

**Analysis approach:**

1. **Condition change detection** — identify when sea state, TWS, or TWD
   shifts beyond a threshold (e.g., Hs changes by > 0.3 m, TWS by > 3 kts).
   Use the `sea_state` and `winds` tables with a sliding window.

2. **Response window** — define a window (e.g., 2-5 minutes) after a detected
   condition change. This is the period where the crew should be adapting.

3. **Behavior change metrics** — compare the statistical signature of sailing
   parameters (rudder variance, heel variance, BSP/polar%, TWA distribution)
   in the pre-change window vs. the post-change window.

4. **Gear-switch score** — quantify how much the crew's behavior changed
   relative to how much conditions changed. High score = responsive crew.
   Low score = "sailing the old breeze."

5. **Debrief output** — timeline visualization showing condition changes as
   vertical bands, with gear-switch scores overlaid. Highlight "missed shifts"
   where conditions changed significantly but sailing parameters didn't adapt.

**Example debrief insight:**
> "At 14:22, sea state increased from 0.5 m to 1.1 m Hs over 4 minutes.
> Wind remained 12 kts. Crew did not adapt — rudder variance stayed low
> (smooth-water style), BSP dropped 8% below polar target. Comparable
> boats in the co-op who adapted showed only 3% polar drop. Suggested
> response: increase helm activity, foot 5° lower, power up with vang."

**Co-op dimension:**
If multiple boats in a co-op share session data, gear-switching analysis
becomes comparative — "Boat A adapted in 90 seconds, Boat B took 5 minutes,
Boat C never adapted." This is powerful coaching data. Subject to the
data-licensing policy (co-op data is view-only, no bulk export).

**Dependencies:**
- Sea state recording (IDX-032) — the primary trigger signal
- Rudder angle logging (#337 / IDX-030) — key behavior metric
- Polar performance baselines (`polar.py`) — for target BSP comparison
- Optionally: vision-based sail settings (IDX-026) — physical gear evidence

**Open questions:**
- What thresholds define a "condition change" worth analyzing? Should these
  be configurable per boat class / crew experience level?
- How to handle gradual shifts vs. sudden changes (squall vs. building sea)?
- Should the analysis run in real-time (live "you should be switching gears"
  alert, possibly via IDX-011 to the B&G display) or post-race only?
- How to avoid false positives — tactical maneuvers (tack, gybe, mark
  rounding) look like behavior changes but aren't gear switches
- What's the minimum data set needed? Can we do a useful version with just
  wind + BSP + rudder, before the ESP32 sea state sensor exists?

**Notes:**
- *2026-03-30:* Initial capture. This is the "so what" that makes sea state
  recording valuable beyond just logging a number. Without analysis, sea
  state data sits in a table. With gear-switching detection, it becomes
  actionable coaching feedback. The minimum viable version could work with
  just wind shifts (already captured) + BSP + rudder angle — sea state from
  the ESP32 makes it richer but isn't strictly required to start. Could
  prototype the analysis on existing session data by using TWS/TWD changes
  as the trigger instead of sea state.

---

## IDX-034: Database replication and offline analysis — Pi → test Pi / Mac

- **Date captured:** 2026-04-06
- **Origin:** Conversation about needing offline analysis when the boat's Pi isn't online, plus backup
- **Status:** `promoted`
- **Promoted to:** combined with IDX-038 into issue #757 (off-Pi viewing & analysis — replication + read-only mode); this entry covers Phase 0 (read-only mode) + Phase 1 (`helmlog sync` to owner-controlled hosts)
- **Related:** SQLite storage (`storage.py`), audio recording (`audio.py`), ArUco images, federation

**Description:**
Replicate the SQLite database, WAV audio files, and ArUco images from
corvopi-live to corvopi-tst and the MacBook Pro. This serves two purposes:

1. **Offline analysis** — run debrief, export, and analysis workflows on
   the Mac or test Pi without needing the boat's Pi online.
2. **Backup** — a second copy of all data protects against SD card failure
   or Pi damage on the boat.

**Design questions:**

1. **Replication mechanism** — options include:
   - `rsync` over Tailscale (simplest; cron or on-demand)
   - Litestream for continuous SQLite WAL streaming to S3 or a local target
   - Custom `helmlog sync` CLI command that copies DB + media files
   - SQLite backup API (`VACUUM INTO` or `.backup`) for consistent snapshots

2. **Media files** — `data/audio/*.wav` and any ArUco images need to sync
   alongside the database. rsync handles this naturally; Litestream only
   covers the DB.

3. **Conflict resolution** — replicas should be read-only to avoid divergence.
   Only the live Pi writes. If the Mac or test Pi need to write (e.g., adding
   notes during debrief), that's a harder problem — possibly write-back via
   the Pi's API over Tailscale.

4. **Trigger** — when does sync happen?
   - On session end (race finish / debrief complete)
   - Periodic (every N minutes while on home network)
   - Manual (`helmlog sync` command)
   - On deploy (as part of `deploy.sh`)

5. **Partial sync** — for the Mac, do we want the full history or just
   recent sessions? Full DB could be large over time. Could offer a
   `--since` flag.

6. **Security** — data in transit over Tailscale is encrypted. Data at rest
   on the Mac should follow the same data-licensing rules (boat owns its
   data, PII handling for audio/transcripts).

7. **Read-only mode** — the Mac/test Pi instance should start in a read-only
   mode that serves the web UI for analysis/export but doesn't try to
   connect to Signal K or record audio.

**Simplest starting point:**
A `helmlog sync` CLI command that runs `rsync` over Tailscale to copy
`data/logger.db`, `data/audio/`, and `data/notes/` to a target host.
Combined with a read-only mode flag (`HELMLOG_READ_ONLY=true`) that
disables Signal K, audio, and CAN readers.

**Notes:**
- *2026-04-06:* Initial capture. Motivated by wanting to do analysis on the
  Mac without the boat being online. Also provides backup against Pi
  hardware failure. rsync over Tailscale is the obvious first step — no new
  infrastructure needed. Litestream is interesting for continuous streaming
  but adds complexity. The read-only mode is important to prevent the
  replica from trying to record data or connect to instruments.

---

## IDX-035: BLE shutter-remote emulation as fallback for X4 OSC mode control

- **Date captured:** 2026-04-10
- **Origin:** Insta360 horizon-leveling investigation that landed
  `is_dual_fisheye()` and `promote_to_insv_extension()` in
  `src/helmlog/insta360.py`
- **Status:** `raw`
- **Related:** `src/helmlog/cameras.py`, `src/helmlog/insta360.py`,
  `scripts/import-matched.sh`

**Description:**
Insurance plan in case Insta360 ever changes the X4 OSC behaviour we
currently rely on. Today the X4 (firmware `DLC4_1.9.21.6_build5`) records
correct dual-fisheye 360° content via OSC `camera.startCapture` but
mislabels the file `.mp4`; we work around it by probing the stream count
with ffprobe (`is_dual_fisheye`) and renaming on disk during import
(`promote_to_insv_extension`). If a future firmware actually starts
recording flat single-lens video over OSC, no amount of `setOptions` can
fix it (we exhaustively probed every documented and undocumented option,
and `videoStitchingSupport` is hard-coded to `["none"]`). At that point
we'd need to drive the camera through a transport that inherits the
touchscreen mode the way the physical shutter button does — i.e. emulate
a Bluetooth shutter remote from the Pi.

**Reference implementation details (verified by Patrick Chwalek's
ESP32 work and the btittelbach ESPHome config — see Notes for sources):**

- Pi advertises BLE local name `Insta360 GPS Remote` (the camera's
  "Add Remote" pairing screen filters on this name)
- Service UUID: `0000ce80-0000-1000-8000-00805f9b34fb`
- Write characteristic (commands): `0000ce81-...`
- Notify characteristic (status): `0000ce82-...`
- Shutter toggle (one press = start, next press = stop, inherits
  whatever mode the touchscreen is in):
  `fc ef fe 86 00 03 01 02 00`
- Mode button: `fc ef fe 86 00 03 01 01 00`
- Power short / long: `... 00 03 01 00 00` / `... 00 03`
- No pairing/bonding crypto required after first "Add Remote" handshake;
  the camera whitelists the Pi's BLE MAC and reconnects automatically

Implementation would live in a new `src/helmlog/cameras_ble.py` sibling
module, called from the same `start_camera`/`stop_camera` entry points
in `cameras.py` selected by a `CAMERA_TRANSPORT=osc|ble` env var. Pi
side: `bleak` for GATT writes, BlueZ `btmgmt`/`bless` for advertising
the local name. Estimate: 1–2 days of work to a first cut.

**Notes:**
- *2026-04-10:* Captured during the same investigation that found the
  `.mp4` mislabelling. The actual fix turned out to be much smaller (the
  OSC recordings were always correct content with a wrong extension), so
  this BLE plan is intentionally being parked rather than built. Holding
  it here so we don't have to re-discover the protocol from scratch if
  Insta360 ever forces our hand. Sources:
  [pchwalek/insta360_ble_esp32](https://github.com/pchwalek/insta360_ble_esp32),
  [Patrick Chwalek — BLE Control of Insta360 Cameras](https://medium.com/@patrickchwalek/ble-control-of-insta360-cameras-7bf6894648a4),
  [btittelbach ESPHome X4 config](https://github.com/btittelbach/esphome_config_examples/blob/main/insta360_ble_remote_waveshare_touch169_esp32s3.yaml),
  [Hackaday: X3 BLE remote with ESP32](https://hackaday.io/project/188975-insta360-x3-ble-remote-control-with-esp32).

---

## IDX-036: Time-series foundation models (TimesFM) for predictive sailing analytics

- **Date captured:** 2026-04-12
- **Origin:** Discussion about [google-research/timesfm](https://github.com/google-research/timesfm) as a zero-shot forecaster for instrument data
- **Status:** `raw`
- **Related:** `src/helmlog/analysis/`, `src/helmlog/polar.py/`, `src/helmlog/external.py/`, IDX-012 (GRIB forecasts), IDX-031 (pluggable AI providers)

**Description:**
Integrate Google's Time-series Foundation Model (TimesFM) to provide zero-shot
forecasting, anomaly detection, and missing-value imputation for 1Hz sailing
instrument data. Unlike traditional ARIMA or RNN models, TimesFM is pre-trained
on billions of time-points and can generalize to new time-series (like boat
speed or wind direction) without per-boat training.

**Killer use cases for HelmLog:**

1. **Short-term tactical wind forecasting:** Analyze the True Wind Direction (TWD)
   time-series to forecast the next 5-15 minutes of shifts (oscillations). This
   provides a high-resolution, local tactical advantage that GRIB-based models
   (IDX-012) cannot match.
2. **Current vector forecasting:** Forecast the local current for the next 30-60
   minutes by projecting the observed current vector time-series (SOG/COG vs
   BSP/HDG) forward, potentially constrained by tide predictions from `external.py`.
3. **Performance anomaly detection:** Use TimesFM to forecast expected boat speed
   (BSP) based on current conditions (TWS, TWA) and compare it to real-time
   performance. Large deviations could trigger alerts for weed on the keel, sail
   trim issues, or sensor calibration drift.
4. **Maneuver recovery modeling:** Model the velocity recovery curve during tacks
   and gybes to predict "time to full recovery" more accurately than simple
   linear assumptions in `maneuvers.py`.

**Architecture:**
Since TimesFM is a transformer-based model, it is likely too heavy for the Pi 5's
CPU during active racing. The implementation should leverage the **IDX-031
Pluggable AI Provider** architecture:
- **On-water (Offline):** Fall back to simpler heuristic models or wait for
  connectivity.
- **Post-sail (Debrief):** Offload the time-series forecasting to a local Mac
  or a cloud-hosted TimesFM instance over Tailscale/WiFi.

**Notes:**
- *2026-04-12:* Initial capture. TimesFM's zero-shot capability is the key
  enabler here — it avoids the "cold start" problem where a new boat has no
  history to train on. The model's ability to handle noisy 1Hz data with
  varying frequencies makes it well-suited for the intermittent nature of
  sailing instrument logs.

---

## IDX-037: Race start management — countdown, flags, line pings, and OCS awareness

- **Date captured:** 2026-04-17
- **Origin:** Conversation about expanding HelmLog into a capable pre-start tool
  that the crew actually uses on the water
- **Status:** `promoted`
- **Promoted to:** #644
- **Related:** `races.py`, `triggers.py`, `sk_reader.py`, `web.py`, IDX-011
  (write derived data back to B&G), IDX-013 (mark-anchored threads),
  IDX-021 (on-device audio keyword detection), IDX-032 (ESP32 sea state),
  `docs/domain.md`

**Description:**
Turn HelmLog into a full pre-start assistant, not just a passive logger. The
pre-start is the highest-leverage minute of a race and is currently under-served
by the app. This idea bundles a set of tightly related features that together
make HelmLog the helm/tactician's primary start tool.

**Core components:**

1. **Countdown timer.** Standard RRS sequences (5-4-1-0, 3-2-1-0, match-racing
   dial-up, team-race rolling). Large, glanceable UI suitable for a cockpit
   screen. Audible callouts ("one minute", "thirty", "ten-nine-eight…") through
   the cockpit speaker. Sync/reset via a single tap.

2. **Flag display.** Show the flags that *should* currently be up on the RC
   boat given the selected sequence and elapsed time — class flag, P / I / Z /
   U / Black, and the transitions between them. Also: AP (postponement),
   X (individual recall), First Substitute (general recall), N (abandonment).
   A glance at the phone tells the crew where we are in the sequence without
   needing binoculars on the committee boat.

3. **Clock sync.** One-tap "sync to gun" — when the crew hears the warning
   signal, tap to align HelmLog's countdown to the RC's actual signal. Corrects
   for NTP drift, RC timer drift, and any lag in the sequence. Optional:
   listen for horn blasts via microphone (tie-in to IDX-021 Hailo NPU audio
   keyword detection) to auto-sync without a tap.

4. **Rolling starts.** Support for team-race and match-race formats where
   multiple starts are stacked back-to-back. Countdown rolls into the next
   sequence automatically. Also support "practice starts" that don't create
   a session but do capture line pings and start quality.

5. **General recall handling.** One-tap "general recall" rewinds to the
   warning signal and restarts the sequence (with the First Sub flag
   convention). Keeps the line pings intact across the restart.

6. **Line pings — boat end and pin end.** While circling near the line, tap
   to capture GPS coordinates at the boat end (RC boat) and pin end. Store
   with the session. Compute derived values in real time:
   - **Line bias:** which end is favoured given current TWD
   - **Distance to line** (perpendicular)
   - **Time to line** at current BSP/SOG
   - **Time-to-burn** (seconds to burn off if we arrive early)
   - **Line sight** bearing for a visual transit
   Line coordinates become part of the session's metadata — available to the
   debrief UI, exported alongside the track, and anchor-able from IDX-013
   co-op threads ("comment on the line bias for this start").

7. **OCS detection.** After the gun, compare the boat's track to the line
   segment and flag whether we crossed early (OCS) or clean. Surface as
   session metadata and a debrief overlay.

8. **Start quality scorecard.** Post-start, compute and display: distance
   from favoured end at gun, speed at gun vs target, time-on-distance error,
   and whether we were OCS or clean. Historical trend across sessions so
   the crew can track improvement.

**Things worth considering:**

- **Course area / start line as part of `races.py` RaceConfig** — the line
  pings should extend the existing race model, not sit in a separate store.
- **Broadcast to crew devices.** The helm's phone runs the timer; the tactician
  and bow also see it. HelmLog's web app already works on multiple phones
  on the boat's WiFi — reuse that.
- **Write to B&G network (IDX-011 tie-in).** Publish start-relevant derived
  values (distance to line, time to burn, line bias) as Signal K paths so
  they show up on the mast/cockpit displays without a phone in hand.
- **Postponement handling.** AP flag → pause countdown. AP over A / AP over H
  → clear the line pings? Or keep? Probably keep and let user reset.
- **Fleet-specific sequence libraries.** Save named sequences per fleet
  (e.g., "PHRF Thursday", "J/70 Worlds"). One tap to load the right
  sequence for the day.
- **Audio gun detection for auto-sync.** Tie to IDX-021. The Pi is already
  recording debrief audio — a sharp horn blast at T-5, T-4, T-1, T-0 is a
  low-effort classification target and eliminates the tap-to-sync UX.
- **Accessibility.** Countdown screen must be readable in bright sun,
  one-handed-tappable while helming, and work offline (no internet at
  most race venues).

**Open design questions:**

- **Source of truth for the countdown.** Pi clock with NTP? Or treat the
  sync-tap as authoritative and re-derive everything from that offset? Latter
  is simpler and more honest about what we know.
- **Line ping UX while maneuvering.** The helm can't fumble with a phone while
  circling. Needs either a big-target "drop ping now" button on the home
  screen, a hardware button (pipeline tie to IDX-030 DIY sensor work), or
  a voice command.
- **Line expiry.** Should line pings expire if the RC moves the line between
  races? Probably offer a "re-ping" flow rather than auto-expire.
- **Multi-boat co-op coordination.** Can a co-op share line pings? (Subject
  to `docs/data-licensing.md` — probably yes within a co-op, as start-line
  coordinates aren't sensitive PII.)
- **Rolling start semantics.** Team racing typically uses a 3-2-1-0 with
  consecutive flights. Match racing uses 10-6-5-4-1-0 with dial-up. Need
  to decide how much of the sequence diversity to support up front.

**Notes:**
- *2026-04-17:* Initial capture. Start minimal: countdown + flag display +
  sync-tap + two line pings stored with the session. Everything else (OCS
  detection, scorecard, B&G writeback, audio gun detection, co-op sharing)
  is additive once the core loop works. The session-integration piece is
  the key differentiator vs. standalone apps like RaceQs or Sailmon — we
  can tie the start directly into the same SQLite record that owns the
  track, maneuvers, debrief audio, and thread anchors.
- *2026-04-21:* Promoted to #644. User reaffirmed scope and added two
  emphases folded into the issue: (1) **multi-class awareness** — app
  should know our class plus classes starting ahead/behind us in the day's
  sequence and roll the visible class flag forward as each start fires;
  (2) **viewer read-only mode** — crew/shore viewers see live state on
  the same race page but cannot mutate (countdown, pings, sync), likely
  gated by `auth.py` role. User framed the target as a "superset of
  RaceMaster, B&G page, and Vakaros".

---

## IDX-038: Cloud-hosted race viewer — decouple viewing from the Pi

- **Date captured:** 2026-04-27
- **Origin:** Conversation about how viewers (crew, friends, club members)
  currently can't browse past races unless the boat's Pi is online and
  reachable over Tailscale. The aspiration: turn the Pi into a pure
  data-gathering appliance and serve the historical/replay UI from
  always-on cloud infrastructure.
- **Status:** `promoted`
- **Promoted to:** combined with IDX-034 into issue #757 (off-Pi viewing & analysis — replication + read-only mode); this entry covers Phase 2 (cloud-hosted viewer, layered on the Phase 1 sync primitive)
- **Related:** **IDX-034** (Pi → Mac/test-Pi DB replication — narrower
  precursor of the same plumbing), IDX-007 (centralized-vs-federated
  tension, already settled in favor of centralized for the social feed),
  IDX-009 (ChartedSails-style cloud offload as comparison), IDX-023 (plugin
  marketplace monetization — overlaps the cost/operator-model question),
  `docs/data-licensing.md`, `docs/federation-design.md`, `web.py`,
  `storage.py`, `auth.py`, `peer_api.py`, response-cache work (#594)

**Description:**
Today HelmLog is Pi-first: `sk_reader.py`, `storage.py`, and the FastAPI
web UI all run on the same Raspberry Pi on the boat. That works for crew
during a race (no internet at sea) but is hostile to shore-side viewers —
to look at last weekend's race, the Pi has to be powered on, on a network,
and reachable via Tailscale. Friends, family, and casual club members
won't jump those hoops.

Split the system in two:

1. **Pi = data-gathering appliance.** Keeps Signal K reader, storage,
   audio/photo capture, on-board live UI (still needed at sea), maneuver
   detection. Continuously (or on a schedule when connected) replicates
   to the cloud.
2. **Cloud = always-on viewer.** Hosts session list, track replay,
   maneuver overlay, polar charts, debrief threads, race cards. Honors
   co-op roles for access control. Boat doesn't need to be online for
   viewers to browse history.

The Pi's role gets simpler in *purpose* (it's no longer the primary viewer
endpoint) but not in *code* — it still runs the live on-board UI and the
capture loop. The cloud is additional infrastructure, not a replacement.

**Design questions:**

1. **Hosting topology** — several models, each with very different
   operational and ethos implications:
   - **Single multi-tenant FastAPI** (Fly.io / Render / Railway):
     `helmlog.cloud/boats/<boat-id>/...`. Cheapest to operate, but one
     operator holds every boat's data. Tension with "boat owns its data."
   - **Per-boat cloud instance**: each owner deploys their own
     Fly/Railway/VM. Aligns with the ethos but operationally heavy for
     non-technical owners.
   - **Co-op-hosted**: each co-op runs a viewer for its member boats.
     Mirrors the existing federation trust boundary. Probably the most
     consistent with how federation/co-op auth already works.
   - **Static export per session**: generate a JAMstack-style snapshot at
     race-end, push to S3 + CloudFront. Trivially cheap. Breaks live
     features (threads, comments, ongoing debriefs, live race viewing).
   - **CDN-cached read replica**: Pi stays primary; cloud is a passive
     cache layer leveraging the response cache from #594. Simpler model;
     still requires Pi to be reachable for cache misses.

2. **What replicates to the cloud, and when?**
   - Track + maneuvers + polar + race metadata: clearly fine, no PII.
   - Debrief audio + transcripts + diarized speaker labels: PII per
     `docs/data-licensing.md`. The cloud operator becomes a processor;
     need explicit owner consent and probably an opt-in per session.
   - ArUco / camera photos: same PII concerns (faces).
   - Co-op embargoes (race-day visibility windows) must survive the
     replica — the cloud can't show data the Pi wouldn't show.
   - **Likely v1 cut:** non-PII only (track, maneuvers, polar, session
     metadata, race cards). Audio/transcripts/photos stay on the Pi
     until the consent flow exists.

3. **Sync mechanism** — heavy overlap with IDX-034:
   - **Litestream**-style continuous WAL streaming → cloud-mounted
     SQLite. Near-real-time, no app changes, but covers only the DB.
   - **rsync over Tailscale** to a cloud VM that joins the tailnet. Drop-
     dead simple, batches well, handles media files, latency is whatever
     the cron interval is.
   - **Custom `helmlog push` CLI** that the Pi runs on session-end. Best
     control over what leaves the boat (re: data licensing) but most code
     to maintain.
   - **Federation as transport** — the cloud joins the boat's federation
     mesh as a peer with `auto-accept-from-this-boat` policy. Reuses the
     existing peer protocol; cloud is just "another boat that happens to
     have a public DNS name and 100% uptime."

4. **Auth and access control.** The co-op model already has
   owner/admin/crew/viewer/public roles. The cloud needs to honor them
   without becoming a separate identity system. Cleanest: cloud trusts
   the boat's Ed25519 identity (same as federation peers do), and the
   boat's auth records replicate alongside the data. A viewer logs in
   to the cloud with the same creds they use on the Pi.

5. **Live race viewing.** Shore-side viewers wanting to watch a race
   *as it happens* push the architecture toward continuous streaming
   (Litestream, websocket relay, or federation deltas). If live viewing
   is out of scope for v1, batch sync at session-end is much simpler.

6. **Federation vs. centralization.** This is the biggest tension. The
   project is built on a federated, boat-owns-its-data model. A
   centralized cloud viewer is the easiest path to "viewers don't need
   the Pi online" but pulls toward the RaceQs/Sailmon/Vakaros
   architecture. Possible reconciliations:
   - The cloud is *a* federation peer, not the only source of truth — Pi
     remains canonical and viewers can hit either.
   - Co-ops run their own clouds; there is no single
     `helmlog.cloud`.
   - The cloud is a thin proxy that read-throughs to one or more boat
     Pis when they're reachable, and serves cached data when they
     aren't (#594 cache makes this much more viable).

7. **Cost / operator model.** Who pays for the cloud, and is HelmLog
   a hosted service or open-source-only? Touches IDX-023's monetization
   discussion. Options: free tier per boat sponsored by the project, paid
   hosted plan, BYO-cloud with a one-line `fly deploy`, club-sponsored
   for member boats.

8. **What can't move to the cloud.** The on-board live UI (no internet
   at sea), the Signal K reader, audio capture, camera control. So
   "Pi as just data gathering" is more accurately "Pi as data gathering
   *plus* on-board live experience." The cloud handles **everything
   shore-side / post-race / asynchronous**.

**Simplest starting point:**
A read-only cloud replica that piggybacks on IDX-034's plumbing. Pi
rsyncs `data/logger.db` (and nothing else for v1) to a cloud VM joined
to the tailnet on session-end. The cloud runs the same FastAPI app in
`HELMLOG_READ_ONLY=true` mode (already proposed in IDX-034) behind a
single shared password. No audio, no photos, no live updates, no
multi-tenant isolation. Get viewers a URL they can hit when the boat is
off-network; iterate on auth, real-time, and PII-bearing data later.

**Notes:**
- *2026-04-27:* Initial capture. Strong overlap with IDX-034 — they
  share the replication plumbing and read-only mode. The differences are
  (a) IDX-034 targets the owner's other devices for personal analysis,
  (b) this targets a public/co-op viewing surface for crew and friends,
  and (c) this raises hosting/auth/PII/federation questions IDX-034
  sidesteps because both endpoints are owner-controlled. Consider this
  the "phase 2" of IDX-034: once the replication primitive exists,
  pointing it at a cloud target is mostly a deployment question; the
  hard parts that remain are co-op auth honoring, PII gating, and the
  centralization-vs-federation philosophical call.
- The federation-as-transport option is attractive — it means the cloud
  isn't a special case in the codebase, just a peer with unusually high
  uptime. Worth a closer look before committing to a bespoke sync path.

---

## IDX-039: GoPro GPS-time auto-sync — zero-friction video-to-race alignment

- **Date captured:** 2026-06-07
- **Origin:** Conversation about whether HelmLog race data (GPS-timestamped via
  Signal K) could be used to sync GoPro video, which embeds GPS UTC timestamps
  in its GPMF metadata track.
- **Status:** `raw`
- **Related:** **IDX-009** (self-hosted video upload + auto-sync from file
  metadata — broader, lower-precision precursor), `video.py`, `youtube.py`,
  `insta360.py`, `storage.py` (`positions` table), `helmlog link-video` CLI,
  YouTube auto-association pipeline

**Description:**
GoPro cameras (Hero 9 and later) embed a GPMF (GoPro Metadata Format) telemetry
track directly in the `.MP4` file. This track includes GPS coordinates and UTC
timestamps at up to 18 Hz. HelmLog's `positions` table is also UTC-referenced,
with timestamps derived from the GNSS sentence received via Signal K (PGN 126992
/ `$GNRMC`). Because both clocks are disciplined to the same GPS constellation,
they agree to well under a second without any manual calibration.

This makes fully automatic, sub-second video-to-race sync achievable:

1. **Extract GPMF GPS track** from the `.MP4` — `ffmpeg`, `gopro-telemetry`, or
   `gpmf-parser` (Python) can pull the GPS samples and their UTC timestamps.
2. **Match to a HelmLog session** — find the race whose `positions` time range
   and lat/lon track overlap the GoPro GPS track. Overlap threshold can be loose
   (e.g. ≥ 60 s within 500 m) to handle the boat being stationary at the dock
   while the camera is recording.
3. **Compute the sync offset** — compare GPS timestamps in both tracks; ideally
   offset ≈ 0 s (same constellation). Store as a `video_links` row using the
   existing sync-point model.
4. **Downstream features work immediately** — the replay scrubber, deep-links
   (`?t=<seconds>`), and maneuver video overlays all consume `video_links` rows;
   no further plumbing needed.

Compared to the current `helmlog link-video --sync-utc … --sync-offset …`
workflow, this eliminates the manual step of identifying a synchronisation moment
visible in both the video and the instrument record.

Compared to IDX-009 (which targets any video file and syncs from file creation
timestamp), this is GoPro-specific but an order of magnitude more accurate:
creation timestamp gives ±1 s at best; GPMF GPS time gives ±0.1 s or better.
The two ideas are complementary — IDX-009's file-creation-timestamp fallback
could still handle iPhone / GoPro-without-GPS-fix cases.

**Open design questions:**

- **Video hosting path:** Does the GoPro video still go to YouTube (and the
  `video_links` row stores a YouTube URL), or does this imply local/self-hosted
  storage? Likely YouTube for now — the sync CLI only needs a video URL to store,
  not the file itself post-analysis.
- **GPS cold-start / no-fix models:** Older GoPro models or cold-start shots may
  have no GPS track, or a track that starts several minutes into recording while
  the receiver acquires a fix. Need a graceful fallback (file-creation timestamp
  from IDX-009's approach, or manual `--sync-utc`).
- **Multi-clip sessions:** GoPro splits long recordings into ~4 GB chapters.
  Need to handle multiple `.MP4` files mapping to one session.
- **`helmlog sync-gopro` CLI vs. web upload UI:** CLI is lower-friction for a
  solo operator; a drag-and-drop upload page on `/admin` would reach crew who
  don't have terminal access.

**Notes:**
- *2026-06-07:* Initial capture. Arose from noticing that HelmLog's `ts` column
  and GoPro GPMF timestamps share the same UTC reference — the sync problem is
  essentially already solved by GPS. The main implementation work is GPMF
  extraction + session-matching heuristic. Cross-references IDX-009 which covers
  the broader self-hosted video story; this entry focuses on the GoPro-specific,
  GPS-grade precision path.
