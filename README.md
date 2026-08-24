# NeatSpace-Core

Autonomous affiliate pinning system: **deterministic Python orchestrator + Google ADK
agents**. Fetches products from official affiliate APIs (AliExpress Portals today,
Temu/Amazon later via adapters), vets and enriches them with Gemini, publishes bridge
pages to static hosting, and pins to niche-specific Pinterest accounts — all driven by
crash-safe state machines in MongoDB and executed by GitHub Actions cron.

Architecture principle: **LLM for judgment (Moderator, Strategist), plain code for
control flow and side effects.** The orchestrator never asks a model what to do next.

## Status

| Work package | Scope | Status |
|---|---|---|
| WP1 | Scaffold, CI (mongo:7 service container), env contract | ✅ done |
| WP2 | Mongo index migrations incl. 3 unique double-publish backstops | ✅ done |
| WP3 | Declarative state machine registry + pure validator | ✅ done |
| WP4 | Repo layer: atomic claims, leases, transitions, sweeps | ✅ done |
| WP5 | Governor (warm-up curves, quotas, spacing) | planned |
| WP6 | AES-256-GCM token envelope encryption | planned |
| WP7 | Seeds (niches/accounts) + ops tools | planned |
| Phase 2 | Adapters, ADK agents, bridge/pinterest tools | planned |
| Phase 3 | GH Actions runner, Telegram, dashboard | planned |

## Setup

Python 3.12+, then:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   bash: source .venv/bin/activate
python -m pip install -e ".[dev]"

cp .env.example .env   # fill in MONGO_URI, MONGO_DB, TOKEN_MASTER_KEY
```

## Testing

```bash
pytest          # pure registry tests always run; Mongo tests skip if no local Mongo
ruff check .    # lint
```

Mongo-backed tests (migrations, unique-index guarantees) need a reachable Mongo:

- **CI**: runs automatically on every push — a `mongo:7` service container serves
  them; unreachability is a hard failure there (`MONGO_TEST_REQUIRE=1`).
- **Locally against Atlas**: `set MONGO_TEST_URI=<your-atlas-uri>` (Windows:
  `export` in bash). Tests touch **only** `MONGO_TEST_DB`
  (default `affiliate-pinner-test`) and **drop it per test** — your production
  database is never modified.

## Migrations

```bash
python scripts/migrate.py            # creates all indexes (idempotent, safe per deploy)
```

## Safety-critical invariants (do not weaken)

1. `products(source, source_product_id)` unique — ingestion idempotency.
2. `pins(account_id, product_id)` unique — **a product can never be pinned twice
   to one account**, even by buggy code.
3. `pins(pin_id)` partial unique — API-level duplicate pins are rejected.
4. All control flow lives in `pinner/statemachine/registry.py` as data; the
   exhaustive matrix tests in `tests/test_registry.py` are the contract. A
   transition not in the registry cannot happen.
5. Forward-only state machines; only `OPS` events exit terminal states.

## Layout

```
pinner/
├── statemachine/    # registry.py (pure data) + validator.py (pure functions)  [WP3]
├── repo/            # mongo.py [WP2]; engine.py (claims/leases/transitions/
│                    #   sweeps/pause) + pins.py + products.py + audit.py [WP4]
├── governor/        # quotas (WP5)
├── crypto/          # token envelope (WP6)
├── adapters/        # aliexpress (+temu/amazon later)          [Phase 2]
├── agents/          # ADK Moderator + Strategist               [Phase 2]
├── tools/           # bridge (GitHub/Pages) + Pinterest        [Phase 2]
├── runner/          # GH Actions entrypoint                    [Phase 3]
└── config.py        # frozen env contract
scripts/migrate.py   # index migrations CLI
tests/               # exhaustive registry matrix + schema tests
.github/workflows/   # ci.yml (+ runner.yml placeholder, manual only)
```
