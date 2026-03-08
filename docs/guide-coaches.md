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
- You can take notes, screenshots, or prepare presentations from what you
  see — that's expected coaching work

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
relationships stay healthy:

1. **No aggregation across co-ops.** If you coach multiple fleets, you
   cannot combine data from different co-ops to build cross-fleet models
   or comparisons.

2. **No derivative works beyond your access window.** If you build a
   presentation, polar model, or analysis from co-op data, you should not
   continue distributing it after your access expires. The fleet's data
   stays with the fleet.

3. **No sharing co-op data with non-members.** You can discuss your
   coaching observations (that's your expertise), but you cannot share raw
   session data, track files, or instrument recordings with anyone outside
   the co-op.

4. **Audit transparency.** The co-op admin can see which sessions you
   accessed and when. This isn't surveillance — it's the same transparency
   that any shared-data system should have.

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

## Getting started

Talk to the co-op admin about setting up your access. They'll need:

- Your email address (for out-of-band communication)
- The date range for your coaching engagement
- Confirmation from the fleet that coaching access has been agreed to

For the full technical details on how coach access is implemented, see the
[Data Licensing Policy](data-licensing.md) (Section 5: Coach and Tuning
Partner Access).
