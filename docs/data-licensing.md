# Helm Log — Data Licensing Policy

## Plain English Summary

- **You own your data.** Your instrument logs, notes, and photos belong to you.
- **Reciprocal sharing.** By joining a co-op, you share your instrument data
  (wind, speed, GPS) to see everyone else's.
- **Privacy by default.** Your audio recordings, personal notes, and hard-earned
  current observations are private and never shared unless you say so.
- **Crew rights.** Any crew member can ask to have their voice or face deleted or
  scrambled from your logs at any time.
- **No spying.** We don't track non-member boats via AIS or radar.
- **Governance.** The co-op is a democracy. Big moves (like building AI current
  models or selling data) require a supermajority or unanimous vote.
- **Easy exit.** You can leave anytime. Your data stays in the co-op but becomes
  permanently anonymous ("Boat X").
- **Safety first.** This data is for performance analysis, not navigation. Don't
  hit a rock because of a shared log.

---

This document defines data ownership, access, sharing, and retention rules for all
data collected, stored, and shared through the Helm Log platform. These rules
govern both single-boat usage and co-op data sharing.

A **data co-op** is a group of boats that agree to share instrument data under this
policy. Multiple independent co-ops can exist — for example, a J/105 co-op in
Seattle, a J/105 co-op in San Francisco, and a J/80 co-op in Seattle are all
separate co-ops with independent membership, governance, and data pools. A boat may
belong to more than one co-op, subject to the per-event exclusivity rules below.

All participants must agree to this policy before joining a data co-op. Single-boat
users who do not participate in any co-op are bound only by the ownership and
audio/PII sections.

---

## 1. Data Ownership

### Instrument data

All instrument data — positions, wind, speed, heading, depth, heel, pitch, and
derived metrics — is owned by the **boat owner or instance operator** (the person
who administers the Helm Log instance on that boat's hardware).

The boat owner has full rights to export, share, delete, or restrict access to
their instrument data. The boat owner controls who their data is shared with and
can revoke any person's access at any time.

#### AIS and proximity data exclusion

Instrument systems may passively receive AIS (Automatic Identification System)
transmissions or other proximity data from nearby boats. Helm Log **must not
capture, store, or share** AIS positions or identifying information of other
boats. The co-op exists to share data that members voluntarily contribute — it
must not become a surveillance tool for non-participating boats.

This exclusion applies to any passively received data that identifies or tracks
another vessel, regardless of the source (AIS, radar targets, DSC, etc.).

### Audio and voice data

Audio recordings are personally identifiable information (PII). Under GDPR, CCPA,
and equivalent privacy laws, **speakers retain personal rights over their voice data**
regardless of who operates the recording hardware.

This means:

- Any crew member — active or former — can request deletion or anonymization of
  audio recordings containing their voice, without needing to leave the crew.
  A crew member may simply not want a specific conversation on the record.
- **Anonymization** (voice scrambling, redaction, or speaker removal from the
  audio) is an acceptable alternative to full deletion, provided the speaker's
  voice is no longer identifiable in the resulting recording.
- Transcripts derived from audio inherit the same status — a speaker can request
  deletion or anonymization of transcript segments attributed to them.
- Diarized (speaker-labeled) transcripts carry stronger PII obligations than
  unlabeled transcripts. Anonymizing a diarized transcript means removing or
  replacing the speaker label and redacting identifiable content.

The boat owner controls access to audio and transcripts, but cannot override a
speaker's deletion or anonymization request for their own voice data.

### Photos and images

Photos captured as part of session notes, on-board cameras, or image-based
detection (e.g., sail shape analysis) are owned by the boat and are **boat-private
by default**. Photos that contain identifiable people are PII — the same deletion
and anonymization rights that apply to audio (above) apply to identifiable photos.

### Notes and annotations

Session notes, race comments, and annotations are owned by the boat and are
boat-private by default.

### YouTube and external video

YouTube videos linked to sessions are hosted on YouTube and governed by YouTube's
terms of service. The Helm Log stores only **metadata** (video ID, title, sync
points) — not the video content itself. The boat owner controls which videos are
linked and can unlink a video at any time. Unlinking removes the metadata from the
logger but does not affect the video on YouTube.

If a boat departs the co-op or requests data deletion, YouTube video metadata
linked to that boat's sessions is included in the deletion scope (see Section 5).
The actual YouTube video remains on YouTube under the uploader's control.

### Tide and current observations

Helm Log captures publicly available **tide and current predictions** from
sources like NOAA (reference station predictions, harmonic constants). These
public predictions are not proprietary and carry no ownership restrictions.

However, as a boat sails in a specific area over time, Helm Log can derive
**observed current data** — actual current vectors computed by comparing
instrument speed (BSP/heading) against ground track (SOG/COG). These
observations reveal how real currents differ from NOAA predictions at specific
locations, times, and tide phases. This is **hard-earned local knowledge** that
represents genuine competitive advantage built up over seasons of racing.

Observed current data is:

- **Owned by the boat** — it is derived from the boat's own instrument data
- **Boat-private by default** — it is never shared with the co-op unless the
  boat owner explicitly opts in
- **Temporally rich** — current observations are most valuable in the context of
  their tide cycle phase. An observation from three years ago is highly relevant
  today if the tide conditions match, because tidal patterns are cyclical
- **Geographically specific** — current knowledge for one sailing area has no
  value in another

For co-op current models built from multiple boats' observations, see Section 8
(AI, Machine Learning, and Derived Models).

### Race results

Race results occupy a unique position: they are entered into the logger by the
boat owner, but the same information is independently published by the **organizing
authority** (yacht club, class association, or regatta committee) as part of the
official event record.

For the purposes of this policy, "race results" means **officially scored rank,
finishing time, and corrected time** as published by the organizing authority — not
high-resolution GPS tracks, instrument telemetry, or other session data from that
race. The full instrument data for a race session is governed by the instrument
data rules above.

Because race results are **publicly available data** published by third parties:

- Race results entered into the logger are owned by the boat, but the boat owner
  cannot claim exclusive rights over publicly published finishing positions, times,
  or scores
- On departure or deletion, a boat's race results are **anonymized** in the co-op
  (displayed as "Boat X") but not removed, since the same data is publicly
  available from the organizing authority
- Race results are included in co-op shared data by default (they are already
  public information)
- Annotations, comments, or notes attached to race results remain boat-private
  (they are not public data)

#### Full-fleet result imports

A co-op may vote to import **full-fleet official results** from the organizing
authority, which includes finishing data for boats that are not co-op members. The
co-op must comply with any licensing terms imposed by the organizing authority or
race management software provider (e.g., Sailwave, Yacht Scoring) on the use of
their published results.

**Liability for result imports rests with the co-op, not the platform.** The
co-op admin who initiates the import is responsible for verifying that the
source data's license permits the intended use. Helm Log provides the import
mechanism but does not warrant that any specific co-op's use of imported results
complies with third-party terms.

Non-member boats appear in imported results **only as their official scored
finish** (rank, time, corrected time, and boat name as published by the organizing
authority). No instrument data, GPS tracks, or other session data is captured for
non-member boats. The co-op's only view of a non-member is what the organizing
authority has already made public.

### Coach and combined datasets

When a coach accesses multiple boats' data for analysis, the coach holds a
**delegated access license**, not ownership. The following rules apply:

- The coach may **view and analyze** data within the Helm Log platform but may
  **not bulk-export or download** co-op data from other boats
- The coach may **not aggregate** multiple boats' data into a derived dataset that
  the coach retains independently of the platform
- Coach access grants are **time-limited** and must be renewed each season (or at
  an interval set by the boat owner). There is no perpetual coach access
- When a coaching engagement ends — whether by expiration, revocation, or mutual
  agreement — the coach must **delete all data** from that boat. This is mandatory,
  not optional
- **Derivative works** — any reports, summaries, screenshots, spreadsheets, or
  other materials the coach creates using co-op or boat data are subject to the
  same recall and deletion obligations. The coach may not retain derivative works
  after access ends
- The boat owner can revoke a coach's access at any time, which triggers immediate
  deletion obligations (including derivative works)

---

## 2. Data Sharing — The Co-op Model

### Overview

A Helm Log data co-op is a **reciprocal sharing arrangement** for instrument
data. Members share their race data with the co-op and gain access to other
members' data in return.

### What is shared by default

When a boat joins a co-op, the following data is shared with all co-op members:

- **Instrument data**: positions, wind (TWS, TWA, TWD, AWS, AWA), boat speed (BSP,
  SOG, COG), heading, depth, heel, pitch
- **Session metadata**: date, duration, race name, venue
- **Derived metrics**: VMG, polar performance percentage, tacking angles
- **Race results**: finishing positions, times, scores (publicly available data)

### What is NOT shared by default

The following data remains **boat-private** and is never shared unless the boat
owner explicitly opts in:

- Audio recordings
- Transcripts (full text and segments)
- Photos and images
- Session notes and annotations
- Race comments and threaded discussions
- Crew roster and role assignments
- Sail selections and tuning notes
- YouTube video links and metadata
- Observed current and tide data (derived local knowledge)

### No bulk export of co-op data

Co-op data is available for **in-platform viewing and comparison only**. Members
may not bulk-export, scrape, or programmatically extract other boats' raw data
from the co-op. Each boat can export its own data freely, but co-op data from
other boats is view-only within the Helm Log interface.

This restriction exists to prevent data extraction attacks (join, download
everything, leave) and to preserve the collective value of the co-op dataset.

### Explicit sharing with coaches and tuning partners

Boat owners can explicitly share private data (notes, transcripts, audio, photos)
with:

- **Designated coaches**: identified by user account, granted access to specific
  sessions or all sessions. Coach access is time-limited and must be renewed each
  season (see Section 1). The boat owner can revoke a coach's access at any time.
- **Tuning partners**: another boat in the co-op granted reciprocal access to
  private data for collaborative tuning work

Explicit sharing is per-boat, revocable at any time, and does not extend to the
broader co-op.

### Contribution threshold

To join the co-op and access co-op data, a boat must share **at least one race
session**. There is no ongoing minimum contribution requirement.

This low threshold maximizes adoption. In a one-design fleet where everyone races
together, social dynamics are a more effective incentive than technical enforcement.

---

## 3. Co-op Membership and Governance

### Co-op identity

Each co-op is an independent group defined by its membership. There is no global
co-op — each co-op has its own members, governance, and data pool.

### Boat representatives

Each boat in the co-op has a **designated representative** — typically the boat
owner — who is the voting member for governance decisions. One person, one vote per
boat, regardless of crew size.

#### Club and institutional ownership

When a single entity (e.g., a sailing club, corporate team, or charter company)
owns multiple boats, the following rules apply:

- Each boat still gets **one vote** in co-op governance
- The owning entity designates a representative for each boat (this may be the same
  person for multiple boats, or different people)
- To prevent bloc voting from dominating a small co-op, a single entity's boats
  collectively hold **no more than 1/3 of total votes** in any governance decision.
  If one entity's boats exceed 1/3 of co-op membership, their excess votes are
  excluded from the count
- **Single-entity co-ops**: when all boats in a co-op are owned by the same entity
  (e.g., a sailing club's fleet), the vote-capping rule does not apply — the entity
  has full governance control. However, if independently-owned boats later join, the
  1/3 cap takes effect immediately for the original entity's boats

### Co-op administration

#### Admin role

Co-op admins handle day-to-day operations: approving new members, managing
settings, and enforcing policy. The admin role is **not** a governance override —
admins are subject to the same voting rules as any other member.

#### Admin selection

- During **bootstrap phase** (fewer than 5 boats), the co-op founder serves as
  admin
- Once the co-op exits bootstrap, admins are **elected by simple majority** of
  boat representatives
- Admin terms last **one year**, with no term limits. Admins may be re-elected
- There may be more than one admin

#### Admin removal

An admin can be removed by the same **supermajority (2/3) vote** used for
expulsion. Admin removal does not affect the person's co-op membership — they
remain a member, just no longer an admin.

### Joining

Any boat running a Helm Log instance can request to join a co-op. The platform
is fully functional without co-op membership — joining is a choice, not a
requirement. Joining requires:

1. Agreeing to this data licensing policy
2. Sharing at least one race session
3. Acceptance by a co-op admin

#### Disclosure of active agreements

Before joining, the co-op must disclose to the prospective member all active:

- **Commercial agreements** (Section 9) — what data or derivatives are being
  commercialized, by whom, and the revenue-sharing terms
- **ML projects** (Section 8) — what models are being trained, on what data,
  and what opt-out rights exist
- **Current model projects** (Section 8) — which geographic areas have active
  current models and which members have contributed
- **Cross-co-op data sharing agreements** (Section 6) — which other co-ops
  have access to this co-op's data

Each co-op should maintain a **co-op charter** (see `docs/co-op-charter-template.md`)
that summarizes its specific rules, active agreements, and admin roster in a
human-readable format. The charter is presented to prospective members as part of
the join flow.

By joining, the new member **accepts all active agreements as disclosed**. Their
contributed data becomes subject to those agreements from the moment it enters
the co-op. There is no grace period or retroactive carve-out — the member had
full information before joining and chose to participate.

If a prospective member objects to an active agreement, they may choose not to
join. They can still use Helm Log as a standalone platform without co-op
membership.

#### Bootstrap phase

When a co-op has **fewer than 5 member boats**, it operates in bootstrap phase:

- Admission is by admin approval only (no vote required)
- Expulsion requires **unanimous vote** of all other members (since 2/3 of a tiny
  group is effectively everyone)
- Once the co-op reaches 5 members, standard governance rules apply

### Voluntary departure

A boat may leave the co-op at any time. On departure:

- The boat's server-side co-op access is revoked immediately
- The departing boat's historical data is immediately **anonymized** in co-op
  comparisons (displayed as "Boat X" rather than the actual boat name) but remains
  accessible to other co-op members in anonymized form
- During a **30-day grace period**, the departure can be reversed and the boat's
  identity restored
- After 30 days, anonymization becomes **permanent** — the mapping between "Boat X"
  and the real identity is deleted and can no longer be reversed

### Expulsion

The co-op may vote to remove a member. The process is:

1. **Initiation**: any co-op member's representative may propose expulsion with a
   stated reason
2. **Vote**: a **supermajority (2/3) of boat representatives** must vote in favor
   of removal
3. **30-day notice**: the member is notified. During this period, their data is
   **suppressed** from co-op views (not visible to other members) but they retain
   read access to co-op data
4. **Appeal**: during the 30-day notice period, the member may make their case to
   the co-op. A new supermajority vote can reverse the expulsion decision
5. **Expulsion takes effect**: if the vote stands after 30 days:
   - Server-side co-op access is revoked
   - The expelled member's **license to use co-op data is revoked** — they must
     delete any co-op data in their possession (this includes local copies)
   - The expelled boat's historical data is **anonymized** in the co-op (same
     treatment as voluntary departure)

### Post-expulsion data contribution

After expulsion, the expelled boat's data remains on their own local instance —
expulsion only affects co-op access, not local data. If the expelled boat is
re-admitted to the co-op (see below), they can resume sharing and their locally
stored sessions become available to the co-op again.

During expulsion, the boat **cannot contribute new data** to the co-op. Any
sessions recorded during this period remain local until re-admission.

### Inactive co-ops

If a co-op has **no governance activity** (no votes, no admin actions, no new
members) for **2 consecutive years**, it enters **dormant status**:

- The co-op's data is **frozen** — preserved as-is but no new data is accepted
- Member access to co-op data continues in read-only mode
- Any member may **reactivate** the co-op by proposing a governance action (e.g.,
  electing an admin), which triggers a 30-day response window
- If no member acts to reactivate within 30 days of a reactivation proposal, the
  co-op is **dissolved**: all data is anonymized, identity mappings are deleted,
  and the co-op ceases to exist
- Individual boats retain their own local data regardless of co-op dissolution

#### Minimum viable co-op

If a co-op's membership drops to **fewer than 3 boats**, it enters dormant status
immediately regardless of activity level. A co-op of 1–2 boats does not provide
meaningful reciprocal value, and the remaining members should not retain access
to the anonymized historical data of all the boats that departed. The same
reactivation and dissolution rules apply — if membership returns to 3 or more
within the 30-day window, the co-op resumes normal operation.

### Re-entry after expulsion

An expelled member may re-apply to the co-op at any time. Re-admission requires
the same **supermajority (2/3) vote** as expulsion.

---

## 4. Crew Access and Departure

### Active crew

The boat owner controls which crew members have access to the boat's Helm Log
instance and what level of access they hold (view, edit, admin).

### Per-recording deletion

A crew member does **not** need to leave the crew to request deletion or
anonymization of a specific recording. If a crew member was recorded discussing
something they don't want on the record, they can request removal of that specific
audio and its associated transcript at any time, per Section 1.

### Crew departure

When a crew member leaves the boat, the **boat owner decides** what happens to
that crew member's access:

- **Retain read-only access**: the former crew member can view sessions they
  participated in but cannot modify or export data
- **Retain full access**: the former crew member keeps the same access to sessions
  they were part of, including export
- **Revoke access**: all access to the boat's data is removed

The boat owner can change this decision at any time.

### Audio and photo deletion rights

Regardless of the boat owner's departure policy, a former crew member always
retains the right to request deletion or anonymization of audio recordings and
identifiable photos containing them, per Section 1.

---

## 5. Data Retention and Deletion

### Retention

Data is retained indefinitely by default on the boat's local Helm Log instance.
There is no automatic expiration.

### Deletion requests

Any boat owner may request **full deletion** of all their data from the co-op and
any instances that synced it. Deletion requests are subject to a **30-day grace
period** before execution. This prevents impulsive data loss and allows time to
reconsider.

During the grace period:

- The boat's data is **suppressed** — hidden from co-op views but not yet deleted
- The request can be cancelled at any time, which restores visibility
- After 30 days, all identifiable data is permanently deleted from co-op systems

### What deletion covers

- All instrument data, session metadata, and derived metrics associated with the
  boat
- Any audio, transcripts, photos, notes, or annotations that were explicitly shared
- YouTube video metadata linked to the boat's sessions (the actual YouTube videos
  are unaffected — they remain on YouTube under the uploader's control)
- The boat's identity in co-op comparisons (replaced with anonymized placeholder)
- Race result annotations and comments (the results themselves remain as public
  data, anonymized as "Boat X")

### What deletion does not cover

- Other boats' data that happened to include the deleted boat in co-op comparisons
  (those comparisons are anonymized, not removed)
- Aggregated or statistical data that cannot be attributed to a specific boat
- YouTube videos themselves (hosted on YouTube, not controlled by the logger)

---

## 6. Cross-Co-op Data Boundaries

### No cross-co-op aggregation

Each co-op's data pool is **independent and isolated**. Members who belong to
multiple co-ops may not aggregate data across co-ops into a combined dataset.

Specifically:

- A member of both Co-op A and Co-op B may **not** merge, join, or cross-reference
  data from A and B into a single dataset or analysis
- This applies to both manual and automated aggregation
- A boat's **own data** is exempt — a boat owner can always use their own data
  across any context, since they own it

### Cross-co-op data sharing agreements

Two or more co-ops may choose to share data with each other. This requires:

1. A **supermajority (2/3) vote** in **each** participating co-op
2. A written agreement specifying what data is shared, for what purpose, and for
   how long
3. Either co-op may **withdraw** from the agreement at any time by supermajority
   vote, at which point cross-shared data must be deleted from the withdrawing
   co-op's systems

Cross-co-op agreements do not merge co-ops — each retains independent governance,
membership, and the right to withdraw.

### Dual membership and per-event exclusivity

A boat may belong to multiple co-ops. However, when two or more of a boat's
co-ops participate in the **same event** (race, regatta, or series), the boat
must designate **one co-op** to receive their session data for that event. The
same session data may not be contributed to multiple co-ops.

This rule:

- **Prevents data duplication** across co-ops for the same event
- **Eliminates proxy aggregation** — two co-ops cannot reconstruct the same
  race from overlapping members' data
- **Applies per-session** — a boat can contribute Saturday's race to Co-op A
  and Wednesday's race to Co-op B if the co-ops don't overlap for those events
- **Does not restrict non-overlapping events** — if only one of the boat's
  co-ops participates in a given event, no choice is needed

#### Transparency

Dual membership must be disclosed:

- When joining a second co-op, the boat must inform **both co-ops** of the
  dual membership
- Each co-op may set its own policy on whether dual membership is permitted.
  A co-op may choose to prohibit dual membership with a specific other co-op
  (e.g., if they have competing commercial agreements) by majority vote

---

## 7. Non-Member Boats

### Principle: the co-op must not poison the well

The existence of a data co-op within a fleet must not create adverse consequences
for boats that choose not to participate. Non-members should experience racing
exactly as they would if the co-op did not exist.

### What the co-op knows about non-members

The co-op's only information about non-member boats comes from **official race
results published by the organizing authority**. This is limited to:

- Boat name (as published by the OA)
- Scored rank, finishing time, and corrected time
- Class, sail number, and other regatta registration data

The co-op has **no instrument data, GPS tracks, performance metrics, or session
data** for non-member boats.

### What the co-op must NOT capture about non-members

- AIS positions or tracks (see Section 1, AIS exclusion)
- Radar targets or other proximity-derived position data
- Any data that would allow the co-op to reconstruct a non-member's race
  performance beyond what the organizing authority publishes

### Non-member removal requests

Non-member boats **cannot request removal** of their data from the co-op, because
the only non-member data in the co-op is official race results that the organizing
authority has already published. The co-op is not the source of this data — the
organizing authority is.

If a non-member has concerns about their race results being published, they should
address those concerns with the organizing authority.

### Joining the co-op

A non-member boat that wants to see co-op data (including how co-op members
performed relative to them) can join the co-op by meeting the standard membership
requirements in Section 3.

---

## 8. AI, Machine Learning, and Derived Models

### Individual boat data

A boat owner may use **their own data** for any purpose, including training machine
learning models, building performance predictors, or developing automated systems.
The boat owner's data is theirs — no restrictions apply beyond the PII protections
in Section 1.

### Co-op data and ML

Co-op data may **not** be used for machine learning or model training by default.
To use co-op data for ML, the following conditions must all be met:

1. **Supermajority (2/3) vote** of co-op boat representatives approving the
   specific ML project
2. The vote must specify:
   - What data will be used for training
   - What kind of model will be built
   - Who will build and maintain the model
   - How the model will be used
3. The resulting model and any derivatives are **owned by the co-op**, not by any
   individual member or external party
4. The co-op is responsible for **maintaining, hosting, and governing** the model
   — it cannot be handed off to a third party without a new supermajority vote
5. Individual members may **opt out** of having their data included in ML training.
   Opting out does not affect their co-op membership or access to co-op data

### Current and tide models — elevated governance

Current and tide models built from co-op data are among the most competitively
sensitive derivatives the co-op can produce. A high-resolution current model for
a specific racing area can fundamentally change competitive outcomes. Because of
this sensitivity, current models require **stricter governance** than general ML
projects:

1. **Unanimous consent** of all co-op boat representatives is required to
   initiate a co-op current model project (not the standard 2/3 supermajority).
   Every member must agree, because every member's local knowledge is at stake
2. Current models must be **scoped to a specific geographic area** (e.g.,
   "Elliott Bay," "SF Bay central," "Shilshole to West Point"). A co-op cannot
   build a single undifferentiated "all waters" current model — each area is a
   separate project requiring its own vote
3. Members may **opt out per geographic area** — a member might contribute data
   for one sailing area but not another, even within the same co-op
4. The resulting current model is **owned by the co-op** and available to all
   members (including those who opted out of contributing — the incentive to
   contribute is that the model improves with more data, not that access is
   gated)
5. A member may combine the co-op current model with their **own private
   observations** for their own use. This is not a derivative work violation —
   it is simply a member using co-op resources plus their own data to race
   better. However, if the member attempts to **extract, sell, or distribute**
   the combined result, that falls under the commercial use rules in Section 9

#### Competitive incentive for non-members

A co-op current model creates a natural incentive for non-member boats to join:
the model gets better with more boats contributing data across the sailing area,
and members benefit from collective knowledge that no single boat could build
alone. This incentive is intentional — it grows the co-op's value without
coercing participation.

### Prohibitions

- No member may use co-op data to train models **without co-op approval**
- No member may sell, license, or provide co-op data to a third party for ML
  training purposes
- Coaches and tuning partners may **not** use their delegated access to train
  models on co-op data
- Models trained on co-op data may **not** be sold or commercially licensed
  without a separate supermajority vote specifically authorizing commercial use

---

## 9. Commercial Use and Value

### Non-commercial by default

Co-op data and any models, analytics, or derivatives produced from co-op data are
**non-commercial by default**. They exist for the benefit of co-op members.

### Commercial use requires co-op approval

Any commercial use of co-op data or its derivatives — including but not limited to
selling access, licensing to third parties (e.g., sail manufacturers, yacht
designers, analytics companies), or building commercial products — requires:

1. A **supermajority (2/3) vote** of co-op boat representatives
2. A clear definition of what is being commercialized and by whom
3. A **revenue-sharing agreement** that returns value to contributing boats

### Value stays with the co-op

The co-op dataset, its derivatives, and the value they generate belong to the
co-op that sourced the data. No individual member, admin, coach, or external party
may capture the value of the co-op dataset for private benefit without co-op
approval.

This principle applies regardless of who performs the technical work of building
analytics, models, or products on top of co-op data.

### Revenue distribution

If the co-op votes to commercialize data or derivatives, revenue is distributed to
contributing boats. The specific distribution formula is determined by the co-op at
the time of the commercial agreement and must be included in the vote.

---

## 10. Dataset Representativeness

### No guarantee of balance

The co-op dataset reflects the boats that choose to participate and the sessions
they choose to share. Better-resourced programs with higher-quality sensors, more
sessions, and cleaner data will be disproportionately represented.

The co-op makes **no guarantee** that its dataset is representative, balanced, or
free from systematic bias. Members and any approved ML projects should account for
this when drawing conclusions from co-op data.

### Anonymization limitations in small datasets

In small co-ops (fewer than ~10 boats), anonymization (replacing a boat's name
with "Boat X") **does not guarantee de-identification**. GPS tracks, wind data,
and racing patterns may be unique enough that knowledgeable fleet members can
deduce a boat's identity from the data alone. Members should be aware that
anonymization provides identity protection against casual inspection, not against
determined analysis by someone with fleet knowledge.

---

## 11. Liability and Warranty

### No warranty

All data shared through the co-op is provided **"as-is" without warranty of any
kind**, express or implied, including but not limited to warranties of accuracy,
completeness, reliability, or fitness for a particular purpose.

### No liability for use

Neither the co-op, nor individual members, nor the Helm Log platform are liable
for any tactical errors, navigational decisions, groundings, equipment failures,
collisions, personal injury, or any other consequence resulting from the use of
shared data. Members use co-op data **at their own risk**.

### No guarantee of availability

The co-op does not guarantee continuous access to shared data. Data may become
unavailable due to member departures, deletions, technical failures, or co-op
dissolution.

---

## 12. Technical Requirements

This policy requires the following technical capabilities in the Helm Log
codebase:

| Requirement | Purpose |
|---|---|
| `data_sharing_consent` table | Record each boat's agreement to this policy and co-op membership status |
| Multi-co-op support | A boat can belong to multiple co-ops; each co-op has independent membership and data pools |
| Per-session sharing flags | Mark individual sessions as co-op-shared, coach-shared, or private |
| Boat-level identity in auth | Boats are first-class entities with owners, designated representatives, crew, and sharing posture |
| Club/multi-boat entity support | Track which boats belong to the same owning entity for vote-capping rules |
| Coach/tuning-partner ACLs | Time-limited, revocable access grants with mandatory deletion on expiration |
| No-bulk-export enforcement | Co-op data viewable in-platform only; API and export restricted to own-boat data |
| Reversible anonymization | Replace boat identity with "Boat X" in co-op comparisons; retain mapping for 30-day reversal window, then permanently delete mapping |
| Audio anonymization | Voice scrambling / redaction as an alternative to full deletion |
| Photo PII handling | Deletion or anonymization of identifiable photos on request |
| Data suppression (soft delete) | Hide (but preserve) a boat's data during 30-day grace periods; data remains in DB but is excluded from all queries and views |
| Permanent deletion (hard delete) | Irreversibly purge data from the database after grace period expiration; no recovery possible |
| Audio PII deletion | Ability to delete or anonymize specific audio segments by speaker (requires diarization) |
| Admin election tracking | Record admin elections, terms, and removal votes |
| Expulsion vote tracking | Record votes (by boat representative), notice periods, and appeal outcomes |
| YouTube metadata cleanup | Remove linked video metadata on boat departure/deletion |
| Cross-co-op isolation | Enforce data pool boundaries; prevent cross-co-op queries or exports |
| ML opt-out flag | Per-boat flag to exclude data from approved ML training projects |
| ML project governance | Record ML project proposals, votes, model ownership, and opt-outs |
| Commercial use tracking | Record commercial agreements, votes, and revenue distribution |
| Audit logging | Log all co-op data access (who viewed which session, when) to detect extraction patterns; automatically freeze co-op access and alert admin when anomalous access is detected (e.g., 50+ session views per minute) |
| Co-op dormancy tracking | Track last governance activity date; trigger dormant status after 2 years of inactivity |
| AIS data filtering | Exclude AIS and proximity data from other vessels during capture; never store non-member tracking data |
| Non-member result scoping | When importing full-fleet results, store only official scored finish data for non-members; no instrument or session data |
| OA license compliance | Track organizing authority and race management software licensing terms for imported results |
| Current observation derivation | Compute observed current vectors from BSP/heading vs SOG/COG; store as boat-private by default |
| Current model geographic scoping | Scope current/tide models to defined geographic areas; per-area opt-in/opt-out per boat |
| Current model unanimous consent | Enforce unanimous vote requirement (not 2/3) for current model projects |
| Pre-join disclosure | Present all active commercial, ML, current model, and cross-co-op agreements to prospective members before admission |
| Per-event co-op assignment | When a boat belongs to multiple co-ops, require co-op selection per session before data is shared; prevent same session from being contributed to multiple co-ops |
| Dual membership tracking | Record multi-co-op memberships; notify both co-ops; enforce co-op-level dual membership policies |

---

## 13. Software License

The Helm Log source code is licensed under the **GNU Affero General Public
License v3.0 (AGPLv3)**. See the `LICENSE` file in the repository root.

This means:

- Anyone can use, study, modify, and distribute the software
- Anyone who runs a modified version as a network service (e.g., hosting their own
  instance) must make their modified source code available under the same license
- This protects the project from proprietary forks while encouraging community
  contribution

The AGPLv3 governs the **software**. This data licensing policy governs the
**data** collected and shared through the software. They are complementary but
independent — using the software does not grant rights to other boats' data, and
participating in the data co-op does not grant rights to modify the software
beyond what the AGPLv3 allows.

---

## Document History

| Date | Change |
|---|---|
| 2026-03-07 | Initial version — ownership, co-op model, expulsion, crew departure, retention |
| 2026-03-07 | Rev 2 — multi-co-op model, photos, YouTube metadata, audio anonymization, club ownership, boat representatives, bootstrap phase, data suppression during grace periods, per-recording crew deletion rights |
| 2026-03-07 | Rev 3 — single-entity co-op governance, race results as public data, reversible anonymization on departure, post-expulsion data contribution rules |
| 2026-03-07 | Rev 4 — rebrand from "J105 Logger" to "Helm Log" (helmlog.org) |
| 2026-03-07 | Rev 5 — data extraction protections (no bulk export), coach access hardening (time-limited, no-aggregation, mandatory deletion), AI/ML governance (co-op-owned models, opt-out, commercial vote), commercial use framework (non-commercial default, revenue sharing), cross-co-op isolation, admin elections and removal, dataset bias disclaimer |
| 2026-03-07 | Rev 6 — coach derivative works prohibition, race results clarified as scored rank/time only, small-dataset anonymization disclaimer, inactive co-op dormancy/dissolution, liability shield and no-warranty clause, soft delete vs hard delete distinction, audit logging requirement |
| 2026-03-07 | Rev 7 — non-member boats section, AIS/proximity data exclusion, full-fleet result imports with OA license compliance, non-member removal policy, "do not poison the well" principle |
| 2026-03-07 | Rev 8 — tide and current observations as boat-private data type, co-op current models with unanimous consent and geographic scoping, per-area opt-out, private observation combination rights, competitive incentive for non-members |
| 2026-03-07 | Rev 9 — pre-join disclosure of active agreements, informed consent on joining, standalone platform use without co-op |
| 2026-03-07 | Rev 10 — dual co-op membership rules, per-event exclusivity (same session cannot go to multiple co-ops), dual membership disclosure and co-op-level opt-out |
| 2026-03-07 | Rev 11 — OA result import liability on co-op not platform, minimum viable co-op (3 boats) triggers dormancy |
| 2026-03-07 | Rev 12 — plain English summary at top of document, rate-limiting auto-freeze on anomalous access patterns |
| 2026-03-07 | Rev 13 — co-op charter template, charter reference in pre-join disclosure |
