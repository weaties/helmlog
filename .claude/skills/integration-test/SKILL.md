---
name: integration-test
description: Run federation integration tests — choose the appropriate layer (in-process pytest, Pi harness, or Docker compose) based on the change. TRIGGER when modifying federation.py, peer_api.py, peer_client.py, peer_auth.py, or federation-related storage/auth code. Also trigger when tests/integration/ files change. DO NOT trigger for unrelated test runs, UI changes, instrument decoding, or non-federation features.
---

# Federation Integration Tests

Three-layer test strategy for validating inter-Pi federation, co-op auth,
embargo enforcement, and data licensing compliance.

## Layer 1 — In-Process Pytest (fast, any machine)

Two boats with real Ed25519 keypairs in the same pytest process. No mocking
of crypto. Tests run in ~5 seconds.

```bash
uv run pytest tests/integration/ -v --tb=short
```

**When to use:** Any change touching federation, co-op, peer API, or data
licensing code. Runs automatically in CI via `.github/workflows/integration.yml`.

**What it covers:**

| File | Covers |
|------|--------|
| `test_federation_e2e.py` | Identity, session list, track fetch, audit logging |
| `test_auth_e2e.py` | Valid/invalid sigs, replay, forgery, non-member rejection |
| `test_embargo_e2e.py` | Embargo enforcement, lift, share/unshare lifecycle |
| `test_data_license_e2e.py` | Field allowlist, PII protection, private session isolation |
| `test_session_matching_e2e.py` | Session matching: propose → confirm → name across two boats |

**Key fixture: `fleet`** (from `tests/integration/conftest.py`):
- `fleet.boat_a` — admin boat (Javelina, sail 42)
- `fleet.boat_b` — member boat (Corvo, sail 69)
- Each has: `.identity`, `.storage`, `.client` (httpx), `.resources` (session IDs)
- `fleet.boat_b.sign("GET", path)` → signed headers for requests to boat_a


## Layer 2 — Pi Test Harness (Mac → two Pis over Tailscale)

`scripts/pi_harness.py` runs from your Mac and orchestrates the full lifecycle
on two real Pis: identity init, co-op setup, data seeding via `harness_seed.py`,
smoke tests via `integration_smoke.py`, and teardown. Validates real Tailscale
WireGuard, systemd, nginx, NTP sync, and the live federation API end-to-end.

```bash
# One-time SSH key setup (per Pi, first time only)
ssh-keygen -t ed25519 -f ~/.ssh/helmlog-harness
ssh-copy-id -i ~/.ssh/helmlog-harness.pub weaties@<pi-a>
ssh-copy-id -i ~/.ssh/helmlog-harness.pub weaties@<pi-b>

# Full lifecycle (setup → seed → test → teardown)
uv run python scripts/pi_harness.py \
    --pi-a <pi-a-ip> --pi-b <pi-b-ip> \
    --ssh-key ~/.ssh/helmlog-harness

# Partial modes for iterative testing
uv run python scripts/pi_harness.py --setup-only ...    # identity + co-op + seed, leave in place
uv run python scripts/pi_harness.py --test-only ...     # re-run smoke tests against existing setup
uv run python scripts/pi_harness.py --teardown-only ... # remove AUTH_DISABLED, restart services

# Post structured results to a GitHub issue
uv run python scripts/pi_harness.py ... --issue 281
```

**When to use:** Before merging major federation PRs. After deploying a
federation change to both Pis. Use `--setup-only` to leave a live federation
in place for manual exploration.

**Prerequisites:**
- SSH key copied to both Pis (one-time, see above)
- Both Pis have helmlog installed and service running
- Both Pis are on the same Tailscale network
- The harness handles identity init and co-op setup automatically

**What the harness does automatically:**
1. Preflight: SSH connectivity, service health, branch check
2. Setup: enables `AUTH_DISABLED=true`, inits identities via web API, creates co-op, invites + joins
3. Seed: runs `harness_seed.py` on each Pi to create sessions with instrument data and share them
4. Test: runs `integration_smoke.py` from Pi-A targeting Pi-B, collects JSON results
5. Teardown: removes `AUTH_DISABLED`, restarts services

**9 smoke scenarios:** peer identity, local identity, signed request, bad signature
rejection, no-auth rejection, non-member rejection, track fetch, embargo
enforcement, audit trail.


## Layer 3 — Docker Compose (two containers on Mac)

Two real helmlog instances on an isolated Docker network. Matches Pi
architecture (linux/arm64).

```bash
docker compose -f tests/integration/docker-compose.yml up --build --abort-on-container-exit
```

**When to use:** Testing process isolation, network failure scenarios, or
when you want to validate against the Pi-matching arm64 architecture without
deploying to actual Pis.

## Adding New Integration Tests

1. Add tests to the appropriate file in `tests/integration/`
2. Use the `fleet` fixture — it provides two fully-seeded boats
3. Use `fleet.boat_b.sign("GET", path)` for authenticated requests
4. Query `fleet.boat_a.client` (the server boat's ASGI client)
5. Seed data goes in `tests/integration/seed.py`
6. Run `uv run ruff check . && uv run ruff format .` after adding tests

**Test data available in the fleet fixture:**
- `fleet.boat_a.resources["shared_session_id"]` — session shared with co-op (has 10 seconds of instrument data)
- `fleet.boat_a.resources["embargo_session_id"]` — session under future embargo
- `fleet.boat_a.resources["private_session_id"]` — unshared session (invisible to peers)
- `fleet.boat_a.co_op_id` — the test co-op ID

## Environment

| Host | Role | Yolo OK? |
|------|------|----------|
| Mac (dev machine) | Docker host, test orchestrator, Layer 1 | No |
| corvopi-tst1 | Test Pi, Layer 2 source | Yes |
| corvopi-live | Live boat Pi, Layer 2 target | **No** — the harness sets `AUTH_DISABLED=true` during setup; confirm with the user before running it here |
| Docker containers | Disposable, Layer 3 | Yes |
