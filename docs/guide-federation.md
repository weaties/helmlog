# Federation — Setting Up Identity and Co-ops

> How to create your boat's cryptographic identity, start or join a co-op,
> and share race sessions with your fleet.

_Requires: HelmLog with federation support (schema v28+)._

---

## Quick overview

Federation lets boats share race data directly with each other — no central
server. Each boat has a cryptographic identity (Ed25519 keypair), and co-ops
are groups of boats that agree to share data.

```
  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
  │  Your Pi     │◄───►│  Fleet mate  │◄───►│  Fleet mate  │
  │  (identity)  │     │  (identity)  │     │  (identity)  │
  └──────────────┘     └──────────────┘     └──────────────┘
        │                     │                     │
        └──────── co-op (shared data) ──────────────┘
```

All communication happens over your existing Tailscale mesh. Your private
key never leaves your Pi.

---

## 1. Create your boat identity

Every boat needs an identity before it can join a co-op. Open the admin
interface and go to **Federation** (or navigate to `/admin/federation`).

If no identity exists yet, you'll see an initialization form:

1. Enter your **sail number** and **boat name**
2. Enter your **owner email** — optional for standalone use, but required
   for co-op membership (used for admin communication, votes, emergencies;
   visible only to co-op admins)
3. Click **Initialize Identity**

The page will show your identity details including your **fingerprint** —
a short hash of your public key that uniquely identifies your boat in the
co-op system.

Use the **Copy boat card JSON** button to copy your public identity
document. This is what you'll share with fleet mates who want to invite
you to a co-op (or vice versa). The boat card is public information —
safe to send over Slack, email, or AirDrop.

### What gets created

| File | Purpose |
|------|---------|
| `~/.helmlog/identity/boat.key` | Ed25519 private key (mode 0600 — only readable by the service) |
| `~/.helmlog/identity/boat.pub` | Public key (freely shareable) |
| `~/.helmlog/identity/boat.json` | Boat card — your public identity document |

The private key never leaves the Pi. A reference copy of the public key
and fingerprint is also stored in the SQLite database.

---

## 2. Create a co-op

The first boat to set up a co-op becomes its **moderator** (admin).
Typically this is the fleet captain or whoever is organizing data sharing.

On the Federation admin page, scroll to the **Create Co-op** section:

1. Enter a **co-op name** (e.g. "Puget Sound J/105")
2. Optionally enter **areas** as a comma-separated list (e.g.
   "Elliott Bay, Shilshole")
3. Click **Create Co-op**

The co-op appears in the **Co-op Memberships** section with your boat
listed as admin and sole member. The co-op charter is a cryptographically
signed document stored on disk.

---

## 3. Invite boats to the co-op

To invite another boat, you need their **boat card** — the JSON document
they can copy from their own Federation admin page.

On the Federation admin page, scroll to **Invite Boat to Co-op**:

1. Select which co-op to invite them to (if you admin more than one)
2. Paste the invitee's boat card JSON into the text area
3. Click **Send Invitation**

The invitation creates a cryptographically signed membership record. The
invitee's boat appears in the co-op's member list.

### How to exchange boat cards

The boat card is public information. Common ways to share it:

- **Copy/paste** — click "Copy boat card JSON" on the Federation page and
  send it over Slack, iMessage, email, etc.
- **File transfer** — copy `~/.helmlog/identity/boat.json` via AirDrop,
  USB stick, or `scp`
- **In person** — show the QR code on your phone (future feature)

---

## 4. Share a race session

Once you're in a co-op, you can share individual race sessions. This is
done per-session, per-co-op — you always choose exactly what to share.

Session sharing will be available on the session detail page (a "Share
with co-op" button). You can also share with an optional **embargo** — a
date after which the data becomes visible to the co-op. This is useful
for series where you don't want competitors to see your data until the
series is over.

### What gets shared

| Shared | Kept private |
|--------|-------------|
| GPS track | Audio recordings |
| Boat speed & angles | Photos & notes |
| Wind data | Crew roster |
| Race results | Sail selection |
| Heading & COG | Debrief transcripts |

---

## 5. Data ownership

Your data stays on your Pi. When another boat queries the co-op, their Pi
talks directly to yours over Tailscale. You can:

- **Unshare** any session at any time
- **Leave** a co-op at any time — your data goes with you
- **Export** your own data in any format regardless of co-op membership
- **Delete** your data — co-op peers only cache metadata, not your full tracks

For the full data ownership and privacy policy, see `docs/data-licensing.md`.

---

## Command-line reference

Everything above can also be done from the command line. This is useful
for scripting, headless setups, or SSH sessions.

### Identity

```bash
# Create identity
helmlog identity init --sail-number 69 --boat-name "Javelina" --email skipper@example.com

# View current identity
helmlog identity show

# Regenerate identity (WARNING: changes your boat's cryptographic identity)
helmlog identity init --sail-number 42 --boat-name "New Boat" --force
```

### Co-op management

```bash
# Create a co-op (you become admin)
helmlog co-op create --name "Puget Sound J/105" --area "Elliott Bay" --area "Shilshole"

# Check membership status
helmlog co-op status

# Invite a boat using their boat card file
helmlog co-op invite ./blackhawk-boat.json

# Invite to a specific co-op (if you admin multiple)
helmlog co-op invite ./blackhawk-boat.json --co-op-id a1b2c3d4e5f6g7h8
```

---

## Filesystem layout

After creating an identity and a co-op, the `~/.helmlog/` directory
looks like this:

```
~/.helmlog/
├── identity/
│   ├── boat.key          # Private key (0600 permissions)
│   ├── boat.pub          # Public key
│   └── boat.json         # Boat card (shareable)
└── co-ops/
    └── <co-op-id>/
        ├── charter.json  # Signed co-op charter
        └── members/
            ├── <your-fingerprint>.json    # Your membership record
            └── <invitee-fingerprint>.json # Each invited boat
```

---

## Troubleshooting

**"No identity initialized yet"** — Click Initialize Identity on the
Federation admin page, or run `helmlog identity init` from the command
line.

**"Co-op requires an owner email"** — Re-initialize identity with an
email address. On the admin page this means creating a new identity
(the current one must be regenerated). From the CLI, use
`helmlog identity init --email your@email.com --force`.

**"You are not an admin of any co-op"** — Only the co-op admin can
invite boats. Ask your fleet captain to send the invitation.

**"Identity already exists"** (CLI only) — Your boat already has an
identity. Use `helmlog identity show` to see it. Only use `--force`
if you genuinely need a new keypair.

**"Boat card missing required fields"** — The pasted JSON must contain
`pub`, `fingerprint`, `sail_number`, and `name`. Make sure you're
pasting the full boat card, not a partial snippet.
