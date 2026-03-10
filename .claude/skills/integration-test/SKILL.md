---
name: integration-test
description: Run federation integration tests — choose the appropriate layer based on the change
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

**What it covers (32 tests):**

| File | Tests | Covers |
|------|-------|--------|
| `test_federation_e2e.py` | 10 | Identity, session list, track fetch, audit logging |
| `test_auth_e2e.py` | 10 | Valid/invalid sigs, replay, forgery, non-member rejection |
| `test_embargo_e2e.py` | 5 | Embargo enforcement, lift, share/unshare lifecycle |
| `test_data_license_e2e.py` | 7 | Field allowlist, PII protection, private session isolation |

**Key fixture: `fleet`** (from `tests/integration/conftest.py`):
- `fleet.boat_a` — admin boat (Javelina, sail 42)
- `fleet.boat_b` — member boat (Corvo, sail 69)
- Each has: `.identity`, `.storage`, `.client` (httpx), `.resources` (session IDs)
- `fleet.boat_b.sign("GET", path)` → signed headers for requests to boat_a

## Layer 2 — Pi Smoke Tests (over Tailscale)

Runs on one Pi and exercises the real running helmlog service on a peer Pi.
Validates real Tailscale WireGuard, systemd, nginx, NTP sync.

```bash
# From your Mac:
ssh weaties@corvopi-tst1 "cd ~/helmlog && uv run python scripts/integration_smoke.py --peer corvopi-live"

# Directly on the Pi:
uv run python scripts/integration_smoke.py --peer corvopi-live

# JSON output for CI:
uv run python scripts/integration_smoke.py --peer corvopi-live --json
```

**When to use:** Before merging major federation PRs. After deploying a
federation change to both Pis.

**Prerequisites:**
- Both Pis have initialized identities (`helmlog identity init`)
- Both Pis are members of at least one shared co-op
- Both Pis are on the same Tailscale network

**9 scenarios:** peer identity, local identity, signed request, bad signature
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
| corvopi-live | Live Pi, Layer 2 target | Yes |
| Docker containers | Disposable, Layer 3 | Yes |
