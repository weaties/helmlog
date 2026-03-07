# Incident: SSH Lockout — 2026-03-01

## Summary

An autonomous Claude Code session running on the Raspberry Pi (`corvopi`)
executed SSH hardening commands from issue #116 that deleted the only working
SSH key from `~/.ssh/authorized_keys`. The operator was immediately locked out
of the Pi with no remote recovery path. Access was restored by physically
removing the microSD card, mounting it on a Mac, and using `debugfs` to write
new keys directly to the ext4 filesystem.

**Duration:** ~2 hours from lockout to restored access.

**Impact:** No data loss. The Pi continued running all services (Signal K,
InfluxDB, Grafana, helmlog) throughout the incident — only SSH access was
affected.

---

## Timeline

### What triggered the lockout

Issue [#116](https://github.com/weaties/helmlog/issues/116) defined a
multi-phase security hardening plan for the Pi deployment. The operator ran
Claude Code in autonomous ("yolo") mode on the Pi to execute Phases 1 and 2.

As part of Phase 1, issue #111 ("Harden SSH: disable X11, remove RSA key")
included this command:

```bash
sed -i '/^ssh-rsa /d' ~/.ssh/authorized_keys
```

The intent was to remove a "legacy RSA-2048 key" and keep "only Ed25519." The
problem: the RSA key was the operator's **only working key**. The Ed25519 key
already present in `authorized_keys` (`...AMDot`) was a different key placed
there by cloud-init during the original Pi OS imaging — it did not match the
operator's Ed25519 key (`...h2Dw`), which had never been added to the Pi.

After the RSA key was deleted and `sshd` was reloaded, every SSH attempt was
rejected:

```
weaties@corvopi: Permission denied (publickey).
```

### Troubleshooting steps

**1. Confirmed the lockout was real**

Verbose SSH (`ssh -v`) showed both the RSA key and the Ed25519 key from the
SSH agent being offered and rejected. The server only accepted `publickey`
authentication — no password fallback.

**2. Checked Tailscale as a bypass**

The Pi was online on Tailscale (`tailscale status` showed it active). However:

- Regular SSH over the Tailscale IP (`100.122.21.5`) hit the same OpenSSH
  rejection — Tailscale was just proxying to the Pi's `sshd`.
- `tailscale ssh` (Tailscale's own SSH server, which bypasses OpenSSH) was not
  enabled on the Pi. Enabling it requires running `tailscale set --ssh` *on the
  Pi* — a catch-22 when you can't SSH in.
- The Tailscale admin console (login.tailscale.com) had no remote toggle to
  enable Tailscale SSH on a machine.

**3. Checked the web app**

The helmlog web app on port 3002 was responding, but the hardening session
had also enabled authentication:

```
$ curl http://corvopi:3002/
{"detail":"Not authenticated"}
```

No admin shell or command execution was available through the web interface.

**4. Physical access — microSD card**

The operator powered off the Pi and removed the microSD card. On the Mac:

- `/dev/disk12s1` (FAT32 `bootfs`) mounted automatically.
- `/dev/disk12s2` (ext4 `rootfs`) did **not** mount — macOS cannot read ext4
  natively.

**5. Attempted cloud-init (did not work)**

The boot partition had a `user-data` cloud-init config with the original SSH
keys. Editing it was considered, but cloud-init's `users` and `runcmd` modules
only execute on the first boot. Since the Pi had long since completed its first
boot, changes to `user-data` would be ignored.

**6. Installed `e2fsprogs` for ext4 access**

```bash
brew install e2fsprogs
```

This provided `debugfs`, a tool that can read and write files on ext4
filesystems without mounting them.

**7. First write attempt (did not persist)**

```bash
sudo debugfs -w /dev/disk12s2
debugfs: rm /home/weaties/.ssh/authorized_keys
debugfs: write /tmp/authorized_keys /home/weaties/.ssh/authorized_keys
```

The `cat` command confirmed the new keys were written. However, after
reinserting the card and booting the Pi, SSH still failed. Re-examining the
card showed the **old** key was back — `Size: 81` bytes (the original
cloud-init key).

**Root cause of the failed write:** The ext4 journal had not been flushed.
`debugfs` writes bypass the journal, so when the Pi booted and the kernel
replayed the journal, it reverted the changes.

**8. Cleared the journal, then rewrote**

```bash
sudo e2fsck -fy /dev/disk12s2   # recover journal, fix block counts
sudo debugfs -w /dev/disk12s2
debugfs: rm /home/weaties/.ssh/authorized_keys
debugfs: write /tmp/authorized_keys /home/weaties/.ssh/authorized_keys
debugfs: set_inode_field /home/weaties/.ssh/authorized_keys uid 1000
debugfs: set_inode_field /home/weaties/.ssh/authorized_keys gid 1000
debugfs: set_inode_field /home/weaties/.ssh/authorized_keys mode 0100600
```

After `e2fsck` cleared the journal, the `debugfs` write persisted through
reboot.

**9. Access restored**

```
$ ssh weaties@corvopi echo "back in"
back in
```

**10. Enabled Tailscale SSH as a backup path**

```bash
ssh weaties@corvopi "sudo tailscale set --ssh"
```

This ensures that even if OpenSSH's `authorized_keys` is broken again,
`tailscale ssh weaties@corvopi` will work using Tailscale identity-based
authentication.

---

## Root cause

The issue #116 hardening plan assumed:

1. The RSA key in `authorized_keys` was a "legacy" key that could be safely
   removed.
2. The Ed25519 key already present was the operator's current key.

Both assumptions were wrong. The RSA key was the operator's **only** working
key, and the Ed25519 key in `authorized_keys` was from cloud-init — not from
the operator's SSH agent.

The autonomous Claude Code session executed the plan as written without
verifying that the remaining key would actually allow the operator to log in.

---

## Remediation

### Immediate fixes

1. **Restored `authorized_keys`** with both the operator's Ed25519 and RSA
   public keys.
2. **Enabled Tailscale SSH** (`tailscale set --ssh`) as a backup access path
   that does not depend on OpenSSH's `authorized_keys`.

### Preventive changes (this commit)

1. **`setup.sh` SSH safety guard:** The SSH hardening section now verifies
   that `authorized_keys` exists and contains at least one valid key before
   applying any changes. If the file is missing or empty, hardening is skipped
   with a warning.

2. **`setup.sh` never modifies `authorized_keys` contents:** A comment block
   explicitly forbids adding or removing keys in the setup script. Key
   management is an operator action, not an automation action.

3. **`setup.sh` enables Tailscale SSH:** The Tailscale section now runs
   `tailscale set --ssh`, ensuring there is always a backup access path that
   bypasses OpenSSH.

---

## Lessons learned

1. **Never delete SSH keys in an automated script** without first confirming
   the operator can authenticate with what remains. Deleting a key is a
   one-way door — if you get it wrong, you need physical access.

2. **Autonomous agents should not modify authentication credentials.** SSH
   keys, passwords, sudoers files, and similar security-critical configs
   should require explicit operator confirmation before changes are applied.

3. **Always have a backup access path.** Tailscale SSH, a serial console, or
   at minimum a known console password. One access method is zero if it
   breaks.

4. **ext4 journal replay can revert `debugfs` writes.** When using `debugfs`
   to repair a Linux filesystem from another OS, run `e2fsck` first to flush
   the journal. Otherwise the kernel will replay the journal on boot and undo
   your changes.

5. **cloud-init `user-data` edits are ignored after first boot.** On a
   Raspberry Pi that has already completed initial setup, modifying
   `user-data` on the boot partition has no effect unless you also clear
   `/var/lib/cloud/` on the root partition.

6. **macOS cannot mount ext4.** If your recovery plan involves editing Linux
   filesystems from a Mac, install `e2fsprogs` (`brew install e2fsprogs`)
   ahead of time. The `debugfs` tool can read and write individual files
   without a full mount.
