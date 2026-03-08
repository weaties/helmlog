# Fleet Champion's Playbook

> A guide for the tech-forward sailor bringing Helm Log to their fleet.

---

## Your role

You're the person who sees the potential: a fleet where every boat shares
race data, everyone gets faster, and nobody has to trust a commercial
platform with their competitive secrets. You're going to make that happen.

This guide covers how to pitch Helm Log to your fleet, how to get boats
set up, and how to run a co-op without it becoming a second job.

---

## The mental model

When you're explaining Helm Log to someone, the simplest way to think
about it: **each boat is its own server.** Your Pi records your data,
stores it, and serves it to other boats you choose to share with. There
is no company in the middle. No website you upload to. No subscription.

The co-op is just an agreement between boats to share instrument data
with each other. The technology enforces the agreement — who's in, what's
shared, when access expires — so you don't have to police it yourself.

If someone asks "where does my data go?", the answer is: "nowhere you
didn't send it."

```
  ┌──────────┐       ┌──────────┐       ┌──────────┐
  │  Boat A   │◄─────►│  Boat B   │◄─────►│  Boat C   │
  │  (Pi)     │       │  (Pi)     │       │  (Pi)     │
  └──────────┘       └──────────┘       └──────────┘
       ▲                                       ▲
       └───────────────────────────────────────┘
              direct, encrypted connections
                  (no central server)
```

---

## Pitching it to the fleet

### The one-liner

> "We all share our race tracks, we all see where we're fast and slow,
> and nobody's data goes anywhere we don't control."

### The three things sailors care about

1. **"Will it make me faster?"**
   Yes. You'll see your boat speed, angles, and maneuvers compared to the
   fleet average. You'll know exactly where you're losing and gaining.

2. **"Is my data safe?"**
   Your data stays on your boat. Nothing goes to a cloud server. You
   choose what to share, race by race. You can leave anytime and your
   data comes with you.

3. **"Is it complicated?"**
   For you, no. After setup, it's one tap to share a race. The tech
   stuff happens in the background.

### Common objections and how to answer them

**"I don't want people seeing my mistakes."**
They already see your mistakes — they're on the race course. The
difference is that now you can see theirs too, and everyone learns from
it. The boats that debrief together improve the fastest.

**"We're already fast enough."**
Are you? Without data, you're guessing. The boats at the top of every
competitive fleet use data. This just makes it available to everyone,
not just the boats with the biggest budgets.

**"This sounds complicated."**
For you, it's not. After setup (which I'll help with), it's one tap to
share a race. The tech runs in the background. If you can use a weather
app, you can use this.

**"I don't trust technology with my data."**
Good — neither do we. That's why there's no cloud, no company, no
subscription. Your data stays on your boat unless you choose to share
it. You can leave anytime and take everything with you.

**"What if someone uses this against me in a protest?"**
They can't. Co-op data cannot be used in protests — it's explicitly
prohibited in the charter. The platform exists to make the fleet faster,
not to police it.

### What not to lead with

- Don't lead with "peer-to-peer" or "cryptographic identity" or
  "Ed25519 signatures." Nobody cares.
- Don't lead with the governance model. Lead with the value: faster
  sailing, better debriefs, fleet improvement.
- Don't lead with privacy protections. Lead with the positive: what
  they'll learn. Mention privacy when they ask "what's the catch?"

---

## Getting the first boats set up

### What each boat needs

- A **Raspberry Pi** (Pi 4 or Pi 5, 4GB+ RAM)
- A connection to the boat's **instrument system** (B&G, Garmin, etc.)
  via Signal K
- **Internet access** on the boat (phone hotspot is fine)
- A **Tailscale account** (free tier, takes 2 minutes)

### The setup sequence

1. **Start with your own boat.** Get Helm Log running, record a few
   races, make sure it works. You need to be confident before you
   evangelize.

2. **Recruit 2-3 early adopters.** Pick the boats most likely to be
   interested — usually the boats that already have instrument systems
   and care about data. Help them set up in person (dock day, beer
   recommended).

3. **Create the co-op.** Once you have 3 boats, create the co-op from
   your Pi. You'll be the initial admin. Pick one other tech-capable
   sailor as a backup admin.

4. **Run it for a few weeks.** Share races, compare tracks, show people
   the fleet benchmark view. Let the value speak for itself.

5. **Invite the rest of the fleet.** Once the early adopters are hooked,
   the remaining boats will want in. Help them set up one at a time.

### Common setup issues

| Problem | Solution |
|---|---|
| Pi won't connect to instruments | Check Signal K is running and the NMEA 2000 gateway is configured |
| No GPS data | Make sure the instrument system is outputting position (PGN 129025) |
| Pi loses time after power-off | Normal — it syncs via NTP when internet is available. The protocol handles clock drift. |
| Tailscale won't connect | Check that the phone hotspot is active and the Pi has internet |
| "My boat's WiFi is terrible" | Hardwire the Pi to a small router, tether the router to a phone |

---

## Running the co-op

### Day-to-day (almost nothing)

Once the co-op is running, your job is minimal:

- **Approve new boats** when they request to join
- **Check the audit log** occasionally (is anyone accessing data weirdly?)
- **Answer questions** from fleet members

### Seasonal tasks

- **Start of season**: renew coach access if applicable, check all Pis
  are online and up to date
- **End of season**: review co-op membership, remove boats that sold or
  left the fleet
- **If a vote is needed**: proposals (like enabling current model sharing)
  are created from your admin page and sent to all active members

### What to do when things go wrong

| Situation | What to do |
|---|---|
| A boat's Pi dies | Help them set up a new one. Their co-op membership is stored on other Pis — they just need to re-join. Historical data on the dead Pi is lost unless they had backups. |
| Someone wants to leave | They can leave from their own Pi. Their data is anonymized within 30 days. No drama needed. |
| Dispute about data sharing | Point to the charter. If the charter doesn't cover it, propose an amendment and vote. |
| Coach wants more access than allowed | Explain the rules. The platform enforces them — it's not personal, it's policy. |
| Fleet is too small (<3 boats) | The co-op can still function but is in "light mode" — simpler governance, single admin. Recruit more boats. |
| "I shared a session but nobody can see it" | The other boats need to be online to pull the data. If their Pis are off, it syncs next time they connect. |
| "The clock on my Pi is wrong" | Normal after a power cycle — the Pi has no battery-backed clock. It syncs via NTP when internet is available. The protocol tolerates up to 5 minutes of drift. |
| "I only want to share with some boats" | Sharing is all-or-nothing within the co-op. If you want selective sharing, keep the session private and share individual files directly (outside the platform). |
| "A new boat joined but can't see old races" | Correct — new members see sessions shared after they join. The co-op doesn't backfill historical data to new members. |

---

## Light mode vs full mode

### Light mode (3-5 boats)

For small fleets, keep it simple:

- **Single admin** with a designated backup
- **No formal voting** — decisions by consensus on the dock
- **No multi-admin signatures** — the admin manages join/leave directly
- **No embargo periods** — data is shared immediately
- **Charter is optional** — a handshake agreement is fine at this size

### Full mode (6+ boats)

As the fleet grows, formalize:

- **2-3 admins** with multi-admin signing
- **Written charter** (use the template)
- **Formal voting** on proposals (2/3 supermajority)
- **Embargo support** if the fleet wants delayed sharing
- **Coach access** with time-limited grants

The transition from light to full mode happens naturally. When the fleet
is big enough that a handshake isn't sufficient, you create a charter
and add a second admin.

---

## How decisions get made

As the fleet grows, people will ask governance questions: "Can we add a
coach?", "Can we share data with another fleet?", "What if someone
breaks the rules?"

### In light mode (3-5 boats)

Decisions happen on the dock. You're the admin, you have a backup, and
everyone knows each other. If the fleet agrees, you make the change.
There's no formal process because you don't need one yet.

### In full mode (6+ boats)

Decisions go through the charter:

- **Routine changes** (adding a boat, renewing coach access) — the admin
  handles it directly
- **Policy changes** (enabling current model sharing, granting cross-co-op
  access, adding a new admin) — require a proposal and a 2/3 supermajority
  vote from active members
- **Current model sharing** — requires unanimous consent (higher bar because
  current knowledge is competitively valuable)

The platform enforces these rules. You don't have to remember who voted
for what — the signed records are on every Pi.

### If there's a dispute

Point to the charter. If the charter doesn't cover the situation, propose
an amendment and vote on it. The goal is to keep governance lightweight
but explicit — nobody should be surprised by a decision.

---

## What success looks like after one month

If things go well, here's where you'll be four weeks in:

- **5-8 boats sharing** race data regularly
- **Coach running debriefs** with real track overlays and fleet benchmarks
- **Fleet benchmark heatmap** showing where each boat is strong and weak
- **A culture of sharing** — boats asking each other "did you share that
  race?" instead of hoarding data
- **Better racing** — boats that were mid-fleet starting to close the gap
  because they can see exactly what the leaders do differently

You don't need the whole fleet on day one. Three boats sharing
consistently is enough to prove the value. The rest will follow.

---

## How to run a dock talk

Your first group pitch should take about 15 minutes:

1. **5 minutes: what it is** — use the mental model ("each boat is its
   own server"), show the diagram, explain shared vs private
2. **5 minutes: show a shared race** — pull up a real session on your
   phone, overlay two boats, point out where one gained on a beat
3. **5 minutes: Q&A** — let people ask questions, handle objections
   using the language above

Bring a printout of the [Fleet Quickstart](fleet-quickstart.md) for
anyone who wants to take something home.

---

## The narrative you're building

You're not just setting up software. You're building a culture of
reciprocal improvement in your fleet. The boats that share data and
debrief together will get faster. The fleet as a whole will rise.

The best fleets in the world do this already — they just do it with
expensive commercial tools, paid coaches, and centralized platforms.
Helm Log makes it possible for any fleet, anywhere, with no subscription
and no middleman.

That's the story. Tell it.

---

## Resources

- [Fleet Quickstart](fleet-quickstart.md) — one-page handout, print and
  give to every boat at the dock
- [How the Co-op Works](guide-sailors.md) — give this to sailors who
  want to understand the co-op before joining
- [Coach Access Guide](guide-coaches.md) — give this to coaches
- [Data Licensing Policy](data-licensing.md) — the full policy (technical)
- [Federation Protocol](federation-design.md) — how the protocol works
  (technical)
- [Co-op Charter Template](co-op-charter-template.md) — fillable charter
  for your co-op
