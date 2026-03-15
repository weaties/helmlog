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
- **Status:** `raw`
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
- **Status:** `raw`
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
- **Status:** `raw`
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
