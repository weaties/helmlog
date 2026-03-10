# Helm Log — Data Licensing Policy

## Table of Contents

- [Plain English Summary](#plain-english-summary)
- [Data Type Matrix](#data-type-matrix)
- [Crew Rights Summary](#crew-rights-summary)
- [Definitions](#definitions)
- [1. Data Ownership](#1-data-ownership)
- [2. Data Sharing — The Co-op Model](#2-data-sharing--the-co-op-model)
- [3. Co-op Membership and Governance](#3-co-op-membership-and-governance)
- [4. Crew Access and Departure](#4-crew-access-and-departure)
- [5. Data Retention and Deletion](#5-data-retention-and-deletion)
- [6. Cross-Co-op Data Boundaries](#6-cross-co-op-data-boundaries)
- [7. Non-Member Boats](#7-non-member-boats)
- [8. AI, Machine Learning, and Derived Models](#8-ai-machine-learning-and-derived-models)
- [9. Commercial Use and Value](#9-commercial-use-and-value)
- [10. Dataset Representativeness](#10-dataset-representativeness)
- [11. Liability and Warranty](#11-liability-and-warranty)
- [12. Technical Requirements](#12-technical-requirements)
- [13. Software License](#13-software-license)
- [Document History](#document-history)

---

## Plain English Summary

- **You own your data.** Your instrument logs, notes, and photos belong to you.
- **Reciprocal sharing.** By joining a co-op, you share your instrument data
  (wind, speed, GPS) to see everyone else's.
- **Privacy by default.** Your audio recordings, personal notes, and hard-earned
  current observations are private and never shared unless you say so.
- **Crew rights.** Any crew member can ask to have their voice, face, or likeness
  deleted or blurred from your audio and video at any time.
- **No spying.** We don't track non-member boats via AIS or radar.
- **Governance.** The co-op is a democracy. AI/ML model training and commercial
  use require a 2/3 supermajority vote. Current model sharing requires unanimous
  consent — a higher bar because current knowledge is competitively sensitive.
- **Easy exit.** You can leave anytime. Your identifiable session data is deleted
  from peer caches within 30 days. Your contributions to fleet benchmarks are
  preserved but permanently anonymized ("Boat X").
- **No gambling.** Co-op data may not be used for betting or wagering, period.
- **Your email is protected.** Email addresses used for co-op membership are PII
  with the same deletion rights as audio and photos.
- **Biometrics stay separate.** Heart rate, fatigue, and other body data require
  their own explicit consent — completely independent of instrument data sharing.
- **You can always export.** Every boat can export all of its own data in open
  formats (CSV, GPX, JSON) at any time. No lock-in.
- **Not for protest hearings.** Co-op data cannot be used as evidence in racing
  protests or redress hearings.
- **Seasonal controls.** The co-op can time-delay sharing — race during the
  series, share after it ends.
- **Anonymous benchmarking.** The co-op computes fleet-wide statistics (medians,
  percentiles, rankings) so you can see exactly where you stand — without
  revealing who is faster. You learn "your gybes cost 0.9 seconds vs the fleet
  median" without learning who gybes well.
- **Processing offload is temporary.** When heavy tasks (transcription, video
  analysis) are sent to a faster machine, no data stays on the offload host
  after processing. Your Pi is the only permanent home for your data.
- **Safety first.** This data is for performance analysis, not navigation. Don't
  hit a rock because of a shared log.

---

## Data Type Matrix

| Data Type | Owner | Default Visibility | Shared with Co-op? | Deletion Rights | Coach Access? |
|---|---|---|---|---|---|
| **Instrument data** (GPS, speed, wind, heading, depth, heel, pitch) | Boat owner | Boat-private until co-op join | Yes — per-session, when you choose to share | Anonymized on departure ("Boat X"); full deletion after 30-day grace period | Yes, per-boat opt-in only |
| **Derived metrics** (VMG, polar %, tacking angles, laylines) | Boat owner | Same as instrument data | Yes — per-session, when you choose to share | Same as instrument data | Yes, per-boat opt-in only |
| **Audio recordings** | Boat owner (speakers retain PII rights over their voice) | Boat-private | No — never shared unless boat owner explicitly opts in | Speakers can request deletion/anonymization at any time, including former crew | Only if boat owner explicitly grants session access |
| **Transcripts** | Boat owner (speakers retain PII rights) | Boat-private | No — never shared unless boat owner explicitly opts in | Same as audio — speakers can request deletion of segments attributed to them | Only if boat owner explicitly grants session access |
| **Video / camera recordings** | Boat owner (crew retain PII rights over likeness) | Boat-private; YouTube default is unlisted | No — video links/metadata are boat-private | Crew can request face-blur or removal; YouTube videos must be deleted separately by uploader | Only if boat owner explicitly grants access |
| **Photos and notes** | Boat owner | Boat-private | No — never shared unless boat owner explicitly opts in | Identifiable photos: same PII deletion rights as audio. Notes: deleted on request | Only if boat owner explicitly grants session access |
| **Crew roster / positions** | Boat owner | Boat-private | No | Crew emails scrubbed on departure or on request | No — not shared via coach access |
| **Sail selection** | Boat owner | Boat-private | No — tuning notes are never shared by default | Deleted with session on request | Only if boat owner explicitly grants session access |
| **Race results** (finish order/time) | Boat owner (but publicly available from organizing authority) | Shared — already public data | Yes — included in co-op shared data by default | Anonymized on departure ("Boat X") but not removed (public data) | Yes, visible as part of shared session data |
| **Biometrics** (heart rate, fatigue, etc.) | Individual crew member — not the boat | Person-private | No — never shared with co-op | Crew member can revoke consent and request deletion at any time, independent of crew status | No — requires separate authorization from the individual crew member, not just boat owner |
| **YouTube video links/metadata** | Boat owner | Boat-private | No — listed under "not shared" | Metadata deleted on departure; actual YouTube videos remain on YouTube | Only if boat owner explicitly grants access |
| **Email addresses** | Individual (PII) | Admin-only (owner email visible to co-op admins; crew email visible only to boat owner) | No — never shared with co-op members | Scrubbed from all records on departure or on request | No |
| **Current/tide observations** | Boat owner | Boat-private | No — hard-earned local knowledge, never shared unless boat owner explicitly opts in | Deleted on request | Only if boat owner explicitly grants access |

---

## Crew Rights Summary

Crew PII rights are independent of boat ownership and persist even after a crew
member departs. The following rights apply regardless of crew membership status:

- **Audio recordings**: Any crew member (active or former) can request deletion
  or anonymization of recordings containing their voice. Anonymization (voice
  scrambling, redaction, speaker removal) is an acceptable alternative to full
  deletion. Transcripts derived from audio inherit the same rights — speakers
  can request deletion of transcript segments attributed to them.
- **Video recordings**: Any crew member (active or former) can request that their
  identifiable appearance be face-blurred or removed from video. This applies to
  both local video files and YouTube-published footage. Video anonymization is
  technically harder than audio anonymization and may require re-processing.
- **Biometric data**: Requires explicit, per-person consent **separate from** any
  co-op or instrument data sharing agreement. Crew members can revoke biometric
  consent and request deletion at any time, independent of crew membership.
  Biometric data may not be used in crew selection or personnel decisions. Coach
  access to biometrics requires separate authorization from the individual crew
  member, not just the boat owner.
- **Email addresses**: Treated as PII. Crew emails are visible only to the boat
  owner (never to the co-op). On departure or request, crew emails are deleted
  from the boat's instance. Owner emails are scrubbed from co-op membership
  records, revocation records, and any other co-op documents on departure.

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

## Definitions

The following terms have specific meanings throughout this document:

| Term | Definition |
|---|---|
| **Boat owner** | The legal owner of the vessel, or a person explicitly delegated data authority by the legal owner. If no delegation exists, the legal owner is the boat owner for all purposes in this policy. The boat owner is always the final authority over the boat's data |
| **Instance operator** | The person who administers the Helm Log hardware (Pi) on a boat. This is usually the boat owner but may be a crew member or technician. The instance operator acts on behalf of the boat owner and cannot override the boat owner's data decisions |
| **Session** | A contiguous recording period with a defined start and end time, corresponding to a single race, practice sail, or delivery. A regatta day with multiple races produces multiple sessions. Session boundaries are set by the boat operator (manually or via race start/finish detection) |
| **Instrument data** | Raw and calibrated readings from the boat's sensors: position (GPS), wind (TWS, TWA, TWD, AWS, AWA), boat speed (BSP), speed/course over ground (SOG, COG), heading, depth, heel, pitch. Does not include audio, photos, notes, or biometric data |
| **Derived metrics** | Standardized calculations produced by the Helm Log platform from instrument data: VMG, polar performance percentage, tacking angles, layline estimates, and current vectors. Does not include proprietary analytics, tactical algorithms, or custom models built by individual boats or coaches |
| **Entity** | A single legal person or organization with common ownership or controlling interest over one or more boats. Examples: an individual owner, a sailing club, a corporate sponsor, a charter company, or a syndicate. Family members who independently own separate boats are separate entities unless they share a common ownership structure (e.g., a family trust) |
| **Platform** | The Helm Log software, including the Pi-based logger, web interface, API endpoints, and any hosted services (e.g., helmlog.org gateway). The platform is the tool; it does not own user data |
| **Co-op admin** | A boat designated to perform administrative functions (approving members, signing records) under the M-of-N multi-admin model. Admin is a role, not a rank — admins have no governance authority beyond what this policy grants |
| **Fleet benchmark** | An aggregate statistic computed across all contributing co-op boats for a specific metric and condition range — e.g., "fleet median upwind VMG in 10–12 knots TWS." Benchmarks are anonymous by construction: they contain no boat identities, no individual tracks, and no per-boat data points. A boat sees the fleet distribution and its own position within it, but not which boats produced which data points |
| **PII** | Personally identifiable information — data that can identify a specific individual. In this policy: video recordings (including 360° footage capturing crew faces and nearby boats), audio recordings, voice data, photos containing identifiable people, email addresses, biometric data, and diarized (speaker-labeled) transcripts |

---

## 1. Data Ownership

### Instrument data

All instrument data — positions, wind, speed, heading, depth, heel, pitch, and
derived metrics — is owned by the **boat owner** (see Definitions). If the
instance operator is a different person, they act on behalf of the boat owner
and cannot independently claim ownership of the data.

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

Photos captured as part of session notes or image-based detection (e.g., sail
shape analysis) are owned by the boat and are **boat-private by default**. Photos
that contain identifiable people are PII — the same deletion and anonymization
rights that apply to audio (above) apply to identifiable photos. See also "Video
recordings and the camera pipeline" below for on-board camera footage, which
carries stronger PII obligations due to continuous recording and potential YouTube
upload.

### Email addresses

Email addresses stored by Helm Log are **personally identifiable information
(PII)**. This applies to both:

- **Boat owner emails** — provided in the boat card's `owner_email` field for
  co-op membership (votes, admin transfers, emergencies)
- **Crew member emails** — used for magic-link authentication tokens to access
  the boat's Helm Log instance

Email address rules:

- **Required for co-op membership** (owner email) — enables out-of-band
  communication for votes, admin transfers, and emergencies
- **Required for crew access** (crew email) — used for auth token delivery
- **Optional for standalone use** — a boat running Helm Log without co-op
  membership or crew accounts is not required to collect emails
- **Visible only to co-op admins** by default — owner email addresses are not
  shared with other co-op members unless the co-op charter specifies otherwise.
  Crew emails are visible only to the boat owner, never to the co-op
- Subject to the same **deletion and anonymization rights** as other PII — on
  departure or deletion, a boat's owner email is scrubbed from all membership
  records, revocation records, and any other co-op documents held by other
  members' Pis. Crew emails are deleted when the crew member's access is revoked
  or when the crew member requests deletion

### Biometric and physiological data

If Helm Log is extended to capture **biometric data** — heart rate, crew fatigue
metrics, sleep data, stress indicators, or any other physiological measurements
from wearable sensors — the following rules apply:

- Biometric data is **PII owned by the individual crew member**, not the boat
- Biometric data requires **explicit, per-person consent** separate from any
  co-op membership or instrument data sharing agreement. Consenting to share
  instrument data does not consent to share biometric data
- Biometric data is **never shared with the co-op** by default — it is
  boat-private and person-private
- Biometric data may **not be used in crew selection, contract negotiations,
  or any personnel decisions**. This mirrors protections established in the
  NFL (Article 55 of the 2020 CBA) and NBA (2023 CBA) collective bargaining
  agreements, which prohibit using athlete biometric data in contract
  negotiations under penalty of fine
- Any crew member may **revoke biometric consent and request deletion** at any
  time, independent of their crew membership status
- Coaches with delegated access may **not** access biometric data unless
  separately authorized by the individual crew member (not just the boat owner)

### Notes and annotations

Session notes, race comments, and annotations are owned by the boat and are
boat-private by default.

### Video recordings and the camera pipeline

Helm Log can control on-board cameras (Insta360 X4 or similar) via the platform's
camera API. When a session starts, cameras begin recording automatically. When the
session ends, cameras stop. The resulting video files are processed through an
automated pipeline: files are transferred from the camera's SD card, stitched if
necessary (360° dual-fisheye → equirectangular), uploaded to YouTube, and linked
to the session with time-synchronization metadata.

This pipeline raises distinct data licensing concerns because **video is the
richest PII the platform handles**.

#### Video as PII

On-board video — especially 360° video — captures:

- **Crew faces and bodies** — identifiable individuals on the boat
- **Crew voices** — if the camera records audio (many action cameras do)
- **Other boats and their crew** — non-member boats in close proximity during
  starts, mark roundings, and crossings
- **Sail numbers, boat names, and identifying marks** — even distant boats may
  be identifiable in high-resolution 360° footage
- **Tactical information** — sail trim, crew positions, tacking sequences, and
  other competitive knowledge visible in the footage

Video PII obligations:

- **Crew members retain PII rights over their likeness in video**, the same as
  their voice in audio. Any crew member (active or former) can request that
  their identifiable appearance be removed or blurred in video.
- **Video anonymization** (face blurring, body obscuring) is an acceptable
  alternative to full deletion, provided the person is no longer identifiable.
  This is technically harder than audio anonymization and may require
  re-processing the video through a face-detection pipeline.
- **Non-member boats captured in video** are subject to the same principle as
  AIS data: the platform must not become a surveillance tool. However, video
  captured incidentally during racing is a natural consequence of being on the
  water together. The policy does not require blurring of non-member boats
  in race footage, but boat owners should be aware that uploading video makes
  other boats' tactical decisions visible.

#### Camera consent

Operating cameras on a racing sailboat creates an **implicit recording
environment**. The boat owner is responsible for:

- **Informing crew** that cameras are active during sessions. The platform
  displays camera status on the home page when cameras are recording.
- **Respecting crew objections.** If a crew member objects to being recorded,
  the boat owner should accommodate them (e.g., adjusting camera angles, not
  recording that crew member's position). A crew member's objection does not
  override the boat owner's right to record their own boat, but the boat
  owner should not publish video that a crew member has asked to be excluded
  from.
- **Crew departure.** When a crew member departs, their video PII rights
  persist. Former crew can request face-blur or removal from published videos
  that identify them, the same as audio deletion rights.

#### YouTube as a third-party platform

Video uploaded to YouTube is governed by **YouTube's Terms of Service** in
addition to this policy. Key implications:

- **Data leaves the boat permanently.** Unlike instrument data (stored on the
  Pi) or audio (stored locally and optionally offloaded for transcription),
  YouTube videos are hosted by Google on Google's infrastructure. The boat
  owner cannot guarantee deletion from Google's systems — YouTube's content
  removal process applies.
- **Privacy settings are the boat owner's responsibility.** Videos can be
  uploaded as private, unlisted, or public. The platform defaults to
  **unlisted** (accessible only via direct link). The boat owner controls
  this setting and is responsible for choosing an appropriate visibility level.
- **Linking is not sharing the video.** Helm Log stores only metadata (video
  ID, title, duration, sync points). The co-op never hosts video content.
  Linking a video to a session creates a navigable reference, not a copy.
- **Unlinking removes the reference, not the video.** If a boat owner unlinks
  a video, the metadata is removed from the Pi and any co-op references. The
  YouTube video itself remains on YouTube under the uploader's Google account.

#### Video in the co-op context

YouTube video links and metadata are **boat-private by default** (listed under
"What is NOT shared" in Section 2). This means:

- Co-op members **cannot see** another boat's linked videos unless the boat
  owner explicitly shares them (e.g., via coach access or tuning partner
  sharing).
- If the co-op charter or a future feature enables **video sharing**, the same
  event-scoping and temporal embargo rules that apply to track data would
  apply to video links. Video from events you didn't participate in would not
  be accessible.
- **360° video is tactically more revealing than instrument data.** Sail trim,
  crew weight placement, tacking technique, mark rounding strategy — all
  visible in video but not in instrument telemetry. Co-ops should consider
  this when deciding whether to enable video sharing features.

#### Video deletion and departure

When a boat departs the co-op or requests data deletion:

- YouTube video **metadata** (links, sync points) is deleted from the Pi and
  any co-op references, per the standard deletion process in Section 5.
- The **YouTube videos themselves** remain on YouTube. The platform cannot
  delete YouTube content — only the Google account holder (the uploader) can.
- If a departing boat wants full video deletion, they must separately delete
  the videos from their YouTube channel. The platform's deletion process
  handles metadata only.

#### Video retention

YouTube videos have no automatic expiration — they persist until the uploader
deletes them. This creates an asymmetry with on-Pi data, which can be aged
or purged:

- Instrument data can be aged (reduced resolution after 1-2 seasons)
- Audio can be deleted from the Pi
- But YouTube videos linked to those same sessions remain at full fidelity
  on YouTube indefinitely

Boat owners should be aware of this asymmetry when uploading race video.
The platform may add a reminder when videos are linked to sessions older
than the co-op's data aging threshold.

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

- **Per-boat opt-in**: a coach can only see data from boats that have
  **individually and explicitly granted** that coach access. There is no
  "co-op-wide coach access." Each boat decides independently whether to share
  with a specific coach and which sessions to share
- The coach may **view and analyze** data within the Helm Log platform but may
  **not bulk-export or download** co-op data from other boats
- The coach may **not aggregate** multiple boats' data into a derived dataset that
  the coach retains independently of the platform
- Coach access grants are **time-limited** and must be renewed each season (or at
  an interval set by the boat owner). There is no perpetual coach access
- When a coaching engagement ends — whether by expiration, revocation, or mutual
  agreement — the coach **agrees to delete all data** from that boat. The platform
  revokes technical access automatically; deletion of any offline copies (notes,
  screenshots, downloaded files) is a **normative obligation** that the platform
  cannot enforce technically
- **Derivative works** — the coach **agrees not to retain or distribute** reports,
  summaries, screenshots, spreadsheets, or other materials created using co-op or
  boat data after access ends. This is the same normative obligation — the platform
  can't reach into a coach's laptop, but violation is grounds for the boat owner
  or co-op to deny future coach access
- The boat owner can revoke a coach's access at any time, which triggers the
  same deletion and non-retention obligations

#### Knowledge transfer reality

This policy cannot prevent a coach from **learning** from data and carrying
that knowledge to future engagements. A coach who studies a fleet's wind
patterns, tuning ranges, or tactical tendencies retains that knowledge
regardless of file deletion. This is how competitive knowledge has always
moved in sailing — through people, not databases.

The policy's goal is to prevent **systematic extraction** (bulk data capture,
dataset aggregation, commercial analytics products) while accepting that
**human learning is uncontrollable**. The per-boat opt-in and session-level
permissioning ensure each boat consciously chooses what a specific coach sees,
rather than inadvertently exposing the entire co-op dataset.

### Processing offload

Some operations — audio transcription, speaker diarization, photo analysis,
video processing — are too computationally expensive for the Raspberry Pi.
Helm Log supports **offloading** these tasks to a faster machine (e.g., a
Mac on the same Tailscale network). When data leaves the Pi for processing,
the following rules apply:

#### Own-boat offload (boat owner's machine)

The simplest case: the boat owner sends their own data to their own hardware
(e.g., a personal Mac running a transcription worker). This is functionally
equivalent to processing on the Pi — the data stays under the boat owner's
control. However:

- **Crew PII obligations still apply.** Audio recordings contain crew voices.
  If a crew member requests deletion of their voice data, the boat owner must
  ensure the offload host also purges any cached copies (WAV files, transcript
  segments, intermediate processing artifacts).
- **The offload host must not retain data beyond the processing task.** Once
  the result (transcript, analysis output) is returned to the Pi and stored in
  SQLite, the offload host should delete the source file and any intermediate
  artifacts. The Pi is the single source of truth — offload hosts are
  ephemeral processors, not storage.

#### Third-party offload (cloud services, shared infrastructure)

If the boat owner uses a third-party service for processing (cloud
transcription API, hosted ML inference, etc.):

- The third-party service is a **data processor** (in GDPR terms) acting on
  behalf of the boat owner (data controller). The boat owner is responsible
  for ensuring the processor handles PII appropriately.
- The platform should **warn the boat owner** when configuring an offload URL
  that points outside the Tailscale network (i.e., to a public endpoint) that
  PII will leave the private mesh.
- The same crew deletion obligations apply — if a crew member requests voice
  deletion, the boat owner must ensure the third-party has purged any copies.

#### Co-op processing offload (future)

Some co-op operations may benefit from offload to a designated machine:

- **Current model computation**: querying all peers and running the aggregation
  could run on a member's beefy hardware or a co-op-funded server
- **Benchmark aggregation**: if the co-op grows large enough that each Pi
  querying all peers is impractical, a designated aggregator could compute
  benchmarks centrally

Co-op processing offload introduces additional constraints:

- The offload host sees **co-op data from multiple boats** — it is no longer
  just own-boat data. The host must be treated as a co-op resource, not a
  personal machine.
- The offload host must **not retain raw per-boat data** beyond the
  computation. Only the aggregated result (current model, benchmark
  statistics) may persist.
- The co-op must **approve the offload host** by vote (standard 2/3
  supermajority) and the host must be identified in the co-op charter.
- Audit logging of the offload host's data access is required.

#### Transport security

All processing offload traffic must be encrypted in transit. Tailscale
provides this by default for traffic between Tailscale nodes. For offload
to non-Tailscale endpoints, HTTPS (TLS 1.2+) is required.

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
- **Derived metrics**: standardized platform calculations only — VMG, polar
  performance percentage, tacking angles (see Definitions; does not include
  proprietary analytics or custom models)
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

### Session visibility and data aging

To limit the value of slow, patient data extraction, the co-op may configure
**session visibility rules** in its charter:

#### Event-scoped visibility (recommended)

Members can view full-detail session data only from **events they also
participated in**. This is the core reciprocal value proposition — "I show
you my race, you show me yours." Sessions from events a member did not attend
are visible as **summary metrics only** (session metadata, finishing position,
aggregate performance stats) but not full track or instrument data.

This prevents a member from mining the entire historical dataset of races they
never sailed in, while preserving the full value of head-to-head comparison
for events where both boats were on the water.

#### Data aging tiers (optional)

The co-op may configure **data aging** to reduce the detail level of older
sessions:

| Age | Detail level |
|---|---|
| Current season | Full instrument data at recorded resolution |
| Previous season | Reduced resolution (e.g., 10-second intervals instead of 1 Hz) |
| Older than 2 seasons | Summary metrics only (aggregates, no raw track data) |

Data aging thresholds are set in the co-op charter. A boat's **own data** is
never aged — the boat owner always has full access to their complete history
regardless of co-op aging rules.

#### Default behavior

If the co-op charter does not specify visibility rules, the default is
**full visibility for all shared sessions** (the current behavior). Event
scoping and data aging are opt-in features that the co-op enables by charter
provision or majority vote.

### No export tools for co-op data

The platform **does not provide export, download, or bulk-access tools** for
other boats' co-op data. Each boat can export its own data freely, but co-op
data from other boats is viewable only within the Helm Log interface. API
endpoints serving co-op data are **rate-limited** and **audit-logged**.

This is a **technical restriction, not an absolute guarantee**. View-only data
can still be captured via screenshots, manual transcription, or browser
automation. The platform makes extraction inconvenient and detectable, not
impossible. The audit system (Section 12) detects anomalous access patterns and
alerts admins. Deliberate extraction is a policy violation subject to expulsion.

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

### Temporal sharing controls

The co-op may establish a **sharing delay policy** that applies to all members,
controlling when session data becomes available after a race. This is a
**co-op-level decision** (set by majority vote or in the co-op charter), not a
per-boat choice — ensuring all members operate under the same rules:

- **Immediate sharing** (default): session data is available to the co-op as
  soon as it is marked as shared
- **Delayed sharing**: session data is embargoed for a co-op-specified duration
  (e.g., "share 7 days after session" or "share after series ends"). During
  the embargo, the session is visible in the co-op session list as "pending"
  but track and instrument data are not accessible
- **Seasonal sharing**: the co-op may set a blanket policy (e.g., "share all
  sessions after the last race of the series") that applies to all sessions
  within a date range

The co-op sets the delay policy; individual boats cannot override it with a
shorter or longer delay. This prevents a situation where some boats share
immediately while others delay, creating an asymmetric information advantage.

Delayed sessions **count toward the contribution threshold** — a boat with
delayed-but-committed sessions is fulfilling its reciprocal obligation, just
on a time delay.

Elite cycling teams on Strava routinely hide power and heart rate data during
competition but share freely in the off-season. This feature acknowledges the
same competitive dynamic in one-design sailing.

### Contribution threshold

To join the co-op and access co-op data, a boat must share **at least one race
session**. There is no ongoing minimum contribution requirement.

This low threshold maximizes adoption. In a one-design fleet where everyone races
together, social dynamics are a more effective incentive than technical enforcement.

### Anonymous fleet benchmarking

The co-op computes **fleet benchmarks** (see Definitions) — anonymous aggregate
statistics that let each boat see where they stand relative to the fleet without
revealing who is faster.

#### What benchmarks show

A boat sees its own performance alongside the fleet distribution:

- **Fleet statistics**: median, top 25%, top 10% for any metric
- **Your position**: your value and percentile rank within the fleet
- **Condition binning**: benchmarks are scoped to wind speed ranges, wave state,
  or other environmental conditions so comparisons are apples-to-apples

Example: "Your upwind VMG in 10–12 kts TWS is 5.88 kt — fleet median is 5.72,
top 10% is 5.95. You rank in the top 18%."

Benchmarks can cover any derived metric the platform computes: VMG, tacking
loss, gybe loss, start timing, layline accuracy, polar performance percentage,
and more.

#### What benchmarks do NOT show

- **No boat identities.** Benchmarks never reveal which boats produced which
  data points. "Top 10%" is a statistical threshold, not a leaderboard.
- **No individual tracks.** Benchmarks are computed from aggregate statistics,
  not viewable track data.
- **No reverse-engineering tools.** The platform does not provide tools to
  correlate benchmark positions with individual boats. However, the small-fleet
  anonymization disclaimer (Section 5) applies — in a co-op of 5 boats, "top
  10%" is effectively one boat.

#### Minimum fleet size for benchmarks

Benchmarks are only computed when **at least 4 boats** contribute data for a
given metric and condition bin. Below this threshold, aggregate statistics are
too easily attributed to individual boats. If a condition bin has fewer than 4
contributors, the platform shows "insufficient data" rather than a benchmark.

#### Benchmarks are not ML

Fleet benchmarks are **descriptive statistics** (medians, percentiles, counts),
not predictive models. They do not require the AI/ML governance process in
Section 8. If the co-op later wants to build predictive models from benchmark
data (e.g., "predict your VMG improvement if you reduce tack loss by 0.5s"),
that crosses into Section 8 territory and requires a supermajority vote.

#### Benchmark data and departure

When a boat departs the co-op, their data is anonymized per Section 5. Their
historical contributions to benchmarks remain in the aggregate statistics (since
benchmarks contain no individual identity), but no new benchmarks are computed
from their data.

#### Benchmarks and temporal sharing

Benchmarks respect the co-op's temporal sharing controls. If sessions are
embargoed, the benchmark statistics are not updated with embargoed data until
the embargo lifts. This prevents using benchmark shifts to infer what embargoed
boats are doing.

#### Why this matters

Anonymous benchmarking is the co-op's primary value proposition for competitive
sailors. It answers the questions elite sailors actually ask — "Are we fast?"
"Are we losing in tacks or straight-line speed?" "Is our setup competitive?" —
without requiring anyone to expose their secrets. Every boat that contributes
data improves the benchmarks for everyone, creating a network effect that grows
the co-op's value.

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

The co-op charter specifies one of two administration modes:

**Multi-admin mode** (recommended for co-ops of 5+ boats):
- The co-op founder designates the **initial admin boats** (2–3 boats,
  including themselves) at co-op creation
- Admin authority is distributed: administrative actions (approving members,
  signing revocations) require signatures from a **majority of admin boats**
  (e.g., 2-of-3). This eliminates single points of failure — the co-op
  survives the loss of any single admin's Pi
- Admin rotation is handled by **charter amendment**: the existing admins
  (meeting threshold) sign an amendment that adds or removes an admin boat.
  No private key material changes hands

**Single moderator mode** (available for co-ops of any size):
- The co-op charter designates a **single moderator boat** with a named
  **backup moderator** who assumes the role if the primary is unavailable
- The moderator handles all admin functions without multi-signature
  requirements
- This mode is simpler for small fleets where the full M-of-N signing
  process would be burdensome
- The co-op may switch from single moderator to multi-admin mode by majority
  vote at any time

In either mode, there is no fixed admin term — admins serve until replaced by
charter amendment or removed by member vote.

#### Admin removal

An admin can be removed by the same **supermajority (2/3) vote** used for
expulsion. Admin removal does not affect the person's co-op membership — they
remain a member, just no longer an admin. The remaining admins sign a charter
amendment to update the admin boat list.

### Active and inactive members

In a seasonal sport, requiring votes from boats that are hauled out for the
winter creates deadlock. To prevent this:

- **Active**: a boat that has sent a heartbeat (an automated presence signal)
  within the co-op's configured inactivity threshold (default: 60 days).
  Active boats are counted in the quorum denominator for all votes
- **Inactive**: a boat with no heartbeat in 60+ days. Inactive boats are
  **excluded from the quorum denominator** for standard votes (2/3
  supermajority) but retain full data access, co-op membership, and the
  right to vote if they choose to
- **Unanimous votes** (e.g., current model sharing per Section 8) require
  all **active** members. Inactive members are excluded from the denominator
  but may opt back in by sending a heartbeat before the vote closes

Example: 7-boat co-op, 2 boats inactive for winter haul-out.
- 2/3 supermajority vote: need 4 of 5 active boats (not 5 of 7)
- Unanimous vote: need 5 of 5 active boats

A boat that comes back online and sends a heartbeat immediately becomes
active again and is included in future votes. The inactivity threshold is
set in the co-op charter and may be adjusted by majority vote.

**Seasonal power-down**: many boats remove electronics or power down their Pi
for winter storage. This is expected behavior, not abandonment. A boat owner
may also **manually set their status to inactive** via the web UI or CLI
before haul-out, which has the same effect as an expired heartbeat without
waiting for the threshold. Inactive status does not affect data access or
membership — it only affects quorum calculations.

### Joining

Any boat running a Helm Log instance can request to join a co-op. The platform
is fully functional without co-op membership — joining is a choice, not a
requirement. Joining requires:

1. Agreeing to this data licensing policy
2. Sharing at least one race session
3. Acceptance by a co-op admin

#### Membership eligibility

The co-op exists for **boats that actively race**. To prevent commercial
analytics actors (sailmakers, design firms, performance analytics startups,
betting data companies) from joining solely to observe and extract value:

- Each co-op may set **eligibility criteria** in its charter (e.g., "must
  actively race in the fleet's regular series," "must be a current class
  association member," "must have raced at least 3 events in the past 12
  months")
- A co-op member that is discovered to have joined primarily for data
  observation — rather than reciprocal competitive sailing — may be subject
  to **expulsion** under the standard process (Section 3)
- **Sailmakers, coaches, and analytics providers** may access co-op data
  only through the delegated coach access mechanism (Section 1), which is
  per-boat opt-in, time-limited, and revocable — not through direct
  membership

If no eligibility criteria are specified in the charter, the default is: any
boat with a Helm Log instance that shares at least one session may join.

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

### Expulsion and deletion precedence

Sessions that have been **shared with the co-op** become part of the co-op
dataset. An expelled member **cannot request deletion of sessions that were
shared during their membership**. Those sessions are anonymized (attributed to
"Boat X") but remain in the co-op dataset.

This prevents a scenario where a bad actor is expelled and then requests
deletion to destroy valuable dataset history out of spite. The co-op accepted
the data in good faith during membership; expulsion revokes future access, not
the co-op's right to retain anonymized historical data.

**Voluntary departure** retains the same anonymization treatment — shared
sessions remain in the dataset as "Boat X." A full deletion request (Section 5)
is available for voluntary departures but applies only to **identifiable data**;
anonymized sessions that can no longer be attributed to the boat are retained.

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

### Data portability

Every boat has the **unconditional right to export all of its own data** at any
time in open, non-proprietary formats:

- **Instrument data and tracks**: CSV, GPX, JSON
- **Session metadata and race results**: JSON
- **Audio recordings**: original WAV files
- **Transcripts**: plain text or JSON with timestamps

This right exists regardless of co-op membership status, platform version, or
any other condition. No technical mechanism, API change, or platform update may
restrict a boat's ability to export its own data. This guarantee prevents the
platform lock-in pattern seen in services like Strava, where API policy changes
have restricted athletes' ability to use their own data with third-party tools.

A boat's own data is always the boat's own data. The platform is a tool, not a
custodian.

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

### Gambling and wagering prohibition

Co-op data, derivatives, models, and any information obtained through co-op
membership may **not** be used in connection with **betting, wagering, gambling,
fantasy sports, or prediction markets** of any kind. This prohibition is
**absolute and cannot be overridden by co-op vote**.

This blanket prohibition exists because:

- Betting markets in sailing are growing (SailGP, match racing, offshore events).
  MLB's exclusive data deal with Sportradar is worth hundreds of millions and is
  driven almost entirely by sports betting demand. Once co-op data has gambling
  value, the incentive structure changes in ways that are incompatible with a
  reciprocal sharing cooperative
- The co-op exists to make everyone faster, not to create an information asymmetry
  that benefits bettors
- Individual boats retain full rights to their own data (Section 1) and may use
  it however they wish — this prohibition applies only to co-op data and
  derivatives

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

In small co-ops (fewer than ~10 boats), anonymized race data **will almost
certainly be identifiable** by experienced fleet members. GPS tracks are
fingerprints — start-line position, upwind mode, tacking style, sail
inventory, and performance signature are unique to each boat. Replacing a
name with "Boat X" hides identity from outsiders but not from people who
race against that boat every week.

Members should understand that anonymization provides:

- **Protection against outsiders** who don't know the fleet
- **Plausible deniability** in casual conversation
- **No protection** against determined analysis by someone with fleet knowledge

This is not a flaw in the system — it is an inherent limitation of
anonymizing small, specialized datasets. The same limitation exists in Strava
anonymized segments, cycling power datasets, and esports match telemetry.
Members who are uncomfortable with this should factor it into their decision
to join the co-op.

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

### Limitation of damages

To the maximum extent permitted by applicable law, in no event shall any
co-op member, admin, or the Helm Log platform be liable for any **indirect,
incidental, special, consequential, or punitive damages** arising out of or
related to the use of shared data, regardless of the theory of liability.

### Indemnification

Each co-op member agrees to **indemnify and hold harmless** the other members,
the co-op admins, and the Helm Log platform maintainers from any claims,
damages, or expenses arising from that member's use of co-op data, including
but not limited to violations of this policy, misuse of shared data, or
unauthorized disclosure.

### Governing law

Each co-op specifies its **governing jurisdiction** in its charter. If no
jurisdiction is specified, disputes are governed by the laws of the jurisdiction
where the co-op was created (i.e., the jurisdiction of the founding admin's
registered address). This policy is not a contract with the Helm Log open-source
project — it is a contract between co-op members.

### Data controller

Under GDPR and equivalent privacy frameworks, the **data controller** for each
boat's data is the **boat owner**. The boat owner decides what data to collect,
how long to retain it, and with whom to share it.

When a boat joins a co-op, the co-op functions as a **joint controller** for
shared session data — each member has contributed data and each member has
access. The co-op admin is the designated contact point for data subject
requests related to co-op data.

The Helm Log platform (software and any hosted services) is a **data
processor** — it processes data on behalf of boat owners and co-ops but does
not independently determine the purposes or means of processing.

### Protest hearings and dispute resolution

Co-op data — including GPS tracks, instrument telemetry, and derived metrics
from **any boat other than your own** — may **not** be submitted as evidence in
racing protest hearings, redress requests, or any formal dispute resolution
process governed by the Racing Rules of Sailing.

A boat may use **its own data** in a protest (it's their data — they can do
what they want with it). But using another co-op member's shared data against
them in a hearing would fundamentally undermine the trust required for
reciprocal sharing. If members feared their GPS tracks could be used to
penalize them in a protest, no one would share.

This restriction applies to:

- Formal protests under RRS Part 5
- Redress hearings
- Measurement protests
- Any proceeding before a protest committee or appeals body
- Class association disciplinary proceedings

This restriction does **not** prevent informal post-race discussion or debrief
using co-op data — "hey, look at where we both were at the mark" is fine.
Submitting that data to a protest committee is not.

---

## 12. Technical Requirements

This policy requires the following technical capabilities in the Helm Log
codebase. Requirements are split into **MVP** (needed for initial co-op
launch) and **future** (needed as the platform matures).

### MVP requirements

| Requirement | Purpose |
|---|---|
| `data_sharing_consent` table | Record each boat's agreement to this policy and co-op membership status |
| Per-session sharing flags | Mark individual sessions as co-op-shared, coach-shared, or private |
| Boat-level identity in auth | Boats are first-class entities with owners, designated representatives, crew, and sharing posture |
| No-bulk-export enforcement | Co-op data viewable in-platform only; API and export restricted to own-boat data |
| Data suppression (soft delete) | Hide (but preserve) a boat's data during 30-day grace periods; data remains in DB but is excluded from all queries and views |
| Permanent deletion (hard delete) | Irreversibly purge data from the database after grace period expiration; no recovery possible |
| Audio PII deletion | Delete entire recordings containing a specific speaker. Whole-recording deletion is the baseline; per-segment editing is a future capability |
| Reversible anonymization | Replace boat identity with "Boat X" in co-op comparisons; retain mapping for 30-day reversal window, then permanently delete mapping |
| Data portability export | Unconditional export of all own-boat data in CSV, GPX, JSON, WAV formats; no restrictions on frequency or volume |
| Audit logging | Log all co-op data API access (who fetched which session, when) with **data volume** (points returned, bytes transferred) to detect extraction patterns. Rate-limit based on both request count and data volume — a peer scraping 1 Hz data for hundreds of sessions triggers auto-freeze even if request rate looks normal. Alert admin on anomalous patterns. Legitimate UI browsing must not trigger false positives |
| Replay protection | Include a random nonce in every signed API request; reject duplicate nonces within the clock skew window. Prevents replay attacks even when NTP sync is stale |
| Revocation broadcast | On membership revocation (departure or expulsion), actively push the signed revocation record to all online peers rather than relying on passive polling. Ensures rapid enforcement within minutes of signing |
| Processing offload cleanup | When a processing task (transcription, analysis) completes on a remote host, the Pi should request deletion of the source file and intermediate artifacts from the offload host. Log offload events (what was sent, where, when) for PII audit trail |
| AIS data filtering | Exclude AIS and proximity data from other vessels during capture; never store non-member tracking data |
| Email PII handling | Scrub owner and crew email from records on departure; admin-only visibility by default |
| Active/inactive member tracking | Heartbeat-based activity detection; manual inactive toggle; configurable inactivity threshold; quorum denominator adjustment |
| Fleet benchmark computation | Compute anonymous aggregate statistics (median, percentiles, rank) per metric per condition bin from co-op shared data; enforce minimum 4-boat threshold per bin |
| Benchmark embargo sync | Exclude embargoed session data from benchmark computation until embargo lifts |

### Designed (see [Federation Protocol Design](federation-design.md))

Section 12 groups requirements into three tiers: **MVP** (needed for initial co-op launch), **Designed** (requirements with complete protocol specifications, API endpoints, SQLite schemas, and Python module signatures defined in the federation design document), and **future** (needed as the platform matures). The following requirements are part of the **Designed** tier:

| Requirement | Purpose |
|---|---|
| Multi-co-op support | A boat can belong to multiple co-ops; each co-op has independent membership and data pools |
| Coach/tuning-partner ACLs | Per-boat, time-limited, revocable access grants with mandatory deletion on expiration |
| Cross-co-op isolation | Enforce data pool boundaries; prevent cross-co-op queries or exports |
| Per-event co-op assignment | When a boat belongs to multiple co-ops, require co-op selection per session before data is shared |
| Pre-join disclosure | Present all active commercial, ML, current model, and cross-co-op agreements to prospective members before admission |
| Current observation derivation | Compute observed current vectors from BSP/heading vs SOG/COG; store as boat-private by default |
| Current model geographic scoping | Scope current/tide models to defined geographic areas; per-area opt-in/opt-out per boat |
| Current model unanimous consent | Enforce unanimous vote requirement (not 2/3) for current model projects |

### Future requirements

| Requirement | Purpose |
|---|---|
| Club/multi-boat entity support | Track which boats belong to the same owning entity for vote-capping rules |
| Audio anonymization | Voice scrambling / redaction as an alternative to full deletion |
| Photo PII handling | Deletion or anonymization of identifiable photos on request |
| Admin election tracking | Record admin elections, terms, and removal votes |
| Expulsion vote tracking | Record votes (by boat representative), notice periods, and appeal outcomes |
| YouTube metadata cleanup | Remove linked video metadata on boat departure/deletion |
| Video PII handling | Face-blur or removal of identifiable crew in video on request; extends crew PII deletion rights to video recordings |
| Camera consent notification | Display active camera recording status on home page; provide mechanism for crew to flag recording objections |
| YouTube upload privacy default | Default to unlisted YouTube uploads; warn when changing to public; log upload events with privacy setting |
| Video aging reminder | Notify boat owner when YouTube videos are linked to sessions older than the co-op's data aging threshold, since YouTube videos persist indefinitely unlike aged instrument data |
| ML opt-out flag | Per-boat flag to exclude data from approved ML training projects |
| ML project governance | Record ML project proposals, votes, model ownership, and opt-outs |
| Commercial use tracking | Record commercial agreements, votes, and revenue distribution |
| Co-op dormancy tracking | Track last governance activity date; trigger dormant status after 2 years of inactivity |
| Non-member result scoping | When importing full-fleet results, store only official scored finish data for non-members; no instrument or session data |
| OA license compliance | Track organizing authority and race management software licensing terms for imported results |
| Dual membership tracking | Record multi-co-op memberships; notify both co-ops; enforce co-op-level dual membership policies |
| Biometric consent tracking | Per-person, per-data-type consent records for biometric data; independent of instrument data sharing |
| Biometric data isolation | Store biometric data separately from instrument data; enforce per-person access controls |
| Temporal sharing controls | Co-op-level sharing delay (immediate, duration-based, or date-based embargo); embargo state visible in co-op session list |
| Benchmark condition binning | Configurable environmental condition bins (wind speed, wave state, current) for fleet benchmark computation; co-op-level bin definitions |
| Benchmark historical trends | Per-boat performance trends over time relative to fleet benchmarks; own-boat only (no cross-boat trend comparison) |
| Gambling prohibition enforcement | Include prohibition in co-op membership agreement; policy-level restriction |
| Protest firewall | Technical documentation that co-op data from other boats is inadmissible in protest proceedings; no enforcement mechanism needed (policy-only) |
| Multi-admin signing | M-of-N admin boat signatures for membership, revocation, and charter amendment records |
| Single moderator mode | Alternative to multi-admin for small co-ops; single moderator with designated backup |
| Event-scoped Proof of Participation | When requesting full track data under event-scoped visibility, the requester provides a signed claim proving they raced the same event — neither boat exposes its private session list |
| Maneuver detection | Detect tacks, gybes, mark roundings, starts, and acceleration events from instrument data for fleet benchmark computation; auto-calibrate thresholds from co-op data |
| Third-party offload warning | Warn the boat owner when configuring an offload URL outside the Tailscale network; log that PII will leave the private mesh |
| Co-op offload host approval | Track co-op vote approving a designated processing host; enforce audit logging on the offload host's co-op data access |
| Photo/video analysis offload | Extend the transcription offload pattern to still photos (sail shape, rig tune) and video (maneuver analysis, start replay) with the same PII and cleanup obligations |

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
| 2026-03-07 | Rev 14 — hardening from cross-sport research (NFL/NBA/MLB/SailGP/Strava/esports): email as PII with admin-only visibility and departure scrubbing; biometric data firewall with per-person consent independent of instrument sharing; temporal/seasonal sharing controls (delayed and embargoed sessions); gambling/betting absolute prohibition; data portability guarantee (anti-lock-in); protest hearing data firewall (other boats' co-op data inadmissible under RRS); active/inactive quorum based on heartbeat to prevent winter deadlock; multi-admin M-of-N signing model replacing single-admin elections |
| 2026-03-08 | Rev 15 — PR review feedback: crew member emails covered alongside owner emails; temporal sharing controls changed from per-boat to co-op-level decision |
| 2026-03-08 | Rev 16 — adversarial review hardening: formal definitions section (session, boat owner, entity, derived metrics, PII, platform, etc.); boat owner vs instance operator clarified (owner wins); coach derivative works reframed as normative obligation; bulk export reframed as "no export tools" with honest acknowledgment of view-only limitations; expulsion+deletion precedence (shared sessions survive expulsion as anonymized data); single moderator mode for small co-ops; heartbeat seasonal power-down handling with manual inactive toggle; liability expanded (limitation of damages, indemnification, governing law); data controller/processor roles defined for GDPR; audit logging scoped to API endpoints not UI views; audio PII weakened to whole-recording deletion baseline; tech requirements split into MVP vs future |
| 2026-03-08 | Rev 17 — attack vector mitigations: coach access scoped to per-boat opt-in with session-level permissioning and "knowledge transfer reality" acknowledgment; anonymization disclaimer strengthened to "will almost certainly be identifiable" with specific examples (track shapes, start positions, tactical style); event-scoped session visibility (full detail only for events you participated in) with optional data aging tiers (current season full, previous season reduced, older summary only); membership eligibility criteria (active racing requirement, commercial actors must use coach access, observation-only grounds for expulsion) |
| 2026-03-08 | Rev 18 — anonymous fleet benchmarking: fleet benchmark definition added to Definitions; new "Anonymous fleet benchmarking" subsection in Section 2 covering what benchmarks show (fleet statistics, percentile rank, condition binning), what they don't show (no identities, no tracks, no reverse-engineering tools), minimum 4-boat threshold per condition bin, benchmarks-are-not-ML clarification (descriptive stats don't require Section 8 governance), benchmark behavior on departure and during embargoes, network effect value proposition; MVP tech requirements for benchmark computation and embargo sync; future tech requirements for condition binning and historical trends; plain English summary updated |
| 2026-03-08 | Rev 19 — protocol hardening from security review: audit logging expanded to track data volume (points returned, bytes transferred) for volume-based rate limiting; replay protection via request nonces with dedup; revocation broadcast (active push to all peers instead of passive polling); event-scoped Proof of Participation for track data requests; maneuver detection added to future tech requirements |
| 2026-03-08 | Rev 20 — processing offload: new "Processing offload" subsection in Section 1 covering own-boat offload (PII obligations, ephemeral processing, no data retention on offload host), third-party offload (GDPR data processor role, public endpoint warning), co-op offload (designated host approved by 2/3 vote, no raw per-boat data retention, audit logging), and transport security (Tailscale default, TLS 1.2+ for non-Tailscale); MVP tech requirement for offload cleanup and audit trail; future requirements for third-party warning, co-op host approval, and photo/video analysis offload; plain English summary updated |
| 2026-03-08 | Rev 21 — video and camera pipeline: comprehensive rewrite of "YouTube and external video" into "Video recordings and the camera pipeline" covering: video as PII (crew faces, voices, other boats, tactical info in 360° footage); crew PII rights over likeness in video with face-blur anonymization; camera consent (inform crew, respect objections, departure rights); YouTube as third-party platform (data leaves boat permanently, privacy settings, unlinking vs deletion); video in co-op context (boat-private by default, event-scoping for future sharing); video deletion asymmetry (metadata deleted, YouTube videos persist); video retention (no auto-expiration on YouTube, aging reminder); PII definition updated to include video; future tech requirements for video PII handling, camera consent notification, upload privacy defaults, and video aging reminders |
