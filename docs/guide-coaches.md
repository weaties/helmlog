# Coach Access Guide

> How coaching works in a Helm Log data co-op.

---

## What you get

As a fleet coach, the co-op admin can grant you access to shared session
data. This gives you:

- **Track overlays** — see multiple boats on the same race, color-coded by
  speed
- **Instrument time-series** — boat speed, wind angles, heading, and heel
  for each boat on each leg
- **Polar performance** — how each boat performed relative to target speeds
  at each wind angle
- **Race results** — finish order and time deltas
- **Fleet benchmarks** — anonymous percentile rankings (e.g., "Boat A's
  tacking angles are in the 75th percentile of the fleet")

This is enough to run a full debrief, identify fleet-wide weaknesses, and
give targeted coaching to individual boats.

Note: fleet benchmarks require at least 4 boats contributing data in a
given condition band (wind speed / angle range) to produce meaningful
percentile rankings.

---

## What you don't get

The co-op data policy protects certain categories of data. As a coach,
you **cannot** access:

| Not available to coaches | Why |
|---|---|
| Audio recordings | Crew conversations are private (PII) |
| Transcripts | Spoken content is speaker-owned |
| Photos and notes | Personal race notes stay private |
| Crew rosters | Who sailed where is boat-private |
| Sail selection | Gear choices are competitive info |
| YouTube video links | Video links are boat-private unless a boat shares them with you directly |
| Raw data export / bulk download | Prevents data accumulation beyond your access window |

If a boat wants to share any of these with you directly (e.g., play you an
audio clip during a debrief), that's their choice — but the platform won't
serve it to you automatically.

---

## How access works

### Getting access

1. The co-op admin grants you a **coach access record** with a start and
   end date (e.g., "May 1 through October 31")
2. You receive a link or QR code that sets up your device
3. You can view shared sessions from any co-op member's boat

### What happens during your access window

- You can view any session that a co-op member has shared
- Every time you view a session, it's logged in the co-op's audit trail
- You can view data in the platform but not export it in bulk
- **Taking notes, screenshots, and preparing presentations is explicitly
  encouraged** — that's expected coaching work. The no-export rule applies
  to raw data downloads, not to your own analysis and materials.

### What happens when access expires

- Your access stops automatically on the expiration date
- You can no longer query any co-op member's data
- No action needed from the admin — it's enforced by the protocol
- If the fleet wants to renew, the admin grants a new access window

### Renewal

Access is typically granted per-season. If the fleet renews your coaching
engagement, the admin grants a new access record. There's no automatic
renewal.

---

## Rules to be aware of

These rules exist to protect the fleet and to make sure coaching
relationships stay healthy. Each one has a reason:

1. **No aggregation across co-ops.** If you coach multiple fleets, you
   cannot combine data from different co-ops to build cross-fleet models
   or comparisons.
   *Why: Each co-op's data belongs to that co-op. Combining fleets would
   create competitive intelligence that no individual fleet agreed to.*

2. **No derivative works beyond your access window.** If you build a
   presentation, polar model, or analysis from co-op data, you should not
   continue distributing it after your access expires. The fleet's data
   stays with the fleet.
   *Why: Access is time-limited for a reason. If analyses outlive the
   engagement, the time limit is meaningless.*

3. **No sharing co-op data with non-members.** You can discuss your
   coaching observations (that's your expertise), but you cannot share raw
   session data, track files, or instrument recordings with anyone outside
   the co-op.
   *Why: The fleet shared data with you specifically, not with the world.
   Your insights and observations are yours to share — raw data is not.*

4. **Audit transparency.** The co-op admin can see which sessions you
   accessed and when. This isn't surveillance — it's the same transparency
   that any shared-data system should have.
   *Why: Trust requires visibility. Knowing that access is logged keeps
   everyone honest and makes the system trustworthy for all parties.*

---

## What makes this different from other platforms

Most commercial sailing analytics platforms either:

- Give coaches unlimited access with no controls, or
- Don't support coaching at all

Helm Log's approach is designed to reflect how coaching actually works:

- **Time-limited** — matches the coaching engagement
- **Scoped to what you need** — instrument data and benchmarks, not private
  conversations
- **Transparent** — everyone knows what's being accessed
- **Revocable** — if the relationship ends, so does access
- **No lock-in** — coaches don't accumulate a data warehouse that outlasts
  the engagement

This protects both the fleet and the coach. The fleet knows their data is
governed. The coach knows the rules are clear and consistent.

---

## Running a debrief with Helm Log

A typical post-race debrief using co-op data:

1. **Pull up the race** — open the session in the co-op view. You'll see
   all boats that shared overlaid on the same map.
2. **Identify the key legs** — zoom into the beats and runs where the
   fleet spread out. The speed coloring shows who was fast and slow.
3. **Compare specific boats** — select two boats and walk through the
   leg. Where did one gain? Was it a lane choice, a better tack angle,
   or better VMG on the run?
4. **Check the benchmarks** — fleet percentile rankings show patterns
   across multiple races. "You're in the bottom quartile on downwind
   VMG" is more useful than anecdotes from a single race.
5. **Take screenshots and build your presentation** — you're encouraged
   to capture what you need for your coaching materials.
6. **Debrief with the fleet** — walk through the key moments. The data
   backs up the conversation.

### What to ask boats for before a debrief

Not everything you need is in the platform automatically. Before a
debrief session, ask:

- **Did everyone share their sessions?** If a boat forgot, remind them —
  it's one tap.
- **Wind range and shifts?** The platform records instrument wind, but
  knowing the sailors' read on the conditions adds context.
- **Sail choices?** Sail selection is boat-private in the platform, but
  boats can share it voluntarily. Knowing who was in the #1 vs #3 jib
  matters for the debrief.
- **Tactical context?** "We went left because we saw pressure" — this
  kind of context makes the track data much more useful.

### A note on offline boats

If you can't see a boat's session, their Pi is probably offline. Boats
need to be connected (usually via phone hotspot) for their data to be
available to the co-op. It will sync automatically when they come online
— there's no deadline.

---

## Getting started

Talk to the co-op admin about setting up your access. They'll need:

- Your email address (for out-of-band communication)
- The date range for your coaching engagement
- Confirmation from the fleet that coaching access has been agreed to

For the full technical details on how coach access is implemented, see the
[Data Licensing Policy](data-licensing.md) (Section 5: Coach and Tuning
Partner Access).
