---
name: data-license
description: Review code changes against the HelmLog-specific data licensing policy (docs/data-licensing.md) — embargoes, protest firewall, biometrics consent, gambling exclusion, fleet-benchmark thresholds, coach access expiry. The model recovers general PII / cross-tenant / audit-logging concerns by default; this skill encodes only the HelmLog-specific items that are not recoverable from general engineering judgment. TRIGGER when modifying code that handles user data, PII, co-op/federation data sharing, export endpoints, deletion/anonymization, or audit logging. Key files — storage.py, export.py, peer_api.py, peer_client.py, federation.py, transcribe.py, audio.py, web.py (data endpoints). DO NOT trigger for UI-only changes, instrument decoding, polar analysis, config, docs, or CSS/JS/templates.
---

# /data-license — HelmLog-specific compliance review

The model already catches general PII concerns by default (crew emails as
PII, cross-tenant boundary violations, audit-log gaps, rate limits). This
skill encodes only the HelmLog-specific policies a generalist would miss.
For the exhaustive policy, read `docs/data-licensing.md` directly.

## 1. Identify data-touching changes

Check `git diff` (staged + unstaged) and any new files. Skip if no
data-touching code is involved — report "No data licensing impact."

## 2. HelmLog-specific items to verify

These are the policies a general PII review will NOT catch:

### Temporal sharing embargoes (Section 2)
- [ ] Track data shared to co-op peers respects per-session embargo
      timestamps before serving.
- [ ] When an embargo check is present, verify it is **semantically
      correct** — the comparison operator must match the meaning of
      `embargo_until` (is it the first shareable moment or the last
      blocked moment?). Presence of a check is not sufficient.
- [ ] Embargo policy is enforced at the co-op level, not per-boat.
- [ ] Benchmarks exclude embargoed session data until embargo lifts.

### Fleet benchmarks (Section 2)
- [ ] No boat identities or per-boat data points exposed in benchmark
      outputs.
- [ ] Minimum 4-boat threshold per condition bin enforced before a
      benchmark is published.

### Coach access (Section 2)
- [ ] Coach access grants are time-limited with an expiry date — no
      permanent grants.
- [ ] Per-session authorization required from boat owner, not blanket
      access.
- [ ] Coaches may not bulk-export or aggregate multiple boats' data
      into a derived dataset retained independently of the platform.

### Biometric data (Section 1)
- [ ] Biometric data has per-person consent separate from instrument
      sharing — boat-owner consent is not sufficient.
- [ ] Biometric data cannot be used in personnel decisions.

### AIS / other-vessel data (Section 1)
- [ ] AIS and proximity data from other vessels is excluded from
      ingest, storage, export, and federation.

### Protest firewall (Section 11)
- [ ] Co-op data from other boats is not exportable in protest-ready
      formats.

### Gambling / commercial use (Section 9)
- [ ] Co-op data is not used for gambling, betting, or wagering.
- [ ] No commercial use without a co-op vote.

### ML opt-out (Section 8)
- [ ] Co-op data is not used for ML without governance approval.
- [ ] ML opt-out flag is respected.

### Crew / video PII deletion (Section 1)
- [ ] Crew emails are deleted when access is revoked (not soft-retained).
- [ ] Video face-blur / removal requests are supported per-crew.
- [ ] Camera consent: crew are informed when cameras are recording.
- [ ] YouTube uploads default to unlisted; video links are boat-private.

### Retention boundaries (Section 5)
- [ ] Data portability formats: CSV, GPX, JSON, WAV.
- [ ] Soft delete (suppression) supported during 30-day grace period.
- [ ] Hard delete is irreversible after grace period.

## Don't rationalize skipping the review

These policies are invisible to a generalist PII pass — which is exactly
why "it looked fine" is not a clearance. Common excuses and rebuttals:

| Rationalization | Rebuttal |
|---|---|
| "General PII looks clean, so we're compliant." | The general pass cannot see embargoes, the protest firewall, the 4-boat benchmark floor, or biometric consent separation. Walk Section 2 explicitly. |
| "There's already an embargo check, so it's covered." | Presence ≠ correctness. Verify the comparison operator matches `embargo_until`'s meaning — a flipped check leaks early. |
| "Boat owner consented, so biometric data is fine." | Biometrics need per-person consent separate from instrument sharing. Owner consent is not sufficient. |
| "Coach just needs access for the season." | No permanent or blanket grants — time-limited, per-session authorization only. |
| "It's only an export format tweak." | Export is where the protest firewall and AIS-exclusion rules bite. Re-check both before clearing. |

## 3. Report findings

For each issue: state the policy section, quote the relevant policy
language, describe the violating code, suggest a fix.

If all checks pass, report "Data licensing review: compliant — verified
against HelmLog-specific policies."
