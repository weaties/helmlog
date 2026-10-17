# Mac mirror — reviewing HelmLog while the Pi is on the water

A copy of the boat's HelmLog served from a Mac, so races can be reviewed and
features demoed (e.g. coach access) when the Pi is offline. It is a **mirror**,
not a second boat: data flows Pi → Mac only.

```
Pi logger.db ──sqlite3_rsync (15 min)──▶ ~/helmlog-mirror/replica/logger.db
                                               │ APFS clone (≤ hourly)
                                               ▼
           https://corvo105-mirror.helmlog.org ◀─ cloudflared ◀─ mirror.py serve
                                                                (data/logger.db)
```

| Piece | What it does |
|---|---|
| `scripts/mirror-sync.sh` | Pulls changed DB pages + `data/{audio,notes,avatars}` whenever the Pi is reachable; no-op when it isn't. |
| `scripts/mirror.py serve` | Web app only (no SK reader, audio, cameras, weather, deploy loop). Refuses deploy/network writes so they can't act on the Mac. |
| `scripts/mirror.py carry` | On each swap, keeps logins and coach grants created on the mirror. |
| `scripts/setup-mirror-mac.sh` | Installs the `com.helmlog.mirror-{sync,serve,tunnel}` launchd agents. |

## Freshness

The replica lags the Pi by ≤ 15 min while the Pi is reachable (dock Wi-Fi, or
cellular via Tailscale). The *served* copy is swapped in at most hourly
(`REFRESH_INTERVAL_S`), because each swap restarts the server. On the water
with no connectivity, the mirror shows everything up to the last sync.

**During a demo**, `touch ~/helmlog-mirror/HOLD` to stop swaps/restarts;
`rm` it afterwards.

## What doesn't flow back

Anything written on the mirror — notes, moments, race edits, tags — lives only
in the mirror's served DB and is **discarded at the next swap**. Only login
sessions and coach grants created on the mirror are carried across swaps (and
never sent to the Pi). Syncing mirror edits back to the Pi is deliberately out
of scope until there's a need.

## Which code it serves

`~/helmlog-mirror/code` is a detached worktree that **runs what the Pi runs,
in the Pi's deploy mode** (`DEPLOY_MODE` / `DEPLOY_BRANCH`, read from the
synced DB — change them on the Pi's Deploy admin and the mirror follows):

| Pi mode | Mirror code |
|---|---|
| `evergreen` | latest `origin/<DEPLOY_BRANCH>`, checked each sync — keeps advancing while the Pi is offline, as the Pi will when it reconnects |
| `explicit` | the commit checked out on the Pi, read over SSH when it's reachable |

On a change it checks out, `uv sync`s and restarts the server. `HOLD` pauses
this too. To demo a branch the Pi doesn't run, set `MIRROR_FOLLOW_PI=0` when
running `setup-mirror-mac.sh`, then:

```bash
git -C ~/helmlog-mirror/code checkout --detach <branch-or-sha>
(cd ~/helmlog-mirror/code && uv sync)
launchctl kickstart -k gui/$(id -u)/com.helmlog.mirror-serve
```

Schema migrations from that code run against the served copy only; the
replica stays on the Pi's schema, and the next swap re-applies them. Code
older than the DB's schema is fine — newer migration versions are skipped.

## Setup

1. **Backup SSH works** (`scripts/setup-backup-mac.sh`). The Tailscale ACL
   rule for `helmlog-backup` must be `"action": "accept"` — `"check"` blocks
   on a browser re-auth and the 15-min job can't click it.
2. **`sqlite3_rsync` on both ends, same SQLite release.** It isn't in Homebrew
   or Raspberry Pi OS; build it from the SQLite source zip
   (<https://sqlite.org/download.html>, verify the SHA3-256):
   ```bash
   ./configure --disable-tcl && make sqlite3_rsync
   ```
   Mac → `~/helmlog-mirror/bin/sqlite3_rsync` (if your shell runs under
   Rosetta, prefix with `arch -arm64`). Pi → `/home/helmlog-backup/bin/sqlite3_rsync`
   (build it on the Pi; needs `build-essential zlib1g-dev`).
3. **Cloudflare tunnel.** In Zero Trust → Networks → Tunnels, create a tunnel
   (e.g. `corvo105-mirror`) with public hostname `corvo105-mirror.helmlog.org`
   → `http://localhost:3012`. Use a single-level subdomain — Universal SSL
   doesn't cover `*.corvo105.helmlog.org`. Save the token:
   `brew install cloudflared && pbpaste > ~/helmlog-mirror/cloudflare-token && chmod 600 ~/helmlog-mirror/cloudflare-token`.
4. `./scripts/setup-mirror-mac.sh` — seeds the replica from the newest backup
   snapshot so the first sync is a delta, writes `~/helmlog-mirror/.env`, and
   loads the agents.

Auth stays **on**: the mirror has the Pi's users/passwords, and coach links
work as on the boat. Never set `AUTH_DISABLED` on a publicly tunnelled mirror.

## Operating

- Logs: `~/helmlog-mirror/logs/{mirror-sync,mirror-serve,mirror-tunnel}.log`
- Sync now: `launchctl kickstart gui/$(id -u)/com.helmlog.mirror-sync`
- Force a swap: `rm ~/helmlog-mirror/state/last-refresh`, then sync now.
- The launchd agents run scripts from the checkout that ran setup — after
  merging changes to these scripts, `git pull` that checkout.
