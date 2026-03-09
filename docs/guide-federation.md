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

Every boat needs an identity before it can join a co-op. Run this once:

```bash
helmlog identity init --sail-number 69 --boat-name "Javelina" --email skipper@example.com
```

This creates:

| File | Purpose |
|------|---------|
| `~/.helmlog/identity/boat.key` | Ed25519 private key (mode 0600 — only readable by you) |
| `~/.helmlog/identity/boat.pub` | Public key (freely shareable) |
| `~/.helmlog/identity/boat.json` | Boat card — your public identity document |

The **fingerprint** (a short hash of your public key) is your boat's unique
identifier in the co-op system. It's printed when you create your identity.

**Email is optional for standalone use but required for co-op membership.**
It's visible only to your co-op's admin and is used for out-of-band
communication (votes, admin transfers, emergencies).

To view your identity later:

```bash
helmlog identity show
```

Output:

```
Boat:        Javelina
Sail number: 69
Fingerprint: a1b2c3d4e5f6g7h8
Public key:  <base64-encoded key>
Email:       skipper@example.com
```

### Regenerating your identity

If you need to start fresh (lost key, new boat):

```bash
helmlog identity init --sail-number 42 --boat-name "New Boat" --force
```

**Warning:** `--force` generates a new keypair. Your old identity is gone.
Other boats in your co-op will see you as a different boat. You'll need
to be re-invited to any co-ops.

---

## 2. Create a co-op

The first boat to set up a co-op becomes its **moderator** (admin). Typically
this is the fleet captain or whoever is organizing data sharing.

```bash
helmlog co-op create --name "Puget Sound J/105" --area "Elliott Bay" --area "Shilshole"
```

Output:

```
Co-op created:
  Name:    Puget Sound J/105
  ID:      a1b2c3d4e5f6g7h8
  Admin:   Javelina (a1b2c3d4e5f6g7h8)
  Areas:   Elliott Bay, Shilshole
  Charter: ~/.helmlog/co-ops/a1b2c3d4e5f6g7h8/charter.json
```

The co-op charter is a cryptographically signed document that records
who created the co-op and what its rules are. The `--area` flag is
optional and repeatable.

### Check your co-op status

```bash
helmlog co-op status
```

Output:

```
Co-op: Puget Sound J/105
  ID:      a1b2c3d4e5f6g7h8
  Role:    admin
  Status:  active
  Joined:  2026-03-08T12:00:00Z
```

---

## 3. Invite boats to the co-op

To invite another boat, you need their **boat card** — the `boat.json`
file from their Pi. They can send it to you over Slack, email, AirDrop,
USB stick — it's public information, not sensitive.

```bash
# The other boat shares their boat card:
# scp fleet-mate:/home/helmlog/.helmlog/identity/boat.json ./blackhawk.json

# You (the admin) sign an invitation:
helmlog co-op invite ./blackhawk.json
```

If you're admin of multiple co-ops, specify which one:

```bash
helmlog co-op invite ./blackhawk.json --co-op-id a1b2c3d4e5f6g7h8
```

This creates a signed membership record that the invitee's Pi will use
to authenticate with your co-op.

---

## 4. Share a race session

Once you're in a co-op, you can share individual race sessions. This is
done per-session, per-co-op — you always choose exactly what to share.

Session sharing will be available through the web UI on the session detail
page (a "Share with co-op" button). You can also share with an optional
**embargo** — a date after which the data becomes visible to the co-op.
This is useful for series where you don't want competitors to see your
data until the series is over.

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

## Filesystem layout

After creating an identity and a co-op, your `~/.helmlog/` directory
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

**"No identity found"** — Run `helmlog identity init` first. Every
federation command requires an identity.

**"Co-op membership requires an owner email"** — Re-run
`helmlog identity init` with `--email your@email.com`. You'll need
`--force` if you already have an identity (this regenerates your keys).

**"You are not an admin of any co-op"** — Only the co-op admin can
invite boats. Ask your fleet captain to send the invitation.

**"Identity already exists"** — Your boat already has an identity.
Use `helmlog identity show` to see it. Only use `--force` if you
genuinely need a new keypair.
