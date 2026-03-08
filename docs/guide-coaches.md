# Coach Access Guide

> How coaching works in a Helm Log data co-op.

---

## What you get

Each boat in the co-op can individually grant you access to their shared
session data. This gives you:

- **Track overlays** — see multiple boats on the same race, color-coded by
  speed
- **Instrument time-series** — boat speed, wind angles, heading, and heel
  for each boat on each leg
- **Polar performance** — how each boat performed relative to target speeds
  at each wind angle
- **Race results** — finish order and time deltas
- **Fleet benchmarks** — anonymous percentile rankings (e.g., "Boat A's
  tacking angles are in the 75th percentile of the fleet")

You see only what individual boats have explicitly granted you access to.
Each boat decides independently — there is no co-op-wide coach access.

This is enough to run a full debrief, identify fleet-wide weaknesses, and
give targeted coaching to individual boats. Fleet benchmarks require at
least 4 boats contributing data in a given condition band to produce
meaningful percentile rankings.

---

## What you don't get

The data policy protects certain categories of data. As a coach,
you **cannot** access:

| Not available to coaches | Why |
|---|---|
| Audio recordings | Crew conversations are private (PII) |
| Transcripts | Spoken content is speaker-owned |
| Photos and notes | Personal race notes stay private |
| Crew rosters | Who sailed where is boat-private |
| Sail selection | Gear choices are competitive info |
| YouTube video links | Boats may link race video to sessions; these links are boat-private unless a boat shares them with you directly during a debrief |
| Raw data export / bulk download | Prevents data accumulation beyond your access window |

If a boat wants to share any of these with you directly (e.g., play you an
audio clip during a debrief), that's their choice — but the platform won't
serve it to you automatically.

---

## How access works

### Getting access

Coach access is **per-boat, not per-co-op.** Each boat owner grants you
access individually:

1. You request access from each boat you want to work with
2. Each boat owner grants you a **time-limited access record** with a
   start and end date (e.g., "May 1 through October 31")
3. You receive a link or QR code that sets up your device
4. You accumulate per-boat access records — one from each boat that
   grants you access

In practice, the fleet champion or admin often coordinates this ("everyone
grant Coach Pat access for the season"), but each boat must approve
individually. The admin cannot grant access on behalf of other boats.

### What happens during your access window

- You can view sessions from boats that granted you access
- Every time you view a session, it's logged in the audit trail
- You can view data in the platform but not export it in bulk
- **Taking notes, screenshots, and preparing presentations is expected
  coaching work.** The no-export rule prevents bulk raw-data downloads,
  not your own analysis. That said, screenshots of every boat's track
  are effectively a persistent copy of shared data — the trust model
  assumes coaches use these for coaching, not for building an archive
  that outlasts the engagement.

### What happens when access expires

- Your access stops automatically on the expiration date
- You can no longer query that boat's data
- No action needed from the boat owner — it's enforced by the protocol
- If the fleet wants to renew, each boat grants a new access window

### Renewal and revocation

Access is typically granted per-season. If the fleet renews your coaching
engagement, each boat grants a new access record. There's no automatic
renewal. Any boat owner can revoke your access at any time.

---

## Rules to be aware of

These rules exist to protect the fleet and to make sure coaching
relationships stay healthy. Each one has a reason:

1. **No aggregation across co-ops.** If you coach multiple fleets, you
   cannot combine data from different co-ops to build cross-fleet models
   or comparisons.
   *Why: Each co-op's data belongs to that co-op. Combining fleets would
   create competitive intelligence that no individual fleet agreed to.*

2. **No distributing materials built from co-op data after your access
   expires.** If you build a presentation, polar model, or analysis from
   co-op data, you agree not to continue distributing it after your
   access ends. This is an agreement between you and the fleet, not a
   technical control — the platform can't reach into your laptop. But
   it's the agreement that makes the relationship work.
   *Why: Access is time-limited for a reason. If analyses outlive the
   engagement, the time limit is meaningless.*

3. **No sharing co-op data with non-members.** You can discuss your
   coaching observations (that's your expertise), but you cannot share raw
   session data, track files, or instrument recordings with anyone outside
   the co-op.
   *Why: The fleet shared data with you specifically, not with the world.
   Your insights and observations are yours to share — raw data is not.*

4. **Audit transparency.** Each boat owner can see which of their
   sessions you accessed and when. This isn't surveillance — it's the
   same transparency that any shared-data system should have.
   *Why: Trust requires visibility. Knowing that access is logged keeps
   everyone honest and makes the system trustworthy for all parties.*

### How these rules protect you as a coach

These aren't just restrictions — they're clarity. When the rules are
explicit, you don't have to guess what's appropriate. You know exactly
what you can access, how long you have it, and what's expected when
it ends. No ambiguity means no awkward conversations later.

---

## What boats should do before granting you access

If a boat owner asks you what they need to know, here's the checklist:

- **Confirm your identity** — the boat owner should know who they're
  granting access to (your email or coaching identity)
- **Understand the access window** — access is time-limited; the boat
  owner sets the start and end dates
- **Know what you can see** — instrument data, tracks, benchmarks, race
  results. Not audio, notes, crew, or sails.
- **Know they can revoke anytime** — if the relationship changes, access
  ends immediately

---

## What makes this different from other platforms

Most commercial sailing analytics platforms either:

- Give coaches unlimited access with no controls, or
- Don't support coaching at all

Helm Log's approach is designed to reflect how coaching actually works:

- **Per-boat consent** — each boat decides, not a central admin
- **Time-limited** — matches the coaching engagement
- **Scoped to what you need** — instrument data and benchmarks, not private
  conversations
- **Transparent** — everyone knows what's being accessed
- **Revocable** — any boat can revoke access at any time

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

- **Did everyone grant you access and share their sessions?** If a boat
  forgot either step, remind them — access is one approval, sharing is
  one tap per session.
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
available. It will sync automatically when they come online — there's
no deadline.

---

## Getting started

Talk to the fleet champion about coordinating access. Each boat will
need to:

- Know your coaching identity (email or public key)
- Grant you a time-limited access record
- Share the sessions they want you to see

For the full technical details on how coach access is implemented, see the
[Data Licensing Policy](data-licensing.md) (Section 1: Coach and Combined
Datasets).
