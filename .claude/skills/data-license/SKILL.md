---
name: data-license
description: Review code changes against the data licensing policy for compliance
---

# Data License Compliance Review

Review the current changes against `docs/data-licensing.md` to ensure
compliance with the Helm Log data licensing policy.

## 1. Identify Data-Touching Changes

Check `git diff` (staged + unstaged) and any new files for code that:

- Stores, reads, exports, or deletes user data
- Serves data via API endpoints
- Handles PII (audio, photos, emails, biometrics, transcripts)
- Implements co-op membership, sharing, or governance
- Adds new data types or collection mechanisms

If no data-touching changes are found, report "No data licensing impact" and stop.

## 2. Check Against Policy Sections

For each data-touching change, verify compliance with the relevant sections:

### Section 1 — Data Ownership
- [ ] Boat owner retains full export/delete/restrict rights over their data
- [ ] AIS and proximity data from other vessels is excluded
- [ ] Audio/photo/email deletion and anonymization rights are supported
- [ ] Biometric data (if any) has per-person consent separate from instrument sharing
- [ ] Biometric data cannot be used in personnel decisions
- [ ] Crew emails are deleted when access is revoked

### Section 2 — Data Sharing
- [ ] Default shared data is limited to: instrument data, session metadata,
      derived metrics, race results
- [ ] Private data (audio, notes, sails, currents, photos, YouTube, crew roster)
      is not exposed to co-op endpoints
- [ ] No bulk export of other boats' co-op data
- [ ] Temporal sharing embargo timestamps are respected before serving track data
- [ ] Temporal sharing policy is enforced at the co-op level, not per-boat

### Section 5 — Retention and Deletion
- [ ] Data portability: own-boat data exportable in CSV, GPX, JSON, WAV
- [ ] Suppression (soft delete) works during 30-day grace periods
- [ ] Hard delete is irreversible after grace period

### Section 8 — AI/ML
- [ ] Co-op data is not used for ML without governance approval
- [ ] ML opt-out flag is respected

### Section 9 — Commercial Use
- [ ] Co-op data is not used for gambling, betting, or wagering purposes
- [ ] No commercial use without co-op vote

### Section 11 — Liability
- [ ] Co-op data from other boats is not exportable in protest-ready formats

### Section 12 — Technical Requirements
- [ ] Audit logging for co-op data access
- [ ] Rate limiting with auto-freeze on anomalous patterns

## 3. Report Findings

For each issue found:
1. State the policy section violated
2. Quote the relevant policy language
3. Describe the code that violates it
4. Suggest a fix

If all checks pass, report "Data licensing review: compliant."
